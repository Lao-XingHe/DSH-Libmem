"""B6 MAP v0.1 记忆代理协议

对齐：《通用的记忆 Agent 协议.md》原始设计
三包：memory_request / memory_response / memory_drilldown
枚举：task_type 七类；depth 为 L1/L2/L3
三条硬规则：
  1. 不猜指代 — 指代不明时 needs_clarification=true + clarification_question
  2. 不编造   — 空结果诚实返回 empty=true，不补
  3. 不越权   — decided_by 必须为 "user"

核心设计：默认浅层(L1) + 预算硬限制 + 按需深挖(L2/L3) → 上下文不爆炸

接线状态（2026-09-22）
--------------------
`MAPValidator` 与 `check_and_retry` 早就写好了，**但从来没人调用** —— 和 B5 升温状态机、
B2→B3 索引、B7 审计是同一种病：模块完整，缺接线。现在 `request_handler` 本身
就是带校验的入口（`guarded_request_handler`），所有调用者（MCP / FastAPI / CLI）
自动获得校验，不需要各自记得去调 validator。

```
memory_search(MCP) ─┐
FastAPI chat 路由 ──┼─→ request_handler(=guarded)
CLI/验证脚本 ───────┘        │
                             ├─ 1. 校验 memory_request（纯计算）
                             ├─ 2. raw_request_handler(apply_heat=False) → 纯读响应
                             ├─ 3. 校验 memory_response，不合法则重试 1 次（仍零副作用）
                             ├─ 4. B4 commit_retrieval() → 升温 + 审计，只提交一次
                             └─ 5. 两次都不合法 → 显式降级 degraded=True + 审计留痕
```

两条必须记住的坑：
* **重试绝不重复升温**：副作用的提交被推到格式校验**之后**（`apply_heat=False` 先生成），
  否则重试会把同一批记忆 `heat+1` 两次，且第二次排序已被第一次改动（响应不可复现）。
* **降级必须显式**：架构书 §5.8 的「连续失败 2 次路由 flash」需要云端 API key，
  用户已明确不提供（`config.yaml: llm.large.api_key` 仍是占位符），所以改为**本地降级**。
  降级响应带 `degraded=True` + `degraded_reason` 并写 B7 审计 —— 早期实现只返回一个空
  `empty=True`，调用方分不清「真的没有相关记忆」和「校验器坏了」，那是静默撒谎（违反硬规则 2）。
"""
import hashlib, json, os, re, uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------- 枚举 ----------
PROTOCOL_VERSION = "map/v0.1"
DEPTHS = ["L1", "L2", "L3"]          # L1 摘要 / L2 片段 / L3 原文
TASK_TYPES = [
    "研究", "决策", "验证", "规划",     # 4 类核心任务
    "闲聊", "求助", "其他",              # 3 类辅助
]


# ---------- Layer 预算（每深度的 token 默认值） ----------
LAYER_BUDGET = {"L1": 500, "L2": 1000, "L3": 3000}


# ---------- memory_request ----------

@dataclass
class MemoryRequest:
    """主 Agent → 记忆 Agent 的请求包

    filters 是嵌套 dict（对齐原始设计）：
        {"task_type": "编程", "core_entities": ["Python"], "time_range": "last_6_months"}

    旧平铺字段（filter_task_types 等）会自动合并进 filters 做向后兼容。
    """
    packet_type: str = "memory_request"
    protocol: str = PROTOCOL_VERSION
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    requester: str = ""
    query: str = ""
    depth: str = "L1"
    max_items: int = 5
    max_tokens: int = 500
    allow_followup: bool = True           # 允许后续 drilldown
    filters: Optional[dict] = None        # 原始设计的嵌套过滤结构
    # 硬规则 3
    decided_by: str = "user"
    trace_id: str = ""

    # ---- 向后兼容：旧平铺 filter 字段 ----
    filter_task_types: Optional[list] = None
    filter_time_buckets: Optional[list] = None
    filter_layer: Optional[str] = None
    filter_core_entity_any: Optional[list] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRequest":
        # 兼容：旧平铺 filter 字段 → 新 filters dict
        if "filters" not in d or d["filters"] is None:
            old_filters = {}
            if d.get("filter_task_types"):
                old_filters["task_type"] = d["filter_task_types"][0] if len(d["filter_task_types"]) == 1 else d["filter_task_types"]
            if d.get("filter_core_entity_any"):
                old_filters["core_entities"] = d["filter_core_entity_any"]
            if d.get("filter_time_buckets"):
                old_filters["time_range"] = d["filter_time_buckets"]
            if d.get("filter_layer"):
                old_filters["layer"] = d["filter_layer"]
            if old_filters:
                d = {**d, "filters": old_filters}
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})


