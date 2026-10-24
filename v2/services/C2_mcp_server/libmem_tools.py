"""LibMem 工具实现的**单一真源**。

为什么要有这个文件
------------------
原来这 13 个工具的函数体只存在于 `mcp_server.py` 里，且是**闭包**（写在
`create_mcp_server()` 内部的 `@mcp.tool()` 装饰函数），外部拿不到。于是
「再挂一个入口」只有两条路：

  1. 把函数体抄一遍 —— 本仓明确反对：`memory_drilldown` 的注释里写着
     「免得将来多一条上层路径就多一处漏校验/漏审计」，抄一份等于把这个坑
     从 drilldown 扩散到全部 13 个工具；
  2. 先抽出来 —— 本文的做法。

现在两个入口共用这里的实现，**函数体只有一份**：

    services/C2_mcp_server/mcp_server.py     MCP（stdio/sse）入口，薄包装
    services/C2_mcp_server/libmem_bridge.py  HTTP 入口（DSH 插件 dsh-libmem 用）

因此：改行为只改这里；两个入口的**参数、返回值、错误语义天然一致**。

注意：本模块**不做任何 import 期重活**。所有 `from services...` 都写在函数体
内部（沿用原实现的延迟导入），这样 HTTP 桥启动是毫秒级，bge-m3 之类的重依赖
只在第一次真正调用时加载。
"""
import os
import sys

