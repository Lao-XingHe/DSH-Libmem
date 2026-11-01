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
import json
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
    # ★ 2026-11-01（L5，默认关）：规范控制 —— 查询改写。
    #   只在 `features.authority_rewrite` 打开时生效（默认 false，见 shared/features.py）。
    query_in, _aliases = _authority_expand(query)
    req = MemoryRequest(
        query=query_in,
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

    # ★ 2026-11-01：把 L1/L2 接到检索结果上（默认开，可关）。
    #   不改变排序、不改变条数 —— 只**附上**两样检索本身看不见的东西：
    #     ① 这条属于哪个主题、那个主题一共有多少条（覆盖面自检，L2）；
    #     ② 每条结果的引文邻居（它从哪来 / 谁接着它，L1）。
    #   任何异常都吞掉：这是旁路增强，坏了最多是少一段提示，绝不能让检索失败。
    _enrich_search(out, top_k)
    return out


# 覆盖面提示的上限：实体超过这么多条，说明它是「类目词」不是「主题」，
# 这时候提示「还有 N 条」只会变成噪音（例如「记忆」「用户」）。
_ENRICH_TOPIC_MAX = 150
# 至少要藏起来这么多条才值得一提（藏 1~2 条不算"你看到的只是冰山一角"）。
_ENRICH_MIN_HIDDEN = 3
# 主题总数缓存：同一场对话里反复问同一件事时，不要每次都去数一遍。
# ⚠️ 不能用 `write_epoch` 做键 —— 检索会升温写库，纪元每次都变，缓存永远不命中
#    （实测：加了缓存仍每次 +70ms，就是这个原因）。改用 TTL。
_TOPIC_CACHE: dict = {}
_TOPIC_TTL = 60.0


def _topic_total(entity: str) -> int:
    """某实体的馆藏总数（带 TTL 缓存）。取不到就返回 0，调用方按"不提示"处理。"""
    import time as _time
    now = _time.time()
    hit = _TOPIC_CACHE.get(entity)
    if hit and now - hit[1] < _TOPIC_TTL:
        return hit[0]
    from services.B8_catalog import collocate
    total = int(collocate(entity, limit=0).get("total") or 0)
    if len(_TOPIC_CACHE) > 256:          # 简单封顶，不做 LRU（条目很小）
        _TOPIC_CACHE.clear()
    _TOPIC_CACHE[entity] = (total, now)
    return total


def _enrich_search(out: dict, max_items: int) -> None:
    """给 `memory_search` 的结果补上「主题覆盖面」和「引文邻居」。

    为什么不改 `retrieve()` 而是加在工具层：检索是所有调用方（MCP/MAP/FastAPI/CLI）
    的汇合点，动它等于同时改所有契约；而这里只是**追加字段**，老调用方忽略即可。

    成本（实测）：实体倒排索引命中 <1ms + 引文批量查询 ~2ms ≈ **3ms**，
    相比 188ms 的检索可以忽略；关掉用 `enrich=False`（config `retrieval.enrich`）。
    """
    from collections import Counter

    items = out.get("items") or []
    if len(items) < 2:
        return
    try:
        from services.shared import features
        if not features.enabled("enrich"):
            return
    except Exception:  # noqa: BLE001 - 配置读不到就按默认开
        pass

    enr: dict = {}

    # ① 主题：结果里出现次数最多的实体（同一主题被召回多次 → 值得报总数）
    try:
        cnt = Counter()
        for it in items:
            hdr = it.get("header") or {}
            for e in dict.fromkeys(hdr.get("core_entities") or []):
                if isinstance(e, str) and 2 <= len(e) <= 30:
                    cnt[e] += 1
        if cnt and _ENRICH_TOPIC_MAX > 0:      # 0 = 关掉，连 collocate 都不调
            entity, n = cnt.most_common(1)[0]
            if n >= 2:
                total = _topic_total(entity)
                hidden = total - len(items)
                if 0 < total <= _ENRICH_TOPIC_MAX and hidden >= _ENRICH_MIN_HIDDEN:
                    enr["topic"] = {
                        "entity": entity,
                        "total": total,
                        "shown": len(items),
                        "hidden": hidden,
                        "note": f"「{entity}」在馆里共 {total} 条，这里只给了 {len(items)} 条；"
                                f"要全集用 libmem_collocate，要整份清单用 libmem_catalog。",
                    }
    except Exception:  # noqa: BLE001 - 覆盖面提示失败不影响检索
        pass

    # ② 引文：每条结果的上下游（只报 id，详细用 libmem_citations）
    try:
        from services.B8_catalog.citations import edges_for_many
        ids = [i.get("memory_id") for i in items if i.get("memory_id")]
        edges = edges_for_many(ids, limit=2)
        cit = {}
        for mid, e in edges.items():
            out_ids = [x["memory_id"] for x in e.get("out") or []]
            in_ids = [x["memory_id"] for x in e.get("in") or []]
            if out_ids or in_ids:
                cit[mid] = {"cites": out_ids, "cited_by": in_ids}
        if cit:
            enr["citations"] = cit
    except Exception:  # noqa: BLE001 - 旁路增强失败不影响检索
        pass

    if enr:
        out["enrichment"] = enr


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
# 馆藏层（P1 / P3，2026-10-24）：统一检索 + 按标目浏览
#
# 定位：Libmem 是「AI 馆藏」，记忆只是来源之一。这一组接口把三种能力收拢到一个入口：
#   search  一次查记忆 + 知识库，结果标来源（先并列，不做跨来源融合打分）
#   browse  按标目浏览（分类 / 年月 / 主题词）—— 图书馆的「目录浏览」
#   facets  标目分布（有哪些分类、哪些月份、哪些高频主题词）—— 先看地图再翻书
#
# 纪律：全部**只读**；失败返回带 error 字段的空结果，不抛。
# ---------------------------------------------------------------------------
def library(action: str = "facets", query: str = "", sources: str = "memory,knowledge",
            class_name: str = "", sub_class: str = "", year: int = 0, month: int = 0,
            term: str = "", top_k: int = 5, limit: int = 20,
            order_by: str = "time") -> dict:
    """馆藏统一入口：action = search / browse / facets。"""
    action = (action or "facets").strip().lower()
    if action == "search":
        return _library_search(query, top_k, sources)
    if action == "browse":
        # L6 的开关在这里生效：功能关掉时强制回默认排序（时间倒序）
        try:
            from services.shared import features
            if not features.enabled("subject_shelf"):
                order_by = "time"
        except Exception:  # noqa: BLE001
            order_by = "time"
        return _library_browse(class_name, year, month, term, limit, sub_class,
                               order_by=order_by)
    if action == "guide":
        return _library_guide()
    return _library_facets(limit)


def _library_search(query: str, top_k: int, sources: str) -> dict:
    if not (query or "").strip():
        return {"ok": False, "error": "search 需要 query"}
    want = {s.strip() for s in (sources or "").split(",") if s.strip()} or {"memory", "knowledge"}
    items, counts = [], {}
    if "memory" in want:
        try:
            from services.B4_dual_gate_retrieval import retrieve
            r = retrieve(query_text=query, max_items=top_k, max_tokens=2000)
            rows = r.get("items") or []
            items += [{"source": "memory", "id": it.get("memory_id"),
                       "title": it.get("summary"), "task_type": it.get("task_type"),
                       "layer": it.get("layer"),
                       "score": round(it.get("_score") or 0.0, 3)} for it in rows]
            counts["memory"] = len(rows)
        except Exception as error:  # noqa: BLE001
            counts["memory"] = 0
            items.append({"source": "memory", "error": type(error).__name__})
    if "knowledge" in want:
        try:
            from services.C1_knowledge import search_knowledge_index
            rows = search_knowledge_index(query, top_k) or []
            items += [dict({k: v for k, v in row.items() if k in ("path", "title", "score", "snippet", "text")},
                           source="knowledge") for row in rows if isinstance(row, dict)]
            counts["knowledge"] = len(rows)
        except Exception as error:  # noqa: BLE001
            counts["knowledge"] = 0
            items.append({"source": "knowledge", "error": type(error).__name__})
    return {"ok": True, "action": "search", "query": query,
            "counts": counts, "items": items}


def _library_browse(class_name: str, year: int, month: int, term: str, limit: int,
                    sub_class: str = "", order_by: str = "time") -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    where, params = [], []
    if class_name:
        where.append("d.class = ?"); params.append(class_name)
    if sub_class:
        where.append("d.sub_class = ?"); params.append(sub_class)
    if year:
        where.append("d.year = ?"); params.append(int(year))
    if month:
        where.append("d.month = ?"); params.append(int(month))
    if term:
        where.append("d.terms LIKE ?"); params.append('%"' + term + '"%')
    sql = ("SELECT i.item_id, i.summary, i.created_at, d.class, d.sub_class, d.terms, "
           "d.year, d.month, d.week, d.day, d.task_type, a.kind, a.uri "
           "FROM items i JOIN descriptions d ON d.item_id = i.item_id "
           "LEFT JOIN assets a ON a.asset_id = i.asset_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    # ★ 2026-11-01（L6）：主题排架 —— 只改**浏览顺序**，不动 `layer` 语义。
    #   书 p.45：「按分类排列的文献集合可以帮助用户在浏览中发现相关文献」。
    #   现在的默认仍是「按真实创建时间倒序」（最近的在前），
    #   `order_by="class"` 才切成"同类挨着"——即有意识地"去书架上逛"。
    #   这正是"可回滚"的地方：默认行为一个字节没变，只有显式要才变。
    use_class = (order_by or "time").lower() == "class"
    if use_class:
        sql += " ORDER BY d.class ASC, i.created_at DESC, i.item_id DESC LIMIT ?"
    else:
        sql += " ORDER BY i.created_at DESC, i.item_id DESC LIMIT ?"
    params.append(int(limit))
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            try:
                d["terms"] = json.loads(d.get("terms") or "[]")
            except Exception:
                d["terms"] = []
            rows.append(d)
        # L6：排架次序（公版 class 顺序）而不是类名字母序 —— 见 B8/shelf.py
        if use_class:
            try:
                from services.B8_catalog.shelf import rank as _shelf_rank
                rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
                rows.sort(key=lambda x: _shelf_rank(x.get("class") or ""))
            except Exception:  # noqa: BLE001 - 排架表坏了就保持 SQL 的字母序
                pass
    finally:
        conn.close()
    return {"ok": True, "action": "browse",
            "filters": {"class": class_name, "sub_class": sub_class, "year": year,
                        "month": month, "term": term},
            "count": len(rows), "items": rows}


def _library_facets(limit: int) -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        classes = [{"class": c, "count": n} for c, n in conn.execute(
            "SELECT class, count(*) FROM descriptions GROUP BY class ORDER BY count(*) DESC")]
        months = [{"year": y, "month": m, "count": n} for y, m, n in conn.execute(
            "SELECT year, month, count(*) FROM descriptions WHERE year > 0 "
            "GROUP BY year, month ORDER BY year DESC, month DESC LIMIT 24")]
        subs = [{"class": c, "sub_class": s or "未细分", "count": n} for c, s, n in conn.execute(
            "SELECT class, sub_class, count(*) FROM descriptions "
            "GROUP BY class, sub_class ORDER BY count(*) DESC")]
        terms = [{"term": t, "count": n} for t, n in conn.execute(
            "SELECT tm.canonical, count(*) FROM mem_terms mt JOIN terms tm ON tm.term_id = mt.term_id "
            "GROUP BY tm.canonical ORDER BY count(*) DESC, tm.canonical LIMIT ?", (int(limit),))]
        total = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "action": "facets", "items_total": total,
            "classes": classes, "subclasses": subs, "months": months, "top_terms": terms}


def _library_guide() -> dict:
    """馆藏分类「工作表格」—— 给刚装插件的人看的一张表：类目是什么、怎么维护。"""
    from services.B8_catalog import CLASS_RULES, SUB_CLASS_RULES
    lines = []
    lines.append("# 馆藏分类工作表格")
    lines.append("")
    lines.append("## 一、什么时候用")
    lines.append("")
    lines.append("| 时机 | 动作 | 命令/入口 |")
    lines.append("|---|---|---|")
    lines.append("| 刚装、库<200 条 | 只跑默认规则，不建二级 | `python bin/migrate-to-library.py --apply` |")
    lines.append("| 库到 200~1000 条 | 生成二级类目 | `python bin/build-subclasses.py --apply` |")
    lines.append("| 每半年 / 明显觉得分类别扭 | 复审（做梦模式：让 4B 提方案，人确认） | `python bin/dream-catalog.py` |")
    lines.append("| 行业词多、通用骨架覆盖不到 | 本地扩展词表 | `v2/class_lexicon.yaml`（复制 example） |")
    lines.append("")
    lines.append("## 二、一级类目（通用骨架，跨行业成立）")
    lines.append("")
    lines.append("| 一级类 | 说明 | 判定词（前几个） |")
    lines.append("|---|---|---|")
    for cls, words in CLASS_RULES.items():
        lines.append(f"| {cls} | — | {'、'.join(words[:6])} |")
    lines.append("| 其他 | 兜底；`其他`占比 >15% 说明词表覆盖不足 | — |")
    lines.append("")
    lines.append("## 三、二级类目（半通用：骨架 + 数据驱动兜底）")
    lines.append("")
    lines.append("| 一级类 | 二级类目 |")
    lines.append("|---|---|")
    for cls, subs in SUB_CLASS_RULES.items():
        lines.append(f"| {cls} | {'、'.join(subs.keys())} |")
    lines.append("")
    lines.append("- 新库：二级来自上面这套**通用骨架**（可读、能立刻用）；")
    lines.append("- 数据多了：骨架没覆盖的条目，用**自己馆藏里的高频术语**当小类名（私人化，自动长出来）；")
    lines.append("- 三级不做：二级已到可操作粒度，再往下用「主题词 + 时间」浏览代替（browse term=/year=/month=）。")
    lines.append("")
    lines.append("## 四、健康指标（每次复审看这四个数）")
    lines.append("")
    lines.append("| 指标 | 健康值 | 怎么查 |")
    lines.append("|---|---|---|")
    lines.append("| 未细分比例 | ≤15% | `libmem_library action=facets` |")
    lines.append("| 单一大类占比 | ≤60%（工作重心集中属正常） | 同上 |")
    lines.append("| 每类二级数 | 3~8 个 | 同上 |")
    lines.append("| 二级名是否可读 | 人一眼能懂 | 人工看一遍 |")
    lines.append("")
    lines.append("## 五、做梦模式（dream）")
    lines.append("")
    lines.append("让 4B 小模型**复审目录**：每类抽 8~10 条样本 + 现有二级名，让它提出")
    lines.append("「合并/改名/新增小类」的方案；**默认 dry-run**，人工确认后再写 `class_lexicon.yaml`。")
    lines.append("不做自动改库：分类是长期结构，模型只当参谋。")
    return {"ok": True, "action": "guide", "markdown": chr(10).join(lines)}


# ---------------------------------------------------------------------------
# P3（2026-10-24）：导出 / 备份 —— 让 bin 下验证过的脚本变成可调用工具
#
# 为什么不重写：bin/libmem-export.py 与 bin/libmem-backup.py 已经跑通并有校验，
# 重写一份只会多一处会漂移的真相源。这里用 importlib 加载同一份脚本，直接调它的函数。
# ---------------------------------------------------------------------------
def _load_bin_script(filename: str):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(root, "bin", filename)
    mod_name = "libmem_bin_" + filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def library_export(action: str = "export", format: str = "md", days: int = 0,
                   layer: str = "", with_raw: bool = False) -> dict:
    """导出 / 备份（action=export｜backup）。

    export：把记忆导出成 Markdown（按日期分文件）或 JSONL；输出到 data/exports/。
    backup：全量备份 zip（冷库+温层+header.db 一致性快照+配置脱敏），输出到 backups/。
    """
    action = (action or "export").strip().lower()
    if action == "backup":
        try:
            mod = _load_bin_script("libmem-backup.py")
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            out_dir = os.path.abspath(os.path.join(root, "..", "backups"))
            zip_path = mod.do_backup(out_dir, False, True)
            return {"ok": True, "action": "backup", "zip": zip_path,
                    "size_mb": round(os.path.getsize(zip_path) / 1e6, 2)}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "action": "backup", "error": f"{type(error).__name__}: {error}"}

    try:
        import datetime as _dt
        mod = _load_bin_script("libmem-export.py")
        layers = [x.strip() for x in (layer or "").split(",") if x.strip()]
        items = mod.load({"days": int(days or 0), "layers": layers})
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(os.path.dirname(mod.DATA), "exports")
        if (format or "md").lower() == "jsonl":
            target = os.path.join(out_dir, f"memories-{stamp}.jsonl")
            msg = mod.to_jsonl(items, target, bool(with_raw))
        else:
            target = os.path.join(out_dir, f"md-{stamp}")
            msg = mod.to_md(items, bool(with_raw), target)
        return {"ok": True, "action": "export", "message": msg,
                "items": len(items), "target": target}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "action": "export", "error": f"{type(error).__name__}: {error}"}