# ---------- memory_response ----------

@dataclass
class MemoryItem:
    """memory_response.items 数组元素"""
    memory_id: str
    summary: str = ""
    header: dict = field(default_factory=dict)   # task_type/layer/core_entities 等
    confidence: float = 1.0                      # 0~1
    has_deeper: bool = False                     # 是否可 drilldown 到 L2/L3


@dataclass
class MemoryResponse:
    """记忆 Agent → 主 Agent 的响应包（对齐原始设计）"""
    packet_type: str = "memory_response"
    protocol: str = PROTOCOL_VERSION
    request_id: str = ""                         # 对应 MemoryRequest.request_id
    depth: str = "L1"
    items: list = field(default_factory=list)    # list[MemoryItem]
    # 过滤效果（让主 Agent 感知"从多少条里筛出来的"）
    total_candidates: int = 0
    filtered_from: int = 0
    truncated: bool = False
    followup_hint: str = ""                     # 原始设计兜底方案 A
    # 三条硬规则落地
    needs_clarification: bool = False            # 硬规则 1
    clarification_question: str = ""
    empty: bool = False                          # 硬规则 2
    # 校验器降级（替代架构书 §5.8 的「路由 flash」，云端 key 未提供）
    #   degraded=True 表示「这次不是没有记忆，而是协议层没答上」——
    #   必须和 empty 同时为 True 也不能混淆，所以单独给两个字段。
    degraded: bool = False
    degraded_reason: str = ""
    # 预算与耗时
    tokens_used: int = 0
    trace_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [asdict(i) if isinstance(i, MemoryItem) else i
                      for i in self.items]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryResponse":
        items = [MemoryItem(**i) if isinstance(i, dict) else i
                 for i in d.get("items", [])]
        return cls(**{k: v for k, v in d.items()
                       if k in cls.__annotations__ and k != "items"},
                   items=items)


# ---------- memory_drilldown ----------

@dataclass
class MemoryDrilldown:
    """深挖请求：L1 → L2/L3"""
    packet_type: str = "memory_drilldown"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    memory_id: str = ""
    depth: str = "L2"                            # L2/L3
    max_tokens: int = 1000
    decided_by: str = "user"
    trace_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ---------- Validator ----------