# 把 v2/ 与 services/ 加入 path（与 mcp_server.py 完全一致的推导方式）
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES = os.path.dirname(_HERE)
_V2 = os.path.dirname(_SERVICES)
for _p in (_V2, _SERVICES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 服务版本串。HTTP 桥的 `/health` 与 `/version` 都用它；改名时只改这一处。
SERVICE_VERSION = "LibMem/1.0（书房 OS V1.3.4 内核）"


# ---------------------------------------------------------------------------
# 记忆工具（B1~B7）
# ---------------------------------------------------------------------------
def memory_search(query: str, top_k: int = 5, depth: str = "L1",
                  recent_days: int = 0) -> dict:
    """昔涟记忆检索（V2 · B4 双阶段门控）—— 实现体，描述见 MCP 入口。

    ★ 2026-10-10（A2）：
      1. **回报问题维度**（A1/A2/B1…E1）：结果里多一个 `dimension` 字段，
         让调用方知道"这次是查找题还是汇总题"，从而自己决定要不要更多条 / 原文。
      2. **按维度去重**：`dedupe=true` 的维度（B1 汇总 / D 同一性）丢掉重复摘要 ——
         汇总题的"条数"必须是**不同条目**，否则多给的十几条会被同一天近似内容占满。

      **故意不做**的两件事（依据见 `dsh-libmem/README.md` §8 与 dimensions.yaml 文件头）：
      * 不按维度改交付条数 —— 「提高交付条数」在本仓已被确认测试**证伪**
        （override='B1'，100 题 ΔF1 +0.034、CI 含 0，代价是上下文翻倍 → 生成慢 2.3×）；
      * 不强制给原文 —— 调用方本来就能用 `depth=L2/L3` 按需取，强制给等于每次多吞几倍上下文。

    ★ 2026-10-12 加 `recent_days`（用户报的问题）：
      用户问"最近这几天我干了什么"，返回的却是几个月前的记忆。修法见
      `retrieval.py` 里 recency 那一段（Phase2 加权 + 重排入池保护 + 时间窗口检测）。
      `recent_days > 0` 时**完全换一条管道**：走 `recent_retrieve()` ——
      窗口内、按时间倒序、**不看相关性**。适用于"枚举这段时间发生了什么"的清单题。
    """
    if recent_days and int(recent_days) > 0:
        return memory_timeline(days=int(recent_days), limit=max(int(top_k), 20),
                               max_tokens=500 if depth == "L1" else 1500)

    from services.B6_map_protocol import MemoryRequest, request_handler
    req = MemoryRequest(
        query=query,
        depth=depth if depth in ("L1", "L2", "L3") else "L1",
        max_items=top_k,
        max_tokens=500 if depth == "L1" else 1500,
    )
    resp = request_handler(req)
    out = resp.to_dict()

    try:
        from services.shared import dimensions as DIM
        dim = DIM.classify_detail(query)
    except Exception:              # 维度判不出来不该让检索失败
        dim = None

    if dim:
        items = out.get("items") or []
        if dim.get("dedupe"):
            seen, kept = set(), []
            for it in items:
                sig = (it.get("summary") or "").strip()
                if sig and sig in seen:
                    continue
                if sig:
                    seen.add(sig)
                kept.append(it)
            out["items"] = kept
            if len(kept) != len(items):
                out["dedupe_dropped"] = len(items) - len(kept)
        out["dimension"] = {
            "child": dim.get("child"),
            "parent": dim.get("parent"),
            "name": DIM.describe(dim["child"]) if dim.get("child") else "",
            "topk": dim.get("topk"),
            "delivery": dim.get("delivery"),
            "dedupe": bool(dim.get("dedupe")),
            "note": "只作提示：条数与是否取原文仍由调用方决定（本仓实测『多给』不显著）",
        }
    return out


def memory_search_simple(query: str, top_k: int = 5) -> list:
    """简易检索（不带 MAP 协议信封）—— 实现体。"""
    from services.B4_dual_gate_retrieval import retrieve
    result = retrieve(query_text=query, max_items=top_k, max_tokens=2000)
    return [
        {
            "memory_id": it["memory_id"],
            "summary": it.get("summary", ""),
            "score": round(it.get("_score", 0), 3),
            "sim": round(it.get("_sim", 0), 3),
            "task_type": it.get("task_type"),
            "layer": it.get("layer"),
            "heat": it.get("heat", 0),
        }
        for it in result["items"]
    ]


def memory_drilldown(memory_id: str, depth: str = "L2", max_tokens: int = 1500) -> dict:
    """深挖单条记忆（L1 → L2 片段 或 L3 原文）—— 实现体。"""
    from services.B6_map_protocol import MemoryDrilldown, drilldown_handler

    if depth not in ("L2", "L3"):
        # L1 就是摘要本身，不需要深挖（MAP 的 drilldown 包只允许 L2/L3）
        from services.B3_header_engine import get_by_id
        entry = get_by_id(memory_id)
        if not entry:
            return {"memory_id": memory_id, "depth": depth,
                    "error": f"memory_id={memory_id} 未找到"}
        return {"memory_id": memory_id, "header": entry,
                "summary": entry.get("summary", ""), "layer": "L1",
                "located": False,
                "note": "depth=L1 只要摘要；要原文请用 L2/L3"}

    return drilldown_handler(MemoryDrilldown(
        memory_id=memory_id,
        depth=depth,
        max_tokens=max_tokens,
    ))


def memory_feedback(memory_ids: list) -> dict:
    """回报「这些记忆我真的用上了」—— 实现体。"""
    from services.B5_heat_state import apply_action_batch
    return apply_action_batch(memory_ids, action="adopted")


def memory_write(user_message: str, assistant_message: str) -> str:
    """写入冷记忆 + 触发温提炼（B1 → B3 写入链路）—— 实现体。"""
    from services.B1_cold_memory import record_conversation
    entry_id = record_conversation(user_message, assistant_message)
    # 异步触发提炼
    import threading

    def _t():
        from mcp_tools.memory import _trigger_auto_extract
        _trigger_auto_extract(app=None)

    threading.Thread(target=_t, daemon=True).start()
    return f"已落盘: {entry_id}"


def memory_extract() -> dict:
    """手动触发全量温提炼扫描 —— 实现体。"""
    from mcp_tools.memory import _run_extract_background, _extract_running
    import threading
    if _extract_running:
        return {"status": "running"}
    threading.Thread(target=_run_extract_background, args=(None,), daemon=True).start()
    return {"status": "started"}


def memory_timeline(days: int = 7, limit: int = 20, max_tokens: int = 1500,
                    layer: str = "", task_types: list = None) -> dict:
    """★ 2026-10-12：**按时间列清单**（"这几天我干了什么"）—— 实现体。

    与 `memory_recent` 的区别：这个是**交付形态**的（带预算裁剪、带窗口元信息、
    条数可以开到几十条），`memory_recent` 是聊天开场那一小段（8 条摘要）。
    两个都走 `retrieval.recent_retrieve()`，所以"窗口内 + 时间倒序"的语义只有一份。

    为什么单独一个工具（而不是给 `memory_search` 加权重就完事）：
      用户问的是**枚举**："这几天发生过什么"。相关性排序对这类问题结构上就是错的 ——
      库里几个月前有大量"做了什么"的记录，与问句相似度不低，只要还按相似度排，
      清单里就会混进老条目。所以这里**完全不看相关性**：窗口内 + 时间倒序 + 尽量列全。

    Args:
        days: 最近多少天内（按 `source_date`；0/负数 = 不限）。
        limit: 返回条数上限（之后还要过 `max_tokens` 预算）。
        layer: 只取某一层（`hot`/`warm`/`cold`）；空串 = 全部。
        task_types: 只要这些任务类型（`决策`/`研究`/`验证`…）。
    """
    from services.B4_dual_gate_retrieval import retrieval as B4
    import uuid as _uuid
    return B4.recent_retrieve(days=days, limit=limit, max_tokens=max_tokens,
                              layer=(layer or None), task_types=task_types,
                              apply_heat=False, trace_id=str(_uuid.uuid4()))


def memory_recent(days: int = 7, limit: int = 8) -> list:
    """**最近**的记忆（不按相关性，只按时间倒序）—— 实现体。

    ★ 2026-10-10 新增。为什么需要它：
      会话一旦变长，每一步模型调用都要背上几十万 token 的上下文（实测平均 42.9 万、
      峰值 67.6 万），而其中**真正新的内容不到 1%**。真正的解法是走"短会话 + 按需检索"，
      但短会话开局是"失忆"的 —— 这个函数就是补那一段：新会话开始时按时间拉一批最近
      的记忆，替掉"把整段历史一直背在上下文里"。

    与 `memory_search` 的分工：
      * `memory_search`  —— 按**相关性**找（"关于 X 我记得什么"）
      * `memory_recent`  —— 按**时间**找（"我最近在做什么"），不需要 query

    ⚠️ `limit` 现在放到 **200**（原 100）：`libmem_recent` 工具会拿它列整天的清单，
    窗口内条目常超过 100 条（实测 3 天窗口 = 127 条）。默认值仍是 8 不变，
    只影响显式要更多的调用方。

    Args:
        days: 只要最近多少天内产生的记忆（按 `source_date`；0 或负数 = 不限）
        limit: 返回条数上限

    Returns: `[{memory_id, summary, task_type, source_date, layer, heat}, ...]`，新的在前。
    """
    import datetime
    from services.B3_header_engine import phase1_filter

    rows = phase1_filter(limit=5000) or []
    cutoff = ""
    if days and days > 0:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    def date_of(r):
        return str(r.get("source_date") or "")

    kept = [r for r in rows if date_of(r) and (not cutoff or date_of(r) >= cutoff)]
    # 只按时间倒序：同一天内用 created_at 兜底，保证稳定
    kept.sort(key=lambda r: (date_of(r), str(r.get("created_at") or "")), reverse=True)
    out = []
    for r in kept[:max(1, int(limit))]:
        out.append({
            "memory_id": r.get("memory_id"),
            "summary": r.get("summary") or "",
            "task_type": r.get("task_type") or "",
            "source_date": date_of(r),
            "layer": r.get("layer") or "",
            "heat": r.get("heat", 0),
        })
    return out


def memory_stats() -> dict:
    """记忆统计（B3 SQLite 主索引 + B4 向量缓存）—— 实现体。"""
    from services.B3_header_engine import count_total, get_layer_distribution, phase1_filter
    from services.B4_dual_gate_retrieval import embed_cache, candidate_text
    total = count_total()
    layers = get_layer_distribution()
    cache = embed_cache.stats()
    cov = embed_cache.coverage([candidate_text(r) for r in phase1_filter(limit=100000)])
    return {
        "sqlite_total": total,
        "layer_distribution": layers,
        "embedding_model": "bge-m3",
        "protocol": "map/v0.1",
        "embed_cache": {
            "enabled": embed_cache.enabled(),
            "entries": cache.get("entries", 0),
            "candidates": cov["candidates"],
            "cached": cov["cached"],
            "missing": cov["missing"],
            "coverage": cov["ratio"],
            "size_mb": cache.get("size_mb", 0),
            "path": cache.get("path", ""),
        },
    }


# ---------------------------------------------------------------------------
# 整链体检（P2，2026-10-12）
# ---------------------------------------------------------------------------
def chain_check() -> dict:
    """整链体检：冷库 → 温层 → 索引 三层对账（**只读**）—— 实现体。

    ★ 2026-10-12 新增。为什么要有它：工位各自都活着，但此前**没有任何一处**
    能回答"数据有没有掉件、有没有重复"。唯一的发现方式是把它抓出来：
    2026-10-12 实测冷库 521 行里有 134 行重复写入、索引 818 行里有 90 行孤儿、
    14 组同摘要重复 —— 全是靠人肉数条数才发现的。

    它在 `services/B3_header_engine/check_chain.py`，这里只是转发
    （与其它工具同一条纪律：**实现只有一份**）。
    """
    from services.B3_header_engine.check_chain import check_chain as _impl
    return _impl()


# ---------------------------------------------------------------------------
# 知识库工具（C1）
# ---------------------------------------------------------------------------
def knowledge_search(query: str, top_k: int = 5, with_related: bool = False) -> list:
    """知识库全文检索 —— 实现体。"""
    from services.C1_knowledge import search_knowledge_index
    return search_knowledge_index(query, top_k, with_related=with_related)


def knowledge_reindex() -> dict:
    """重建知识库索引 —— 实现体。"""
    from services.C1_knowledge import reindex_knowledge
    return reindex_knowledge()


def knowledge_stats() -> dict:
    """知识库现状 —— 实现体。"""
    from services.C1_knowledge import knowledge_stats as _kb_stats
    return _kb_stats()


def knowledge_read(path: str, max_chars: int = 3000) -> str:
    """读取知识库文件内容 —— 实现体。"""
    from services.C1_knowledge import read_kb_file_content
    return read_kb_file_content(path, max_chars)


# ---------------------------------------------------------------------------
# 审计工具（B7）
# ---------------------------------------------------------------------------
def audit_recent(limit: int = 20) -> list:
    """最近审计日志（B7）—— 实现体。"""
    from services.B7_audit import get_recent_logs
    return get_recent_logs(limit)


def audit_verify(memory_id: str) -> dict:
    """验证单条记忆完整性 hash（B7）—— 实现体。"""
    from services.B7_audit import verify_integrity
    return verify_integrity(memory_id)


# ---------------------------------------------------------------------------
# 注册表：HTTP 桥按名字分发，也用于 `/tools` 自检与文档生成
# ---------------------------------------------------------------------------
TOOLS = {
    "memory_search": memory_search,
    "memory_search_simple": memory_search_simple,
    "memory_drilldown": memory_drilldown,
    "memory_feedback": memory_feedback,
    "memory_write": memory_write,
    "memory_extract": memory_extract,
    "memory_recent": memory_recent,
    # ★ 2026-10-12：按时间**列清单**（"这几天我干了什么"）。与 `memory_recent`
    #   的区别是交付形态：带预算裁剪、窗口元信息，条数可以开到几十条。
    "memory_timeline": memory_timeline,
    "memory_stats": memory_stats,
    # ★ 2026-10-12（P2）：整链体检 —— 三层对账，只读
    "chain_check": chain_check,
    "knowledge_search": knowledge_search,
    "knowledge_reindex": knowledge_reindex,
    "knowledge_stats": knowledge_stats,
    "knowledge_read": knowledge_read,
    "audit_recent": audit_recent,
    "audit_verify": audit_verify,
}


def call_tool(name: str, args: dict):
    """按名字调用工具。未知名字抛 `KeyError`（桥会翻成 404）。

    参数用 `**args` 展开而不是手工映射：签名不对时由 Python 自己报
    `TypeError: unexpected keyword argument`，比手写校验更准确。
    """
    fn = TOOLS[name]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise TypeError(f"args 必须是对象，收到 {type(args).__name__}")
    return fn(**args)