def collocate(target: str, kind: str = "entity", order: str = "time",
              limit: int = 0, offset: int = 0) -> dict:
    """**聚合清单**：属于 `target` 的**全部**条目（不是最像的 N 条）。

    ★ 2026-11-01（L2）：这是目录五功能里的「聚合」（卡特 collocation）——
      「关于这个主题，本馆究竟有哪些」。

    与 `memory_search` 的根本区别：
      * `memory_search` 答「哪条最像这句话」→ 排序问题，top-k 就够；
      * 本工具答「一共有哪些」→ **完整性**问题，一条都不许少、也不许重复。

    所以它**故意不做相关性排序**：默认按时间倒序，全部返回。
    返回里带 `total / returned / truncated / next_offset`，少给了能看见。

    别名合并：`书房OS` 与 `书房 OS` 会被归一后合并，命中情况报在
    `merged_spellings` 里 —— 那是 L5（规范控制）的输入材料。

    Args:
        target: 实体名（`书房OS`）或类目名（`软件与代码`）。
        kind:   `entity` = 按实体/主题词；`class` = 按一级或二级类目。
        order:  `time`（默认）| `heat` | `class`。
        limit:  `0` = 全部（单次硬上限 500）；>0 = 取这么多。
        offset: 分页偏移，配合 `next_offset` 用。

    Returns: `{ok,total,returned,truncated,offset,next_offset,matched_terms,
               merged_spellings,items[]}`；`items` 每条带
              `memory_id, source_date, class, task_type, summary, heat, matched_by`。
    """
    from services.B8_catalog import collocate as _collocate
    from services.shared import features
    if not features.enabled("collocate"):
        return {"ok": False, "disabled": True,
                "error": "聚合清单已被关闭（features.collocate=false 或 LIBMEM_DISABLE）"}
    return _collocate(target, kind=kind, order=order, limit=limit, offset=offset)