class MAPValidator:
    """三包校验器。P1-4 规格。

    校验项：
      1. packet_type 必须匹配包类型
      2. request_id 非空
      3. depth ∈ DEPTHS
      4. task_type（如果出现）∈ TASK_TYPES
      5. decided_by == "user"（硬规则 3）
      6. MemoryResponse.clarification_question 非空 → needs_clarification 必须为 True
      7. MemoryResponse.items 空 → empty 必须为 True（硬规则 2）
      8. 字段类型校验
    """

    VALID_PACKETS = {"memory_request", "memory_response", "memory_drilldown"}

    def validate(self, packet_type: str, payload: dict) -> tuple[bool, str]:
        """校验三包 payload。

        Returns: (ok, error_msg)
        """
        if packet_type not in self.VALID_PACKETS:
            return (False, f"未知 packet_type: {packet_type}")

        # 通用字段
        if not payload.get("request_id"):
            return (False, "request_id 必须非空")
        if payload.get("decided_by") and payload["decided_by"] != "user":
            return (False, "硬规则 3：decided_by 必须为 'user'")

        if packet_type == "memory_request":
            return self._validate_request(payload)
        elif packet_type == "memory_response":
            return self._validate_response(payload)
        else:  # drilldown
            return self._validate_drilldown(payload)

    def _validate_request(self, p: dict) -> tuple[bool, str]:
        if p.get("depth") and p["depth"] not in DEPTHS:
            return (False, f"depth 必须是 {DEPTHS} 之一")
        if p.get("query") is not None and not isinstance(p["query"], str):
            return (False, "query 必须是字符串")
        return (True, "")

    def _validate_response(self, p: dict) -> tuple[bool, str]:
        items = p.get("items", [])
        # items 空时必须声明 empty=True 或 needs_clarification=True（至少一个）
        if not items and not p.get("empty") and not p.get("needs_clarification"):
            return (False, "硬规则 2：items 空时 empty 或 needs_clarification 至少一个为 True")
        if p.get("clarification_question") and not p.get("needs_clarification"):
            return (False, "clarification_question 非空时 needs_clarification 必须为 True")
        for item in items:
            if "memory_id" not in item:
                return (False, "items[*] 缺少 memory_id")
            conf = item.get("confidence", 1.0)
            if not (0.0 <= conf <= 1.0):
                return (False, "items[*].confidence 必须 ∈ [0,1]")
        return (True, "")

    def _validate_drilldown(self, p: dict) -> tuple[bool, str]:
        if p.get("depth") not in ("L2", "L3"):
            return (False, "drilldown depth 必须是 L2 或 L3")
        if not p.get("memory_id"):
            return (False, "memory_id 必须非空")
        return (True, "")

    def check_and_retry(self, packet_type: str, payload: dict,
                        handler=None) -> dict:
        """校验 + 自动重试 1 次 + 连续失败则**显式降级**（对齐架构书 §5.8）。

        Args:
            handler: callable(payload_dict) → response_dict。**必须是无副作用的**：
                重试会再次调用它，任何写库动作（升温 / 审计）都得由调用方在
                校验通过之后单独提交一次，否则重试会重复升温。
                缺省 = `_raw_payload_handler`（内部走 `apply_heat=False` 的纯读检索）。

        Returns: 合法的 memory_response dict；两次都不合法时返回
            `degraded=True` + `degraded_reason` 的降级响应（不再是「静默空响应」）。
        """
        if handler is None:
            handler = _raw_payload_handler

        # 1) 请求包先校验：纯计算，失败时不产生任何副作用
        ok, err = self.validate(packet_type, payload)
        if not ok:
            return self._degrade_response(payload, f"request 校验失败: {err}", "request")

        # 2) 首次 + 自动重试 1 次（合计 2 次，对应「连续失败 2 次」）
        last_error = ""
        for attempt in (1, 2):
            try:
                result = handler(payload)
            except Exception as error:  # noqa: BLE001 - handler 异常也走降级，不往上抛
                last_error = f"handler 第 {attempt} 次抛异常: {type(error).__name__}: {error}"
                continue
            ok2, err2 = self.validate("memory_response", result)
            if ok2:
                return result
            last_error = f"response 校验失败（第 {attempt} 次）: {err2}"
        return self._degrade_response(payload, last_error, "response")

    def _degrade_response(self, payload: dict, reason: str, stage: str) -> dict:
        """本地降级响应 —— 替代架构书 §5.8 的「连续失败 2 次路由 flash」。

        「路由 flash」原意是把兜不住的请求转给云端 flash 模型。用户已明确不提供
        云端 API key（`config.yaml: llm.large.api_key` 仍是占位符），所以这里**不接
        云端**，改为本地降级，但降级必须**可见**：

        * `degraded=True` + `degraded_reason` 说清是哪个阶段、什么原因失败的；
        * `followup_hint` 给主 Agent 一句可执行的话（重试 / 改写 query）；
        * B7 审计留痕 —— 「谁在什么时候没答上」也是证据。

        硬规则 2（不编造，空结果诚实返回）反过来要求：**不能**把一个坏掉的校验器
        伪装成「真的没有相关记忆」。早期的 `_flash_response` 只返回空的 `empty=True`，
        调用方无法区分这两种情况，等于静默撒谎。
        """
        mode = _flash_mode()
        resp = MemoryResponse(
            request_id=payload.get("request_id", ""),
            depth=payload.get("depth") if payload.get("depth") in DEPTHS else "L1",
            items=[],
            empty=True,
            needs_clarification=False,
            followup_hint=(
                f"B6 协议校验在「{stage}」阶段连续失败，已按 {mode} 本地降级"
                f"（未接云端 flash）：这次不是「没有相关记忆」，请重试或改写 query。"
            ),
            degraded=True,
            degraded_reason=f"[{stage}] {reason}",
            tokens_used=0,
            trace_id=payload.get("trace_id", ""),
        )
        print(f"[B6] MAP 校验降级（{mode}）: [{stage}] {reason}")
        _audit_degraded(payload, resp.degraded_reason,
                        trace_id=payload.get("trace_id", ""))
        return resp.to_dict()


def _flash_mode() -> str:
    """读 `config.b6_map_protocol.flash_routing`，缺省 `local_degrade`。

    只认两种值：`local_degrade`（默认，本地降级）和 `disabled`（同样是本地降级，
    只是语义上说「这条兜底路径关掉了」）。**没有云端实现** —— 见 `_degrade_response`
    的说明，用户不提供云端 key，所以不做假装调用的死代码。
    """
    try:
        from services.shared.config.loader import get_config
        cfg = (get_config() or {}).get("b6_map_protocol", {}) or {}
        return str(cfg.get("flash_routing", "local_degrade"))
    except Exception:  # noqa: BLE001 - 配置读不到不该影响校验
        return "local_degrade"


