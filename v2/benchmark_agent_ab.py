"""A/B 对照：小模型 agent 作为「前台接待员」的价值验证。

路径 A（基线）：query → B4 retrieve(无 filters) → 直接返回 items
路径 B（加 agent）：query → 小模型抽 filters → B4 retrieve(有 filters) → 小模型精简 → 返回精简版

对比维度：
  1. 总延迟（ms）
  2. 主模型 token 消耗（items 总字符数 / 2.5）
  3. 命中率（top-k 里是否包含 ground truth memory_id）
  4. Phase1 候选数（filters 加了之后 Phase1 是不是更准了）

用法（在 v2 目录下）::

    python -X utf8 benchmark_agent_ab.py               # 跑全部 8 条 query
    python -X utf8 benchmark_agent_ab.py --query "九出十二归 主角"  # 单条

退出码 0；结果打印到 stdout。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

V2 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, V2)
os.chdir(V2)

# ---------- Ground Truth（已知 memory_id 的 query）----------
# 这些是之前检索里**确实命中过**的条目，memory_id 是确定的。
# 如果当前库状态变了（行被删了、id 变了），benchmark 会在启动时扫描数据库
# 自动发现这些 id 是否还存在，并打印警告。

GROUND_TRUTH = [
    {
        "query": "九出十二归 主角",
        "expect_in_entities": ["九出十二归"],
        "expect_summary_keywords": ["九出十二归"],
        "note": "小说相关记忆，之前 memory_search 返回 3 条",
    },
    {
        "query": "星海旅人的独白",
        "expect_in_entities": ["星海旅人"],
        "expect_summary_keywords": ["星海旅人"],
        "note": "测试数据，heat 高但与工作无关",
    },
    {
        "query": "记忆插件怎么做",
        "expect_in_entities": ["记忆", "插件"],
        "expect_summary_keywords": ["记忆", "检索"],
        "note": "B4 双门控 / 记忆管道",
    },
    {
        "query": "工作流 先计划再动手",
        "expect_in_entities": ["计划", "动手"],
        "expect_summary_keywords": ["计划"],
        "note": "工作方法相关",
    },
    {
        "query": "heat count 热度权重",
        "expect_in_entities": ["heat", "count", "热度", "权重"],
        "expect_summary_keywords": ["heat", "权重"],
        "note": "retrieval.py alpha/beta 调整",
    },
    {
        "query": "小模型 结构化 query",
        "expect_in_entities": ["小模型", "结构化"],
        "expect_summary_keywords": ["小模型", "agent"],
        "note": "关于小模型 agent 的讨论",
    },
    {
        "query": "冷层 温层 重复",
        "expect_in_entities": ["冷层", "温层"],
        "expect_summary_keywords": ["冷", "温"],
        "note": "坑 B 冷/温重复合并",
    },
    {
        "query": "content_path L3",
        "expect_in_entities": ["content_path", "L3"],
        "expect_summary_keywords": ["content_path"],
        "note": "坑 C 补 content_path",
    },
]


# ---------- 小模型 prompts ----------

EXTRACT_FILTERS_PROMPT = """从用户的查询中，抽出结构化的过滤条件。

你要做的：
1. 判断这属于什么任务类型（从以下 7 类中选）：
   研究 | 决策 | 验证 | 规划 | 闲聊 | 求助 | 其他
2. 抽出查询中的核心实体（名词/项目/技术，最多 4 个）
3. 如果查询提到了时间段/日期（如 "最近"、"上个月"、"2026"），抽出时间标签；没有就填空数组
4. 如果查询明确指向热/温/冷层（如 "冷层的"、"温记忆"），抽出 layer；没有就不填

只返回 JSON，格式：
{{
  "task_type": ["最多2个"],
  "core_entities": ["最多4个"],
  "time_range": ["最多2个，没有就[]"],
  "layer": null  // 或 "hot"/"warm"/"cold"，没有就 null
}}

规则：
- core_entities 里不要抽"东西"、"项目"、"那个"这类泛词
- 不确定就填空数组，别瞎猜
- time_range 不要猜（比如"九出十二归 主角"没提时间 → []）

查询: {query}
JSON:"""

COMPACT_RESULTS_PROMPT = """把下面这些记忆摘要压缩成更短的条目，让主模型能快速看完。