def citations(memory_id: str, direction: str = "both", limit: int = 20) -> dict:
    """**引文邻居**：这条记忆是从哪条长出来的 / 哪条接着它。

    ★ 2026-11-01（L1）：四法里的「引文法」。《图书馆学导论》p.44：
      「引文与被引文献之间存在主题关联性，正是这种关联性，使引文成为一种**检索入口**。」

    与 `memory_search` 的区别是**关系类型**不同：
      * `memory_search` 走**相似度**（"这条像不像你说的"）；
      * 本工具走**谱系**（"这条是从那条来的"）。
    相似的东西可以毫不相干，谱系不会 —— 所以它是另一个入口，不是更好的排序。

    边的三种类型（都能在返回里看到 kind）：
      * `explicit`    后一条自己承认了（摘要含"上次/刚才/之前那个…"+ 共享实体），0.9
      * `successor`   同一场会话里紧接在后 + 共享实体，0.6
      * `same_thread` 同月 + 同任务类型 + 实体重叠 ≥2（弱边，只用来串线索），0.2

    Args:
        memory_id: 记忆 id（`memory_search` / `collocate` 返回的那个）。
        direction: `both` | `out`（它引用了谁）| `in`（谁引用了它）。
        limit: 每个方向最多几条。

    Returns: `{ok, memory_id, summary, out[], in[], total}`，
             每条带 `kind/confidence/evidence` + 对方的 `summary/source_date`。
    """
    from services.B8_catalog import citations_neighbors
    from services.shared import features
    if not features.enabled("citations"):
        return {"ok": False, "disabled": True,
                "error": "引文边已被关闭（features.citations=false 或 LIBMEM_DISABLE）"}
    return citations_neighbors(memory_id, direction=direction, limit=limit)