def _audit_degraded(payload: dict, reason: str, trace_id: str = "") -> None:
    """B7 审计：降级留痕（失败绝不影响返回）。"""
    try:
        from services.B7_audit import AuditLogger
        AuditLogger().log(
            "search",   # 动作枚举里没有 degraded；归到 search（一次没答上的检索）
            {
                "kind": "b6_degraded",
                "reason": reason,
                "query": payload.get("query", ""),
                "depth": payload.get("depth", ""),
            },
            json.dumps([], ensure_ascii=False),
            request_id=payload.get("request_id", ""),
            trace_id=trace_id,
            actor="system",
        )
    except Exception as error:  # noqa: BLE001 - 审计失败不影响协议返回
        print(f"[B7] 降级审计写入失败: {type(error).__name__}: {error}")


# ---------- 硬规则 1：指代不明检测 ----------

# 指代/指示代词。注意**不要**把「这个/那个」当成一票否决：中文里
# 「这个 bug 怎么修」是完整 query，不该被反问。判定见 `detect_ambiguity()`。
PRONOUNS = ["那个人", "这件事", "那个", "这个", "他", "她", "它"]

# 去掉指代词与语气词之后，剩下的「实义内容」少于这个字数才算指代不明。
AMBIGUITY_RESIDUAL_MIN = 5

# 语气词/虚词：它们不承载指代对象，统计残差时要剔掉
_FILLER_RE = re.compile(r"[\s，。、？！,.?!的呢吗吧啊呀嘛啦哦嗯了是就都还也个]")


def detect_ambiguity(query: str) -> tuple[bool, str]:
    """硬规则 1「不猜指代」的落地判定。

    ⚠️⚠️ **它现在不是闸门**（2026-10-01 实测结论，V1.3.4 步骤 3）
    ------------------------------------------------------------
    实测（LIS 补充文档 §2.2，两侧冷库全部真实用户消息 **509 条**）：
    **触发 2.2%，11 条里 0 条真的指代不明** —— 它实际判的是「**query 太短**」，
    命中的全是「继续」「可以」「你好」「好的」这类**正常对话**。
    把它接到主链路 = 把 2.2% 的正常对话打成反问，**纯误伤**。

    而且它哪怕改对了也**不必要**：反问的真正判据是
    「**模型读了上下文之后，能不能看出用户在问什么**」，而那只能由模型自判
    （实测：检索侧信号与 CE 分数都分不开，见 `PLAN-V1.3.4` §3.0）。
    现已改为由**提示词**让模型自己判情形 C，判词与判定都在
    `services/B6_map_protocol/clarify.py`（产品与评测**同一份**）。

    所以本函数**保留仅供统计**（现在仍是 0% 真阳性），
    **不要**再把它的返回值接进任何动作。要动这条策略，先改 `clarify.py` 的指令并重测。

    Returns: (needs_clarification, reason)

    判定分两层：
      1. query 去掉空白后不足 3 字（如「那个」「它呢」）→ 必定反问；
      2. 含指代词，且**去掉指代词/语气词后剩下的实义内容**不足以定位对象
         （如「那个东西呢」→ 残差「东西」2 字）→ 反问。

    为什么不用原来的 `any(p in query)` 一票否决：那样「我们这个项目的下一步」
    「这个 bug 怎么修」这类完整 query 全会被反问 —— 硬规则 1 要求「不猜」，
    不是「见代词就装傻」，过度反问会把可用检索也一起废掉。
    """
    q = (query or "").strip()
    if len(q) < 3:
        return True, f"query 过短（{len(q)} 字），无法确定指代对象"

    hits = [p for p in PRONOUNS if p in q]
    if not hits:
        return False, ""

    residual = q
    for p in sorted(hits, key=len, reverse=True):     # 长词先替，避免「那个人」残留「人」
        residual = residual.replace(p, "")
    residual = _FILLER_RE.sub("", residual)
    if len(residual) < AMBIGUITY_RESIDUAL_MIN:
        return True, (f"指代词 {hits} 的指代对象不明"
                      f"（去掉代词后只剩「{residual}」）")
    return False, ""


# ---------- 调用 B4 检索的 handler ----------

def _join_hint(*parts: str) -> str:
    """把多段 followup_hint 拼成一句人话。

    实测踩到的坑：直接拿空格拼会让 hint 变成一串连读的句子
    （`…memory_id=xxx 指代词 ['那个'] 的指代对象不明…`），
    主 Agent 读起来分不清哪句是哪句。用「；」分隔并丢掉空段。
    """
    return '；'.join(p.strip() for p in parts if p and p.strip())


