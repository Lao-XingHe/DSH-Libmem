"""B5 热度状态机 — heat/count 公式 + 四象限 + 层迁移

规格：《书房 OS 迁入 dsh · 执行任务清单 P1-3》
heat(t) = Σ w × e^(−λ·Δt)
count 只增不减
四象限：高 count 高 heat = 核心优先 / 高 count 低 heat = count 保底 / 低 count 高 heat = 谨慎提升 / 低 count 低 heat = 基本不召回
层迁移：冷→温→热→温→冷，阈值取全库热度前 10% 相对值

== heat 与 count 的分工（2026-09-25 定稿）==
两个数字职责不同，别混用：

  heat  = 「被想起来」的热度。检索交付给调用方时 +1.0（`retrieval_hit`）。
          只表示这条被翻出来过，**不**说明它有没有被用上。
  count = 「被采用」的次数。**只有 `adopted` 动作会给它 +1**，
          也就是调用方显式上报「我真的用了这条」。

为什么拆开：原先 `retrieval_hit` 是 (1.0, 1)，count 等价于「被检索交付的次数」——
而检索发生在 B4、交付之后还有相关性过滤 / 用户确认过滤 / preview 预览三道关卡，
被拦下的记忆 count 也已经涨了。更关键的是 MCP 工具是无状态的：`memory_search`
把结果返回给主 Agent 之后就结束了，那几条有没有进主 Agent 的提示词，
进程里没有任何观测点，只能靠调用方显式回报。

实现：衰减函数 + 动作映射（7 种）+ 四象限判定 + 层迁移 + 全库批量衰减
"""
import math, time, datetime
from enum import Enum
from typing import Optional


class Quadrant(str, Enum):
    HIGH_HIGH = "核心优先"       # 高 count 高 heat
    HIGH_LOW  = "count 保底"     # 高 count 低 heat
    LOW_HIGH  = "谨慎提升"       # 低 count 高 heat
    LOW_LOW   = "基本不召回"     # 低 count 低 heat


# 升温动作 → (heat_delta, count_delta)
ACTIONS = {
    "retrieval_hit":   (1.0, 0),     # 检索命中：只加热度（被翻出来过），不动 count
    "adopted":         (5.0, 1),     # 被采用：**唯一**会给 count +1 的动作（调用方显式上报）
    "user_confirm":    (5.0, 0),     # 用户确认
    "success_decision": (10.0, 0),   # 成功决策
    "user_correct":    (-10.0, 0),   # 用户纠正
    "user_deny":       (-15.0, 0),   # 用户否定
    "forget":          None,          # 归零（强制遗忘）
}

# `apply_action_batch` 允许的动作白名单。
# 刻意只放 `adopted`：这个入口是给调用方「上报采用」用的，
# 放 `retrieval_hit` 进来会让上层误以为可以手动升温检索命中（那是 B4 的职责），
# 放 `forget` 进来则是给一个能清空 heat/count 的远程开关，没必要。
_BATCH_ALLOWED_ACTIONS = ("adopted",)


def _decay_one(old_heat: float, updated_at_ts: float, now_ts: float,
               half_life_days: float = 30.0) -> float:
    """单个条目的指数衰减

    λ = ln(2) / half_life_seconds
    heat(t_now) = old_heat × e^(−λ·Δt)
    """
    delta_seconds = max(0.0, now_ts - updated_at_ts)
    half_life_seconds = half_life_days * 86400
    lam = math.log(2) / half_life_seconds
    return old_heat * math.exp(-lam * delta_seconds)