def citations_stats() -> dict:
    """引文边的体检：总边数、按类型分布、覆盖了多少条记忆。"""
    from services.B8_catalog import citations_stats as _cs
    return _cs()


def citations_backfill(dry_run: bool = True) -> dict:
    """全库重建引文边。**默认 dry_run**，先看会写多少再决定。

    ⚠️ 这是**读全库 + 两两比较**的活，库大了会慢；正常情况走写入钩子增量打边，
    只有历史数据回填才需要它。
    """
    from services.B8_catalog import citations_backfill as _cb
    return _cb(dry_run=dry_run)


def catalog_export(target: str, kind: str = "entity", format: str = "md",
                   with_citations: bool = True, save: bool = False) -> dict:
    """**著录导出**：把一个主题/类目的全部条目渲染成一份著录清单（极简 DC）。

    ★ 2026-11-01（L3）：对应《图书馆学导论》p.42 的 MARC 编码 / p.43 的目录显示格式。

    ⚠️ 它不是第三个平行的抽屉 —— 它是 L1 × L2 的**合成品**：
      * 集合来自 `collocate`（L2）：本工具直接调它，口径不会分叉；
      * 结构来自 `citations`（L1）：每条记录的 `relation` 是**真实抽取的引文边**
        （谁引用了谁），不是语料里那个没人维护的 `relations` 自由文本字段。

    Args:
        target: 实体名（`书房OS`）或类目名（`软件与代码`）。
        kind:   `entity` | `class`，与 `collocate` 完全一致。
        format: `md`（人读，可直接贴给人看）| `json`（机器，DC 记录数组）。
        with_citations: 关掉 = 只要清单，不求谱系（更快）。
        save:   True 时同时写一份到 `data/exports/`，返回里带 `path`。

    Returns: `{ok,total,linked,span,format,text,path?}`；
             `total` 一定等于清单里的记录条数（硬约束）。
    """
    from services.B8_catalog import catalog_export as _export
    from services.shared import features
    if not features.enabled("catalog"):
        return {"ok": False, "disabled": True,
                "error": "著录导出已被关闭（features.catalog=false 或 LIBMEM_DISABLE）"}
    return _export(target, kind=kind, format=format,
                   with_citations=with_citations, save=save)