def _parse_filters(req: MemoryRequest) -> dict:
    """把 MemoryRequest 的 filters（新 dict / 旧平铺字段）统一成 B4 的入参。

    抽出来是为了让检索与审计用**同一份**过滤条件 —— 否则审计里记的 filters
    和实际生效的可能不一致（B7 的证据链就假了）。
    """
    f = req.filters or {}
    task_types = f.get("task_type") or req.filter_task_types
    if isinstance(task_types, str):
        task_types = [task_types]
    return {
        "task_types": task_types,
        "time_buckets": f.get("time_range") or req.filter_time_buckets,
        "layer": f.get("layer") or req.filter_layer,
        "core_entity_any": f.get("core_entities") or req.filter_core_entity_any,
    }


def _retrieve_and_build(req: MemoryRequest, apply_heat: bool = True
                        ) -> tuple[dict, MemoryResponse]:
    """检索 + 组装响应。返回 (B4 原始 result, MemoryResponse)。

    返回原始 result 是因为 `apply_heat=False` 时副作用还没提交，
    调用方（`guarded_request_handler`）需要它去调 B4 `commit_retrieval()`。
    """
    from services.B4_dual_gate_retrieval import retrieve

    filters = _parse_filters(req)
    budget = LAYER_BUDGET.get(req.depth, LAYER_BUDGET["L1"])
    effective_max = min(req.max_tokens or 9999, budget)
    # 链路 id：请求包带了就沿用（这样 MCP 响应里的 trace_id 与 audit_log 里的
    # 证据是同一个 id），没带才自己生成 —— 不然响应 trace_id="" 而审计里另有一个 id，
    # 拿着响应根本查不到自己的证据链。
    trace_id = req.trace_id or uuid.uuid4().hex[:16]

    # B4 双阶段检索（Phase1 WHERE → Phase2 余弦 + heat/count 精排 → 预算裁剪）
    result = retrieve(
        query_text=req.query,
        task_types=filters["task_types"],
        time_buckets=filters["time_buckets"],
        layer=filters["layer"],
        core_entity_any=filters["core_entity_any"],
        max_items=req.max_items,
        max_tokens=effective_max,
        apply_heat=apply_heat,
        trace_id=trace_id,
    )

    items = []
    for r in result["items"]:
        # confidence 必须落在 [0,1]（MAPValidator 会查），所以**不能**直接塞
        # 精排用的复合分数 `_score = sim + 0.3·heat + 0.2·count` —— heat/count
        # 一涨它就 >1（实测 heat=1,count=1,sim=0.7 → 1.2），validator 会把
        # 整个响应判为非法 → 降级成空响应。语义上也该用相似度：confidence
        # 是「这条记忆和问题的匹配把握」，不是「热度加成后的排名分」。
        raw_conf = r.get("_sim", r.get("_score", r.get("confidence", 1.0)))
        try:
            raw_conf = float(raw_conf)
        except (TypeError, ValueError):
            raw_conf = 1.0
        items.append(MemoryItem(
            memory_id=r["memory_id"],
            summary=r.get("summary", ""),
            header={
                "task_type": r.get("task_type"),
                "layer": r.get("layer"),
                "core_entities": r.get("core_entities", []),
                "heat": r.get("heat", 0),
                "count": r.get("count", 0),
                "score": round(float(r.get("_score", 0.0) or 0.0), 3),
                # ⚠️ 2026-09-26 加：**日期必须出到 MCP 面上**。
                #
                # B4 的条目里一直有 `source_date`（Phase1 是 `SELECT *`），但 MAP 响应的
                # header 此前只挑了 task_type/layer/core_entities/heat/count/score，
                # 日期**在这一层被丢掉了**。后果是调用方（主 Agent）拿到记忆后
                # 无法做任何时间推断：原文说 "yesterday"/"last week"，
                # 没有会话日期就永远换算不成绝对日期。
                #
                # 实测代价（LoCoMo conv-42 的 40 道时间推理题）：
                # 上下文不带日期 → cat2 F1 = 0.220；带上日期 → **0.353（+13.3 点）**，
                # 而这种题在官方数据里就是问 "When did X happen"。
                # 典型失败案例：`GT=21 August, 2022` 而模型答 `Yesterday` —— 它没错，是上下文没给日期。
                "source_date": r.get("source_date") or "",
                "time_bucket": r.get("time_bucket") or "",
            },
            confidence=round(max(0.0, min(1.0, raw_conf)), 3),
            has_deeper=bool(r.get("content_path") and req.allow_followup),
        ))

    empty = len(items) == 0

    # 指代不明检测（硬规则 1）
    needs_clarification, clarify_reason = detect_ambiguity(req.query)

    # followup_hint（原始设计兜底方案 A）
    # 有 deeper 可挖时引导主 Agent 深挖
    followup_hint = ""
    has_any_deeper = any(it.has_deeper for it in items)
    if has_any_deeper:
        ids = [it.memory_id for it in items if it.has_deeper][:2]
        # 用逗号分隔的可读字符串，避免 Python list repr 困惑 Agent
        ids_str = ", ".join(ids)
        followup_hint = (
            f"如需细节，可发 memory_drilldown 请求："
            f"depth=L2 或 L3 + memory_id={ids_str}"
        )
    if empty and not needs_clarification:
        # 硬规则 2：空结果诚实返回，并且说清是「筛完真的没有」而不是「没查」
        followup_hint = _join_hint(followup_hint, (
            f"本次未命中任何记忆（Phase1 候选 {result.get('total_filtered', 0)} 条，"
            f"Phase2 精排 {result.get('total_ranked', 0)} 条），未做任何补全。"
        ))
    if needs_clarification:
        followup_hint = _join_hint(followup_hint, clarify_reason)

    response = MemoryResponse(
        request_id=req.request_id,
        depth=req.depth,
        items=items,
        total_candidates=result.get("total_ranked", len(items)),
        filtered_from=result.get("total_filtered", 0),
        truncated=result.get("truncated", False),
        followup_hint=followup_hint,
        empty=empty,
        needs_clarification=needs_clarification,
        clarification_question=(
            "你指的是哪件事？能多给一些上下文吗？"
            if needs_clarification else ""
        ),
        tokens_used=result.get("tokens_used", 0),
        trace_id=trace_id,
    )
    return result, response