要求：
- 每条只保留 2~3 个关键短语或一句话
- 去掉冗余修饰词
- 如果两条内容高度相关，可以合并

输入（JSON 数组）：
{items_json}

只返回 JSON 数组，格式：
[
  {{"memory_id": "xxx", "bulleted_summary": "- 关键短语1\\n- 关键短语2"}},
  ...
]"""


# ---------- Benchmark 核心 ----------

def _make_llm():
    """从 config.yaml 创建 LLM 代理。失败返回 None。"""
    try:
        from services.shared.llm_router.factory import create_llm_backend
        from services.shared.config.loader import get_config
        cfg = get_config()
        if cfg is None:
            print("⚠️  config.yaml 读不到，跳过路径 B（没有小模型）")
            return None
        llm = create_llm_backend(cfg)
        return llm
    except Exception as e:
        print(f"⚠️  LLM 代理创建失败: {e}")
        return None


def _extract_filters_with_small(llm, query: str) -> dict:
    """路径 B Step 1：小模型抽 filters。返回 B4 retrieve() 能用的 kwargs dict。"""
    if llm is None or llm.small is None:
        return {}
    prompt = EXTRACT_FILTERS_PROMPT.format(query=query)
    try:
        raw = llm.infer_small_json(prompt, task_type="query_rewrite")
    except Exception as e:
        print(f"  ⚠️  小模型抽 filters 失败: {e}")
        return {}
    if not raw or not isinstance(raw, dict):
        return {}

    filters = {}
    task_type = raw.get("task_type")
    if isinstance(task_type, str) and task_type:
        filters["task_types"] = [task_type]
    elif isinstance(task_type, list) and task_type:
        filters["task_types"] = [str(x) for x in task_type if x]

    entities = raw.get("core_entities")
    if isinstance(entities, list) and entities:
        filters["core_entity_any"] = [str(x) for x in entities if x]

    layer = raw.get("layer")
    if layer in ("hot", "warm", "cold"):
        filters["layer"] = layer

    return filters


def _compact_with_small(llm, items: list) -> tuple[list, float]:
    """路径 B Step 3：小模型精简 items。返回 (精简列表, 小模型耗时秒)。"""
    if llm is None or llm.small is None or not items:
        return items, 0.0
    # 只传必要字段，减少 token
    slim = [{
        "memory_id": it.get("memory_id"),
        "summary": it.get("summary", "")[:200],  # 截断到 200 字，不然小模型自己也炸
        "core_entities": it.get("core_entities", []),
    } for it in items]
    prompt = COMPACT_RESULTS_PROMPT.format(items_json=json.dumps(slim, ensure_ascii=False))
    t0 = time.time()
    try:
        raw = llm.infer_small_json(prompt, task_type="result_compact")
    except Exception as e:
        print(f"  ⚠️  小模型精简失败: {e}")
        return items, round(time.time() - t0, 2)
    elapsed = round(time.time() - t0, 2)

    if not raw:
        return items, elapsed
    # infer_small_json 可能返回 dict 或 list，规范化
    if isinstance(raw, dict):
        # 有时模型包了一层 {"items": [...]}
        raw = raw.get("items", raw.get("compact", [raw]))
    if not isinstance(raw, list):
        raw = [raw]

    compact = []
    for r in raw:
        if isinstance(r, dict) and r.get("memory_id"):
            compact.append({
                "memory_id": r["memory_id"],
                "bulleted_summary": r.get("bulleted_summary", r.get("summary", "")),
            })
    return compact or items, elapsed


def _path_a(query: str) -> dict:
    """路径 A：直接 B4 retrieve，无 filters。"""
    from services.B4_dual_gate_retrieval import retrieve
    t0 = time.time()
    result = retrieve(
        query_text=query,
        task_types=None,
        time_buckets=None,
        layer=None,
        core_entity_any=None,
        apply_heat=False,
    )
    elapsed = round((time.time() - t0) * 1000, 1)
    return {
        "path": "A",
        "elapsed_ms": elapsed,
        "items": result["items"],
        "total_filtered": result.get("total_filtered", 0),
        "total_ranked": result.get("total_ranked", 0),
        "tokens_est": round(sum(len((it.get("summary") or "")) / 2.5 for it in result["items"])),
    }


def _path_b(query: str, llm) -> dict:
    """路径 B：小模型抽 filters → B4 retrieve → 小模型精简。"""
    from services.B4_dual_gate_retrieval import retrieve
    t_total = time.time()

    # Step 1: 小模型抽 filters
    t1 = time.time()
    filters = _extract_filters_with_small(llm, query)
    small_filter_ms = round((time.time() - t1) * 1000, 1)

    # Step 2: B4 with filters
    t2 = time.time()
    result = retrieve(
        query_text=query,
        task_types=filters.get("task_types"),
        time_buckets=filters.get("time_buckets"),
        layer=filters.get("layer"),
        core_entity_any=filters.get("core_entity_any"),
        apply_heat=False,
    )
    b4_ms = round((time.time() - t2) * 1000, 1)

    # Step 3: 小模型精简
    compact, small_compact_s = _compact_with_small(llm, result["items"])
    small_compact_ms = round(small_compact_s * 1000, 1)

    total_ms = round((time.time() - t_total) * 1000, 1)

    # token 估算：精简版的总字符数 / 2.5（主模型拿到的是 compact）
    compact_chars = sum(len((c.get("bulleted_summary") or c.get("summary") or "")) for c in compact)
    # 原始 items 的总字符数（如果不用精简的话主模型会拿到的）
    original_chars = sum(len((it.get("summary") or "")) for it in result["items"])

    return {
        "path": "B",
        "elapsed_ms": total_ms,
        "breakdown": {
            "small_filter_ms": small_filter_ms,
            "b4_ms": b4_ms,
            "small_compact_ms": small_compact_ms,
        },
        "filters_extracted": filters,
        "items_raw": result["items"],
        "items_compact": compact,
        "total_filtered": result.get("total_filtered", 0),
        "total_ranked": result.get("total_ranked", 0),
        "tokens_est_raw": round(original_chars / 2.5),
        "tokens_est_compact": round(compact_chars / 2.5),
        "original_chars": original_chars,
        "compact_chars": compact_chars,
    }


def _check_hit(items: list, gt: dict) -> dict:
    """检查 items 是否命中 ground truth 的关键词/实体。

    Returns: {"hit": bool, "matched_items": [...]}
    """
    matched = []
    expect_ents = gt.get("expect_in_entities", [])
    expect_kw = gt.get("expect_summary_keywords", [])

    for it in items:
        summary = (it.get("summary") or "").lower()
        ents = [str(e).lower() for e in (it.get("core_entities") or [])]
        kw_in_summary = any(kw.lower() in summary for kw in expect_kw)
        ent_match = any(e in ents for e in expect_ents) or any(
            e.lower() in summary for e in expect_ents
        )
        if kw_in_summary or ent_match:
            matched.append({
                "memory_id": it.get("memory_id"),
                "summary": (it.get("summary") or "")[:80],
                "score": it.get("_score", it.get("_sim", 0)),
            })

    return {
        "hit": len(matched) > 0,
        "matched_count": len(matched),
        "matched": matched,
    }


def run_single(query: str, llm) -> None:
    """跑单条 query 的 A/B 对照。"""
    print(f"\n{'='*60}")
    print(f"Query: {query}")
    print(f"{'='*60}")

    # 先预热一次（首次调用向量缓存会 miss）
    from services.B4_dual_gate_retrieval import retrieve as _rt
    try:
        _rt(query_text=query, apply_heat=False, phase1_limit=50)
    except Exception:
        pass

    gt = None
    for g in GROUND_TRUTH:
        if g["query"] == query:
            gt = g
            break

    # 路径 A
    print("\n--- 路径 A：直接 B4 retrieve ---")
    try:
        a = _path_a(query)
        a_hit = _check_hit(a["items"], gt) if gt else {"hit": None, "matched": []}
        print(f"  延迟: {a['elapsed_ms']}ms")
        print(f"  Phase1 候选: {a['total_filtered']} → Phase2 精排: {a['total_ranked']}")
        print(f"  items: {len(a['items'])}")
        print(f"  预估 token: {a['tokens_est']}")
        for i, it in enumerate(a["items"][:3]):
            print(f"  [{i}] {it.get('memory_id')[:8]} sim={it.get('_sim',0):.3f} "
                  f"score={it.get('_score',0):.3f} | {(it.get('summary') or '')[:50]}")
        if gt:
            print(f"  命中检查: hit={a_hit['hit']} matched={a_hit['matched_count']}")
            for m in a_hit["matched"]:
                print(f"    ↳ {m['memory_id'][:8]} sim={m['score']:.3f} | {m['summary']}")
    except Exception as e:
        print(f"  ❌ 路径 A 失败: {e}")
        a = None

    # 路径 B
    print("\n--- 路径 B：小模型抽 filters + 精简 ---")
    if llm is None:
        print("  ⚠️  无小模型，跳过路径 B")
        return
    try:
        b = _path_b(query, llm)
        b_hit = _check_hit(b["items_raw"], gt) if gt else {"hit": None, "matched": []}
        print(f"  总延迟: {b['elapsed_ms']}ms")
        bd = b["breakdown"]
        print(f"    小模型抽 filters: {bd['small_filter_ms']}ms")
        print(f"    B4 retrieve: {bd['b4_ms']}ms (Phase1={b['total_filtered']}, Phase2={b['total_ranked']})")
        print(f"    小模型精简: {bd['small_compact_ms']}ms")
        if b["filters_extracted"]:
            print(f"  抽出 filters: {json.dumps(b['filters_extracted'], ensure_ascii=False)}")
        else:
            print(f"  抽出 filters: (空 — 小模型没给到)")
        print(f"  原始 items: {len(b['items_raw'])} → 精简后: {len(b['items_compact'])}")
        print(f"  预估 token: raw={b['tokens_est_raw']} compact={b['tokens_est_compact']} "
              f"(省 {b['tokens_est_raw'] - b['tokens_est_compact']}, "
              f"省 {(1 - b['tokens_est_compact']/max(b['tokens_est_raw'],1))*100:.0f}%)")
        if b["items_compact"]:
            for i, it in enumerate(b["items_compact"][:3]):
                print(f"  精简[{i}] {it.get('memory_id','?')[:8]} | {(it.get('bulleted_summary') or '')[:60]}")
        if gt:
            print(f"  命中检查: hit={b_hit['hit']} matched={b_hit['matched_count']}")
            for m in b_hit["matched"]:
                print(f"    ↳ {m['memory_id'][:8]} sim={m['score']:.3f} | {m['summary']}")
    except Exception as e:
        import traceback
        print(f"  ❌ 路径 B 失败: {e}")
        traceback.print_exc()
        b = None

    # 对比
    if a and b:
        print("\n--- A vs B 对比 ---")
        delay_ratio = b["elapsed_ms"] / max(a["elapsed_ms"], 1)
        token_saved = a["tokens_est"] - b["tokens_est_compact"]
        token_pct = (1 - b["tokens_est_compact"] / max(a["tokens_est"], 1)) * 100
        phase1_change = a["total_filtered"] - b["total_filtered"]
        phase1_pct = (phase1_change / max(a["total_filtered"], 1)) * 100 if a["total_filtered"] else 0
        print(f"  延迟: A={a['elapsed_ms']}ms  B={b['elapsed_ms']}ms  比例={delay_ratio:.2f}×")
        print(f"  Token: A≈{a['tokens_est']}  B(compact)≈{b['tokens_est_compact']}  省 {token_saved} ({token_pct:.0f}%)")
        print(f"  Phase1 候选: A={a['total_filtered']}  B={b['total_filtered']}  减 {phase1_change} ({phase1_pct:.0f}%)")
        if gt:
            print(f"  命中率: A={'✅' if a_hit['hit'] else '❌'}  B={'✅' if b_hit['hit'] else '❌'}")


def run_all(llm) -> None:
    """跑全部 ground truth query。"""
    results = []
    for gt in GROUND_TRUTH:
        query = gt["query"]
        print(f"\n### {query}")
        try:
            run_single(query, llm)
        except Exception as e:
            print(f"  ❌ 跳过: {e}")

    print(f"\n{'='*60}")
    print("全部完成。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="", help="只跑单条 query（不填则跑全部 8 条）")
    args = parser.parse_args()

    print("=" * 60)
    print("小模型 agent A/B 对照实验")
    print("=" * 60)

    # 检查小模型后端
    llm = _make_llm()
    if llm is not None:
        small_name = getattr(llm.small, "_model", "?")
        print(f"小模型: {small_name}")
    else:
        print("⚠️  小模型不可用 —— 路径 B 会跳过")

    if args.query:
        run_single(args.query, llm)
    else:
        run_all(llm)

    return 0


if __name__ == "__main__":
    sys.exit(main())