# ---------------------------------------------------------------------------
# L5 · 查询改写（规范控制）—— **默认关**，见 shared/features.py 的说明
# ---------------------------------------------------------------------------
def _authority_expand(query: str) -> tuple[str, list]:
    """把 query 里提到的实体，扩上它在词表里的其它写法。

    例：`书房OS 现在是什么状态` → `书房OS 现在是什么状态 书房 os`
    （词表里这条的规范词是 `书房 os`，不扩的话向量/词法都匹配不到那些写
    成「书房 OS」的条目 —— 这就是 45 组写法冲突里的核心几组。）

    **为什么默认关**：它改的是**检索输入**，也就是会改排序结果。
    按 L5 的设计（`ROADMAP §L5`），这类改动必须先 A/B 量过才允许开。
    实测见 `scratch/ab_authority.py`。

    Returns: (改写后的 query, 追加的词列表)。没开 / 没命中时原样返回。
    """
    try:
        from services.shared import features
        if not features.enabled("authority_rewrite"):
            return query, []
    except Exception:  # noqa: BLE001
        return query, []
    text = (query or "").strip()
    if not text or len(text) > 200:
        return query, []

    try:
        from services.B3_header_engine.sqlite_store import get_conn
        from services.B8_catalog import normalize_term
        q = normalize_term(text)
        conn = get_conn()
        try:
            rows = conn.execute("SELECT canonical, aliases_json FROM terms").fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return query, []

    extra: list[str] = []
    for canon, aliases in rows:
        try:
            al = [a for a in json.loads(aliases or "[]") if a]
        except Exception:
            al = []
        names = [canon] + al
        hit = any(n and normalize_term(n) in q for n in names)
        if not hit:
            continue
        for n in names:
            if not n:
                continue
            if normalize_term(n) in q:
                continue
            if n not in extra:
                extra.append(n)
        if len(extra) >= 4:
            break
    if not extra:
        return query, []
    extra = extra[:4]
    return (text + " " + " ".join(extra)), extra