def raw_request_handler(req: MemoryRequest, apply_heat: bool = True) -> MemoryResponse:
    """**不带校验**的标准 handler：内存请求 → B4 检索 → MemoryResponse。

    `apply_heat=False` 是 B4 `retrieve()` 的既有语义：只读探查，不升温、不写审计。
    `guarded_request_handler()` 靠它先出一份零副作用响应去校验格式。
    一般调用方请用 `request_handler()`（带 validator）。
    """
    return _retrieve_and_build(req, apply_heat=apply_heat)[1]


def _raw_payload_handler(payload: dict) -> dict:
    """给 `MAPValidator.check_and_retry` 用的缺省 handler，入参是请求包的 dict。

    **刻意 `apply_heat=False`**：它是重试路径的生成器，必须无副作用。
    """
    req = MemoryRequest.from_dict(payload)
    return raw_request_handler(req, apply_heat=False).to_dict()


def _sync_heat_into_items(response: MemoryResponse, result: dict) -> None:
    """把提交后的 heat/count 同步回响应条目。

    响应是先于副作用生成的（`apply_heat=False`），所以 `header.heat` 是命中前的旧值。
    提交完升温再同步一次，MCP 返回给主 Agent 的热度才是真的。
    """
    by_id = {it.get("memory_id"): it for it in (result.get("items") or [])}
    for item in response.items:
        src = by_id.get(item.memory_id)
        if not src:
            continue
        item.header["heat"] = src.get("heat", item.header.get("heat"))
        item.header["count"] = src.get("count", item.header.get("count"))


def guarded_request_handler(req: MemoryRequest, apply_heat: bool = True) -> MemoryResponse:
    """**带 B6 validator** 的 handler —— 全链路唯一入口。

    顺序（这个顺序就是「重试不重复升温」的全部秘密）：

      1. 校验 memory_request（纯计算；不合法直接降级，一次副作用都不产生）
      2. `apply_heat=False` 生成响应（纯读）→ 校验 memory_response
      3. 不合法 → 再生成一次（仍 `apply_heat=False`，重试**不会**重复升温）
      4. 校验通过后才调 B4 `commit_retrieval()` 提交**一次**升温 + 审计
      5. 两次都不合法 → 显式降级响应（`degraded=True` + 原因 + 审计留痕）

    为什么接在 service 层而不是 MCP 工具层：与 B7 同理 —— MCP / FastAPI / CLI
    所有调用者都汇合在 `request_handler`，接在这里才没有漏网的路径。
    （B7 当初唯一的审计调用就写在旧 FastAPI 路由里，导致 DSH 走的 MCP 路径完全没审计。）
    """
    validator = MAPValidator()
    # 先钉一个链路 id：请求包、响应包、B4 检索、B7 审计共用同一个 id。
    # 不这么做的话，MCP 返回的响应 trace_id 是空的，而降级审计里又有一个自己生成的 id，
    # 「拿着响应查证据链」这件事就断了。
    if not req.trace_id:
        req.trace_id = uuid.uuid4().hex[:16]
    payload = req.to_dict()
    picked: dict = {}

    def _generate(p: dict) -> dict:
        # 用请求包重建 req：校验器可能对 payload 做过规范化（filters 兼容等），
        # 以假乱真不如以 payload 为准。仍然 apply_heat=False → 零副作用。
        result, response = _retrieve_and_build(
            MemoryRequest.from_dict(p), apply_heat=False
        )
        picked["result"] = result
        return response.to_dict()

    out = validator.check_and_retry("memory_request", payload, _generate)
    response = MemoryResponse.from_dict(out)

    if response.degraded:
        return response       # 降级路径：不提交任何副作用

    result = picked.get("result")
    if apply_heat and result is not None:
        try:
            from services.B4_dual_gate_retrieval import commit_retrieval
            commit_retrieval(result, query_text=req.query,
                             filters=_parse_filters(req))
            _sync_heat_into_items(response, result)
        except Exception as error:  # noqa: BLE001 - 反馈信号失败不影响检索
            print(f"[B6] 升温/审计提交失败: {type(error).__name__}: {error}")
    return response