class HeatStateMachine:
    """B5 主入口。参数来自 P1-3 规格。"""

    def __init__(self, half_life_days: float = 30.0,
                 fast_decay_days: float = 7.0,
                 hot_percentile: float = 0.9,
                 warm_percentile: float = 0.5):
        self.half_life_days = half_life_days       # 默认 30 天半衰期
        self.fast_decay_days = fast_decay_days     # 快速衰减 7 天档
        self.hot_percentile = hot_percentile       # 热层阈值（前 10%）
        self.warm_percentile = warm_percentile     # 温层阈值（前 50%）

    # ---------- 动作应用 ----------

    def apply_action(self, heat: float, count: int,
                     action: str) -> tuple[float, int, str]:
        """对单个条目应用升温/降温动作。

        Returns: (new_heat, new_count, note)
        """
        if action not in ACTIONS:
            return (heat, count, f"未知动作: {action}")

        delta = ACTIONS[action]
        if delta is None:
            # forget：归零
            return (0.0, 0, "强制归零")

        heat_delta, count_delta = delta
        new_heat = max(0.0, heat + heat_delta)
        new_count = max(0, count + count_delta)
        return (new_heat, new_count, f"heat+{heat_delta}, count+{count_delta}")

    # ---------- 全库批量衰减 ----------

    def decay_all(self, half_life: str = "default"):
        """对全库 memories 表做时间衰减。

        Args:
            half_life: "default" (30d) 或 "fast" (7d)

        Returns: 更新的行数
        """
        days = self.half_life_days if half_life == "default" else self.fast_decay_days
        now = time.time()

        def _decay(old_heat: float, updated_at_ts: float, _now: float) -> float:
            return _decay_one(old_heat, updated_at_ts, _now, half_life_days=days)

        from services.B3_header_engine import update_heat_all
        return update_heat_all(_decay)

    # ---------- 四象限 ----------

    def quadrant(self, heat: float, count: int,
                 median_count: int) -> Quadrant:
        """四象限判定。

        把 count 用全库中位数做切分（不用绝对值，适配不同数据量）。
        heat > 0 视为有热度（衰减到 0 视为无热度）。
        """
        count_high = count >= max(1, median_count)
        heat_high = heat > 0.1  # 衰减到几乎为 0 视为无

        if count_high and heat_high:
            return Quadrant.HIGH_HIGH
        elif count_high and not heat_high:
            return Quadrant.HIGH_LOW
        elif not count_high and heat_high:
            return Quadrant.LOW_HIGH
        else:
            return Quadrant.LOW_LOW

    # ---------- 层迁移 ----------

    def migrate_layers(self):
        """全库层迁移判定。

        规则（阈值用全库热度相对百分位，不写死绝对值）：
          heat > 0 且 heat >= hot_threshold  → hot
          heat > 0 且 heat >= warm_threshold → warm
          其余（含 heat == 0）               → cold

        ⚠️ `heat > 0` 这个前置条件是 2026-09-22 补的，理由见循环里的注释：
        没有它，一个「大多数记忆还没被检索过（heat=0）」的库会让所有行都变成
        温/热 —— 冷层恒空，迁移和「沉冷」都失去意义。

        注意：**层迁移不刷新 updated_at**（`update_layer(..., touch=False)`），
        因为 updated_at 是 B5 衰减的基准时间，刷它等于让那段已过去的时间永不被衰减。

        Returns: (updated_count, hot_threshold, warm_threshold)
        """
        from services.B3_header_engine import (
            get_heat_percentile, list_all_members, update_layer, count_total,
        )
        hot_threshold = get_heat_percentile(self.hot_percentile)
        warm_threshold = get_heat_percentile(self.warm_percentile)

        members = list_all_members()
        updated = 0
        for m in members:
            h = m.get("heat", 0.0)
            # ⚠️ `h > 0` 是 2026-09-22 补的：heat=0 的记忆**按定义就是冷**。
            # 老实现只有 `h >= threshold`，而全库过半记忆 heat=0 时
            # `get_heat_percentile(0.5)` 会返回 0.0 —— 于是 0 热度的行被判成 warm；
            # hot 阈值同样会塌到 0（实测本库 764 行里被标成 hot 的 734 行，
            # 就是这么来的：只有 29 行有热度，第 10% 分位取到的就是 0.0）。
            # 结果是「人人都是温/热、冷层恒为空」，层迁移失去意义，
            # 架构书 §5.7 的「冷→温→热→温→冷」里那个「冷」根本到不了。
            if h > 0 and h >= hot_threshold:
                new_layer = "hot"
            elif h > 0 and h >= warm_threshold:
                new_layer = "warm"
            else:
                new_layer = "cold"

            if new_layer != m.get("layer", "cold"):
                # touch=False：层迁移是派生字段的改动，**不能**刷新 updated_at。
                # updated_at 是衰减的基准时间；刷了它，这段已过去的时间就再也不会
                # 被衰减（记忆沉不下去）。详见 B3 `update_layer` 的说明。
                update_layer(m["memory_id"], new_layer, touch=False)
                updated += 1

        return (updated, hot_threshold, warm_threshold)

    # ---------- 工具 ----------

    def count_median(self) -> int:
        """全库 count 中位数"""
        from services.B3_header_engine import list_all_members
        members = list_all_members()
        counts = sorted(m.get("count", 0) for m in members)
        if not counts:
            return 0
        n = len(counts)
        if n % 2 == 1:
            return counts[n // 2]
        return (counts[n // 2 - 1] + counts[n // 2]) // 2


# ---------- 便捷函数 ----------

_default_sm = HeatStateMachine()


def apply_action(memory_id: str, action: str) -> Optional[dict]:
    """对一条记忆应用动作（自动读取当前值 + 写回）。

    Returns: 更新后的 {"memory_id", "heat", "count"}；memory_id 不存在时返回 None。
    """
    from services.B3_header_engine import get_conn, update_heat_count
    conn = get_conn()
    row = conn.execute(
        "SELECT heat, count FROM memories WHERE memory_id = ?",
        (memory_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    new_heat, new_count, _ = _default_sm.apply_action(row["heat"], row["count"], action)
    heat_delta = new_heat - row["heat"]
    count_delta = new_count - row["count"]
    update_heat_count(memory_id, heat_delta=heat_delta, count_delta=count_delta)
    return {"memory_id": memory_id, "heat": new_heat, "count": new_count}


def apply_action_batch(memory_ids: list[str], action: str = "adopted",
                       trace_id: str = "") -> dict:
    """对一批记忆应用同一个动作，并写一条 B7 审计。

    用途：调用方「显式上报采用」——把真的用进了回答的那几条 memory_id 回报上来，
    于是只有这几条 `count +1`（见模块开头「heat 与 count 的分工」）。

    为什么接在 service 层而不是 MCP 工具层：与 B4 `_audit_heat` / B6 同理 ——
    MCP / FastAPI / CLI 所有调用者都汇合在这里，接在这儿才不会多一条上层路径
    就多一处漏审计。

    Args:
        memory_ids: 上报的记忆 id（重复的会去重，保持首次出现顺序）
        action: 只允许 `_BATCH_ALLOWED_ACTIONS` 里的动作，默认 "adopted"
        trace_id: 链路 id，用于把这次上报与同一次对话/检索串起来（可空）

    Returns: {
        "action": str, "applied": int, "missing": [id...], "failed": int,
        "items": [{memory_id, heat, count}...],
        "error": str        # 仅在 action 不合法时出现，此时一条都不会写
    }
    """
    if action not in _BATCH_ALLOWED_ACTIONS:
        return {"action": action, "applied": 0, "missing": [], "failed": 0,
                "items": [],
                "error": f"不支持的动作 {action!r}；只允许 {list(_BATCH_ALLOWED_ACTIONS)}"}

    # 去重保序：同一批里重复上报同一条不该写两次（会多算一次采用）
    unique_ids = list(dict.fromkeys(mid for mid in (memory_ids or []) if mid))

    report = {"action": action, "applied": 0, "missing": [], "failed": 0, "items": []}
    for memory_id in unique_ids:
        try:
            row = apply_action(memory_id, action)
        except Exception as error:  # noqa: BLE001 - 反馈信号失败不能让上报整体炸掉
            report["failed"] += 1
            print(f"[B5] 上报失败 {memory_id}: {type(error).__name__}: {error}")
            continue
        if row is None:
            report["missing"].append(memory_id)
            continue
        report["applied"] += 1
        report["items"].append(row)

    _audit_adopted(report, trace_id)
    return report


def _audit_adopted(report: dict, trace_id: str) -> None:
    """B7 审计：记录一次「被采用」上报（含没找到的 id —— 静默吞掉等于丢了反馈信号）。"""
    if not (report["applied"] or report["missing"] or report["failed"]):
        return
    try:
        from services.B7_audit import AuditLogger
        import json as _json
        AuditLogger().log(
            "write",
            {
                "kind": "adopted_heat",
                "action": report["action"],
                "applied": report["applied"],
                "missing": report["missing"],
                "failed": report["failed"],
                "rule": f"ACTIONS[{report['action']!r}] = "
                        f"(heat+{ACTIONS[report['action']][0]}, count+{ACTIONS[report['action']][1]})",
            },
            _json.dumps([it["memory_id"] for it in report["items"]], ensure_ascii=False),
            trace_id=trace_id,
            actor="agent",
        )
    except Exception as error:  # noqa: BLE001 - 审计失败不影响上报本身
        print(f"[B7] 采用审计写入失败: {type(error).__name__}: {error}")