def features_status() -> dict:
    """所有功能开关的**最终状态 + 来源**（default / config / env）。

    排查"为什么这个功能没生效"先看它：环境变量 `LIBMEM_DISABLE` 优先级最高，
    会盖过 config.yaml，而且不看这里根本发现不了。
    """
    from services.shared import features
    return {"ok": True, "flags": features.all_flags()}


def see_also_stats() -> dict:
    """参见边体检（L4）：多少条边、覆盖多少词、最强的几条长什么样。"""
    from services.B8_catalog import see_also_stats as _s
    return _s()


def see_also_build(dry_run: bool = True) -> dict:
    """重建参见边（PMI）。默认 dry_run。"""
    from services.B8_catalog import see_also_build as _b
    return _b(dry_run=dry_run)


def authority_lookup(term: str) -> dict:
    """**款目查询**：这个写法属于哪一个「作品」，还有哪些别的写法。

    ★ 2026-11-01（L5 的数据那一半）：两层款目表 —— 公版（随插件分发）+ 私版（AI 从
    这个用户的记忆里建）。私版覆盖公版。

    为什么它有用：聚合/著录里「同一个东西只算一份」靠的就是它。
    例：`书房OS` 与 `书房 OS` 是同一个作品，`resolve` 会给同一个 canonical。
    """
    from services.B8_catalog import authority as A
    r = A.resolve(term)
    if not r:
        return {"ok": True, "term": term, "found": False,
                "hint": "公版/私版里都没有这条。要么是新词（可等私版重建），"
                        "要么写法差得比分隔符更多（需要人工确认）。"}
    return {"ok": True, "term": term, "found": True, **r}