def request_handler(req: MemoryRequest, apply_heat: bool = True) -> MemoryResponse:
    """标准入口：接收 MemoryRequest → 校验 → B4 检索 → 返回 MemoryResponse。

    对齐原始设计：默认 L1 浅层，followup_hint 引导深挖，预算硬限制。
    2026-09-22 起它**自带 B6 validator**（原先 `MAPValidator` 从未被调用过），
    见 `guarded_request_handler` 的顺序说明。
    """
    return guarded_request_handler(req, apply_heat=apply_heat)


# ---------- memory_drilldown 落地：L1 → L2/L3 取原文 ----------

_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def _locate_jsonl_entry(path: str, memory_id: str):
    """在「整天冷库 JSONL」里按 memory_id 反查那一行。

    ⚠️ 2026-09-30：实现已**移到 B3 `cold_locate`**（唯一一份）—— 路4 的原文索引
    也要按同一条规则去冷库取原文，规则留在两处迟早会不一致。
    这里保留函数名与返回值形状（`(entry|None, 总行数, 失败原因)`），
    调用方一行都不用改。B6 → B3 本来就是允许的依赖方向。
    """
    from services.B3_header_engine.cold_locate import locate_jsonl_entry
    return locate_jsonl_entry(path, memory_id)


def hashlib_md5_12(raw: str) -> str:
    """与 B3 `_memory_id` 完全一致的 id 算法（md5 前 12 位）。

    ⚠️ 2026-09-30：实现已**收敛**到 `cold_locate.hashlib_md5_12`（唯一一份）。
    """
    from services.B3_header_engine.cold_locate import hashlib_md5_12 as _impl
    return _impl(raw)