def authority_status() -> dict:
    """款目表体检 + **成长提醒**（该不该建一次私版）。

    `suggest.should_prompt=true` 时，把 `reasons` 说给用户听，
    他同意后再调 `authority_build`（**只做增量**）。
    ⚠️ 这个工具**不花算力**（只数数），所以可以随便调；
    真正烧 token 的是 `authority_build`，那个必须先问过用户。
    """
    from services.B8_catalog import authority as A
    return {"ok": True, "stats": A.stats(), "suggest": A.suggest()}


def authority_build(dry_run: bool = True) -> dict:
    """建私版款目表（**增量**）。默认 dry_run。

    ⚠️ 这是烧 token 的活（要判断"这两个名字是不是同一个东西"），
       **必须用户点头之后才调**，不许在后台静默跑。
    """
    from services.B8_catalog import authority as A
    return A.build_private(dry_run=dry_run)


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
    # ★ 2026-10-24（P3）：馆藏统一入口（search / browse / facets）
    "library": library,
    # guide 走 library(action="guide")，不用单独注册
    # ★ 2026-10-24（P3）：导出 / 备份
    "library_export": library_export,
    # ★ 2026-11-01（L2）：聚合清单 —— 目录五功能里的「聚合」，给全集不给 top-k
    "collocate": collocate,
    # ★ 2026-11-01（L1）：引文边 —— 四法里的「引文法」，另一个检索入口（谱系而非相似）
    "citations": citations,
    "citations_stats": citations_stats,
    "citations_backfill": citations_backfill,
    # ★ 2026-11-01（L3）：著录导出 —— L2 的集合 × L1 的结构
    "catalog_export": catalog_export,
    # ★ 2026-11-01（L4 临时版）：参见边（PMI）
    "see_also_stats": see_also_stats,
    "see_also_build": see_also_build,
    # ★ 2026-11-01（L5 数据版）：款目表（公版 + 私版）
    "authority_lookup": authority_lookup,
    "authority_status": authority_status,
    "authority_build": authority_build,
    # ★ 2026-11-01：功能开关体检（排查"为什么没生效"先看它）
    "features_status": features_status,
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