def read_drilldown(entry: dict, depth: str = 'L2', max_tokens: int = 1000) -> dict:
    """按 MAP `memory_drilldown` 的语义取 L2 片段 / L3 原文。

    为什么需要它：`memories.content_path` 指向的是**整天**冷库 JSONL
    （一天一个文件），而老代码 `json.load(f)` 把 JSONL 当单个 JSON 读，
    必然抛 `Extra data: line 2 column 1` —— 也就是说 memory_drilldown 这个包
    **从来没有返回过 L2/L3 内容**，响应里 `has_deeper: true` 是个谎。

    三种情况都如实说清（硬规则 2）：
      * `content_path` 为空（温提炼索引的行本就不存原文）→ 只能给 L1 摘要；
      * 精确命中那一行 → 返回它的原文/片段；
      * 命中不了 → `located=False` + 原因，**不返回内容** ——
        把整天的内容端出去会被主 Agent 当成「这条记忆的原文」，那是编造。

    Returns: dict，含 summary / layer / located / source_path，
        L2 另带 snippet，L3 另带 content（命中行的完整字段）。
    """
    summary = (entry or {}).get('summary', '') or ''
    path = (entry or {}).get('content_path') or ''
    memory_id = (entry or {}).get('memory_id', '') or ''

    if depth not in ('L2', 'L3'):
        return {'summary': summary, 'layer': 'L1'}
    if not path:
        return {'summary': summary, 'layer': 'L1', 'located': False,
                'note': '该条没有 content_path（温提炼索引的行不存原文），只能给 L1 摘要'}
    if not os.path.exists(path):
        return {'summary': summary, 'layer': depth, 'located': False,
                'source_path': path,
                'error': f'content_path 指向的文件不存在（V1 迁移遗留路径？）: {path}'}

    snippet_chars = max(200, int(max_tokens) * 4)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except Exception as error:  # noqa: BLE001
        return {'summary': summary, 'layer': depth, 'located': False,
                'source_path': path,
                'error': f'读不了 content_path: {type(error).__name__}: {error}'}

    # 情况 1：整份文件就是一个 JSON 文档（老约定：一条记忆一个 .json）
    #
    # ⚠️ 2026-09-23 修的假阳性：`json.loads` 对**只有 1 行的 JSONL** 也会成功，
    # 于是「整天文件」被当成「单文档」直接认成这条记忆的原文 ——
    # 实测 `2026-07-05.jsonl` 只有 1 行，`ba7ec31c432f`（原文行其实已经丢了）
    # 因此被误报 `located=true`，返回的是**别人的内容**。
    # 硬规则 2 要求不编造，所以这里必须**先验证身份**：
    # 只有当这个文档的 id 反算出来正好等于本条 memory_id 时才认。
    try:
        raw = json.loads(text)
        if isinstance(raw, dict):
            date_from_name = os.path.basename(path).replace('.jsonl', '')
            raw_id = str(raw.get('id') or raw.get('entry_id') or '')
            if raw_id and hashlib_md5_12(f'{date_from_name}#{raw_id}') == memory_id:
                body = raw.get('content') or raw.get('summary') or ''
                if depth == 'L2':
                    return {'summary': summary, 'layer': 'L2', 'located': True,
                            'source_path': path,
                            'matched_entry_id': raw_id, 'snippet': str(body)[:snippet_chars]}
                return {'summary': summary, 'layer': 'L3', 'located': True,
                        'source_path': path, 'matched_entry_id': raw_id, 'content': raw}
            # 单文档但**不是这条记忆**（典型：只有 1 行的 JSONL）→ 不返回内容，
            # 落到下面的 JSONL 扫描去如实判定
    except Exception:  # noqa: BLE001 - 不是单文档，按 JSONL 处理（这才是常态）
        pass

    # 情况 2/3：JSONL，逐行反查 memory_id
    hit, total, reason = _locate_jsonl_entry(path, memory_id)
    if hit is None:
        return {'summary': summary, 'layer': depth, 'located': False,
                'source_path': path, 'entries_in_file': total,
                'note': (f'{reason}。为免张冠李戴，**不返回任何内容** —— '
                         f'把整天内容当成这条记忆的原文等于编造')}

    if depth == 'L2':
        parts = [str(hit.get('user') or ''), str(hit.get('assistant') or '')]
        body = '\n'.join(p for p in parts if p) or str(hit.get('summary') or '')
        return {'summary': summary, 'layer': 'L2', 'located': True,
                'source_path': path, 'matched_entry_id': hit.get('id'),
                'snippet': body[:snippet_chars]}
    return {'summary': summary, 'layer': 'L3', 'located': True,
            'source_path': path, 'matched_entry_id': hit.get('id'), 'content': hit}


def drilldown_handler(packet: MemoryDrilldown) -> dict:
    """带校验的 `memory_drilldown` 执行入口（§5.8 要求三包都过 validator）。

    校验、取原文、B7 审计都在 service 层 —— MCP / FastAPI / CLI 谁调都一样，
    不会出现「某条上层路径偷偷绕过校验或漏审计」（B7 当初就吃过这个亏）。
    """
    from services.B3_header_engine import get_by_id

    validator = MAPValidator()
    payload = packet.to_dict()
    ok, err = validator.validate('memory_drilldown', payload)
    if not ok:
        reason = f'[drilldown] 包校验失败: {err}'
        print(f'[B6] MAP 校验降级（{_flash_mode()}）: {reason}')
        _audit_degraded({'query': f'drilldown:{packet.memory_id}',
                         'depth': packet.depth,
                         'request_id': packet.request_id}, reason, packet.trace_id)
        return {'memory_id': packet.memory_id, 'depth': packet.depth,
                'degraded': True, 'degraded_reason': reason, 'error': reason}

    entry = get_by_id(packet.memory_id)
    _audit_drilldown(packet, entry, found=entry is not None)
    if entry is None:
        return {'memory_id': packet.memory_id, 'depth': packet.depth,
                'degraded': False,
                'error': f'memory_id={packet.memory_id} 未找到'}

    content = read_drilldown(entry, packet.depth, packet.max_tokens)
    return {'memory_id': packet.memory_id, 'header': entry, **content}


def _audit_drilldown(packet: MemoryDrilldown, entry: Optional[dict], found: bool) -> None:
    """B7 审计：深挖是「谁看了哪条记忆」的关键证据（失败不影响取数）。"""
    try:
        from services.B7_audit import AuditLogger
        AuditLogger().log_drilldown(
            {'depth': packet.depth, 'found': found,
             'layer': (entry or {}).get('layer'),
             'content_path': (entry or {}).get('content_path') or None},
            packet.memory_id, trace_id=packet.trace_id, actor='agent',
        )
    except Exception as error:  # noqa: BLE001
        print(f'[B7] 深挖审计写入失败: {type(error).__name__}: {error}')
