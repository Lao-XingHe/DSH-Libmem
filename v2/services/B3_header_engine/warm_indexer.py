"""B2 → B3 索引器：把温记忆（warm 层）落进 B3 表头主索引。

为什么需要这个模块
------------------
B2（`services/B2_warm_extract`）的小模型填表结果写在**温层文件**里
（`data/warm/YYYY-MM-DD.jsonl`），而 B4 检索的 Phase1 只从 **B3 `memories` 表**
取候选。两者之间原本**没有任何索引步骤**：

- 全项目里 `bulk_insert`（写 B3 的唯一入口）只被 `migrate_v1_data.py` 调用；
- 因此 `data/warm/` 里的新条目**永远不会进入 B3**，`memory_search` 也就查不到
  新产生的对话 —— 实测：B2 抽出 26 条温记忆后，B3 仍是迁移时的 771 条，
  检索返回的全是旧的 `[冷]` 条目。

字段映射完全对齐 `migrate_v1_data.py::migrate_warm()`，避免两套口径：

===============  ====================================================
B3 字段           来源
===============  ====================================================
`memory_id`      `md5("{source_date}#{id}")[:12]`（与 B3/v1 同一算法）
`layer`          `heat > 15 → hot`，`heat > 5 → warm`，否则 `cold`
`summary`        `summary`，截断 500 字
`task_type`      温记忆的 `type`，缺失时按 `_guess_task_type` 推断
`core_entities`  `entities + topics` 去重，最多 8 个
`time_bucket`    `source_date[:7]`（YYYY-MM）
`heat` / `count` `heat` / `callCount`
`content_path`   留空：L2/L3 存放约定尚未确定（见 HANDOFF §7）
===============  ====================================================

写入用 `INSERT OR REPLACE`，且 `memory_id` 由内容派生，所以**可以反复跑**：
同一条温记忆重复索引只会覆盖自己，不会产生重复行。

用法（在 v2 目录下执行）::

    python -m services.B3_header_engine.warm_indexer            # 索引全部
    python -m services.B3_header_engine.warm_indexer --dry-run  # 只看不写
    python -m services.B3_header_engine.warm_indexer --prune    # 清掉温层已不存在的行
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 与 migrate_v1_data._guess_task_type 保持一致：两套口径会让同一批数据分层不同
_KEYWORDS = [
    ("决策", ["决策", "选择", "决定", "方案"]),
    ("研究", ["研究", "论文", "技术", "调研", "分析"]),
    ("验证", ["验证", "测试", "对比", "性能", "效果"]),
    ("规划", ["规划", "计划", "设计", "架构", "路线"]),
    ("闲聊", ["闲聊", "聊天", "讨论"]),
    ("求助", ["帮助", "求助", "怎么办", "指导"]),
]


def memory_id_for(date_str: str, entry_id: str) -> str:
    """B3/v1 共用的 memory_id：md5("{date}#{entry}")[:12]。

    ⚠️ 2026-09-30：实现已**收敛**到 `cold_locate.memory_id_for`（唯一一份）。
    见那里的模块文档：这条规则原有三份拷贝，路4 的原文索引再抄就是第四处。
    """
    from .cold_locate import memory_id_for as _impl
    return _impl(date_str, entry_id)


def guess_task_type(summary: str, entities: list) -> str:
    """温记忆没有 type 时的兜底推断（与 migrate_v1_data 同规则）"""
    joined = " ".join((entities or []) + [summary or ""]).lower()
    for label, keys in _KEYWORDS:
        if any(k in joined for k in keys):
            return label
    return "其他"


def layer_for(heat: float) -> str:
    """热度分层（与 migrate_v1_data 同阈值）"""
    if heat > 15:
        return "hot"
    if heat > 5:
        return "warm"
    return "cold"


def _clean_entities(ents: list[str]) -> list[str]:
    """过滤 entities 里的问句碎片 / 句子片段 / 极短噪声。

    B2 小模型提取 entities 时会塞问句碎片进来（90% 的行都有），比如：
      ['那么我这个工', '你写吧', '把架构思想固', 'api', '代码']
    其中 'api'/'代码' 是合法短实体，要保留；问句碎片要过滤。

    规则（纯规则，不调模型）：
      ✅ 保留：含英文/数字、≥4 字中文名词短语、无问号
      ❌ 过滤：含问号、**以问句/语气助词收尾**、代词/助词开头且 ≤6 字、明显动词性片段

    2026-09-25 补「以问句助词收尾」这一条：原先只查 `？` 这个**字面**，于是
    「有办法突破吗 / 有什么优点呢 / 是无解吗 / 记一下吧 / 这怎么回事呢 /
    面版运行呢 / 先试试吧」这类**不带问号但明显是问句碎片**的实体全部漏过
    （实测库里残留 7 条）。中文实体不会以 吗/呢/吧/啊/哦 收尾，所以这条误杀风险极低。
    """
    if not ents:
        return []

    _PRONOUN_STARTS = ("你", "我", "他", "她", "它", "那么", "这个", "那个", "话", "呢", "吧", "啊", "哦")
    _VERB_WORDS = ("把", "让", "做", "写", "看", "想", "给", "用", "去", "来", "能", "会", "要", "可以")
    _QUESTION_TAILS = ("吗", "呢", "吧", "啊", "哦")

    result = []
    for e in ents:
        if not isinstance(e, str) or not e.strip():
            continue
        e = e.strip()

        # 含英文或数字 → 保留（api, python, bge-m3, 2024）
        if re.search(r"[a-zA-Z0-9]", e):
            result.append(e)
            continue

        # 含问号 → 必删
        if "？" in e or "?" in e:
            continue

        # 以问句/语气助词收尾 → 必删（"有办法突破吗"，不带问号但就是问句）
        if e.endswith(_QUESTION_TAILS):
            continue

        # ≤3 字纯中文 → 除非是常见技术名词白名单，否则删
        if len(e) <= 3:
            _TECH_WHITELIST = {"代码", "模型", "记忆", "向量", "检索", "知识", "数据", "工具", "文件", "图片", "视频", "对话", "文档", "应用", "系统", "架构", "数据库", "界面", "API"}
            if e in _TECH_WHITELIST:
                result.append(e)
            continue

        # 代词/助词开头 + ≤6 字 → 问句碎片
        if e.startswith(_PRONOUN_STARTS) and len(e) <= 6:
            continue

        # 含动词性结构 + ≤8 字 → 句子片段（把架构思想固）
        if len(e) <= 8 and any(v in e for v in _VERB_WORDS):
            continue

        # 过滤后长度仍 ≥2 → 保留
        result.append(e)

    # 去重保序 + 最多 8 个
    return list(dict.fromkeys(result))[:8]


def warm_to_memory(entry: dict) -> dict | None:
    """把一条温记忆转换成 `bulk_insert` 需要的内存字典。

    返回 None 表示这条不该索引（缺日期或摘要为空）。

    2026-09-23 修坑 A（双重哈希）：
    温层条目里 `id` 已经是哈希（b90c8adcf5be），`ref` 才是 "2026-09-21#204314719000"
    （date + 冷库原始 id）。这里必须从 ref 拆冷库原始 id 来算 memory_id，
    否则 B6.read_drilldown 靠 md5 反查永远匹配不上（双重哈希了）。
    同时 content_path 必须填上冷库 JSONL 路径（V2 运行时用 config loader）。
    """
    date_str = str(entry.get("source_date") or "").strip()
    if not date_str:
        return None

    summary = (entry.get("summary") or entry.get("content") or "").strip()
    if not summary:
        return None

    # —— 坑 A 修复：从 ref 拆冷库原始 id ——
    # 温层条目格式: {id: "b90c8adcf5be", ref: "2026-09-21#204314719000"}
    # B3 的 memory_id = md5(f"{source_date}#{cold_entry_id}")[:12]
    # 不能用 entry["id"]（已经是哈希），必须用 ref 里的冷库原始 id
    ref = entry.get("ref") or ""
    cold_entry_id = ""
    if "#" in ref:
        cold_entry_id = ref.split("#", 1)[1].strip()
    if not cold_entry_id:
        # ref 缺失（极少数），退而用 entry["id"]（会有双重哈希，标注出来）
        cold_entry_id = str(entry.get("id") or "").strip()
        if cold_entry_id:
            import warnings
            warnings.warn(f"warm_to_memory: ref 缺失，entry_id 走双重哈希：{cold_entry_id}")
    if not cold_entry_id:
        return None

    entities = entry.get("entities") or []
    topics = entry.get("topics") or []
    # 2026-09-23: 先合起来、再 _clean_entities 过滤问句碎片（90% 行都有）
    raw_ents = list(dict.fromkeys(list(entities) + list(topics)))
    core_entities = _clean_entities(raw_ents)

    heat = entry.get("heat") or 0.0
    if not isinstance(heat, (int, float)):
        heat = 0.0
    count = entry.get("callCount") or entry.get("call_count") or 0
    if not isinstance(count, int):
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0

    # ---- task_type（v1.3.2 起由标引侧产出**封闭取值**）----
    #
    # 历史：这里原来只有 `entry.get("type")`，而 `type` 的词表是
    # 「事实/偏好/决策/问题/灵感」—— 跟 `task_type` 想要的
    # 「研究/决策/验证/规划/闲聊/求助/其他」**是两套词表**。
    # 于是同一个字段名承载两种语义，实测最终 78.8% 落成"闲聊"（见 task_types.yaml 文件头）。
    #
    # 现在的取值优先级：task_type（新，封闭）→ type（老数据，兼容）→ 关键词兜底。
    # **越界值不直接落库** —— 落进去就又变成"没有编目规则的字段"。
    declared = (entry.get("task_type") or "").strip()
    if declared:
        try:
            from services.shared import task_types as _TT
            if declared in _TT.values():
                task_type = declared
            else:
                task_type = _TT.normalize(declared)
                warnings.warn(f"task_type 越界值 {declared!r} → 收进 {task_type!r}")
        except Exception:                                        # noqa: BLE001
            task_type = declared
    else:
        legacy_type = (entry.get("type") or "").strip()
        task_type = legacy_type or guess_task_type(summary, core_entities)

    # content_path：用 config loader 解析冷库目录 + 日期文件名
    # V2 运行时索引器 —— 指向 V2 自己的 cold_vault
    try:
        from services.shared.config.loader import get_config
        cfg = get_config()
        cold_dir = cfg.get("paths", {}).get("cold_vault", "")
        if cold_dir:
            content_path = os.path.join(cold_dir, f"{date_str}.jsonl")
        else:
            content_path = ""
    except Exception:
        content_path = ""

    return {
        "memory_id": memory_id_for(date_str, cold_entry_id),
        "layer": layer_for(float(heat)),
        "content_path": content_path,
        "summary": summary[:500],
        "task_type": task_type,
        "session_id": entry.get("sessionId") or entry.get("session_id") or "",
        "time_bucket": date_str[:7],
        "core_entities": core_entities,
        "emotional_tone": "",
        "source_weight": 1.0,
        "heat": float(heat),
        "count": count,
        "confidence": 1.0,
        "created_at": entry.get("createdAt") or entry.get("created_at") or "",
        "updated_at": entry.get("createdAt") or entry.get("created_at") or "",
        # Phase2 新字段
        "relations": entry.get("relations") or [],
        "source_date": date_str,
        "has_l3": 1 if (content_path and os.path.exists(content_path)) else 0,
    }


def collect_memories() -> tuple[list[dict], dict]:
    """读取全部温记忆并转换成 B3 行。

    返回 (行列表, 统计)，统计里区分「温层条目数」「转换成功数」「跳过数」。
    """
    from services.B2_warm_extract import load_all_warm_entries

    entries = load_all_warm_entries()
    memories, skipped = [], 0
    for entry in entries:
        memory = warm_to_memory(entry)
        if memory is None:
            skipped += 1
            continue
        memories.append(memory)

    stats = {
        "warm_entries": len(entries),
        "converted": len(memories),
        "skipped": skipped,
        "dates": sorted({m["time_bucket"] for m in memories}),
    }
    return memories, stats


def index_warm(dry_run: bool = False, prune: bool = False) -> dict:
    """把温记忆索引进 B3，返回统计。

    Args:
        dry_run: 只统计不写入。
        prune: 写入后删除 B3 中「温层已不存在」的自动索引行。
            只删 `data_sovereignty='app'` 且 memory_id 不在本次集合里的行中
            属于温层的那些 —— 迁移来的历史行不在温层文件里，所以**不要**
            在没搞清楚来源前用它，默认关闭。
    """
    from services.B3_header_engine import bulk_insert, count_total, get_layer_distribution

    before = count_total()
    memories, stats = collect_memories()

    # 2026-09-23：同一段对话在温层里可能有多条条目 —— `extract_warm_from_date` 是
    # 「读已存在 id → 逐个追加」，两个线程同时跑就会各写一遍（实测同一 ref 写了两条，
    # createdAt 只差 131ms）。而新的 id 规则是 `md5(source_date#冷库id)`，
    # 所以这两条会算出**同一个 memory_id** → `bulk_insert` 只留下一行（后写者赢）。
    #
    # 这**符合设计**（一段对话 = 一条记忆），但必须让人看得见 ——
    # 否则「温层 41 条 → B3 只有 40 行」看起来像丢数据（实测就因此让
    # verify-reindex-heat.py 红过一次）。这里把它列出来。
    seen: dict[str, list[str]] = {}
    for m in memories:
        seen.setdefault(m["memory_id"], []).append((m.get("summary") or "")[:48])
    collapsed = [{"memory_id": mid, "variants": variants}
                 for mid, variants in seen.items() if len(variants) > 1]

    result = {
        "before": before,
        "warm_entries": stats["warm_entries"],
        "converted": stats["converted"],
        "skipped": stats["skipped"],
        "dates": stats["dates"],
        "dry_run": dry_run,
        # 条目数 ≠ 行数：同 ref 的重复条目会被合并成一行（见上）
        "distinct_ids": len(seen),
        "collapsed": collapsed,
    }

    if dry_run:
        result["written"] = 0
        result["after"] = before
        result["sample"] = [
            {k: m[k] for k in ("memory_id", "layer", "task_type", "summary")}
            for m in memories[:3]
        ]
        return result

    written = bulk_insert(memories)
    after = count_total()
    result["written"] = written
    result["after"] = after
    result["new_rows"] = after - before
    result["layers"] = get_layer_distribution()

    # ★ P1（2026-10-24）：同步写馆藏对象（items/descriptions）—— 标目在入库时产生。
    #   失败**绝不影响索引**：标目是旁路，坏了最多是这一批查不到分类/主题词。
    try:
        from services.B8_catalog import upsert_many
        result["catalogued"] = upsert_many(memories)
    except Exception as error:  # noqa: BLE001
        result["catalogued"] = f"failed: {type(error).__name__}: {error}"
        print(f"  [catalog] 标目写入失败（不影响索引）: {error}", file=sys.stderr)

    # ★ L1（2026-11-01）：引文边 —— 四法里唯一缺的那一种。
    #   同样**绝不拖垮索引**：引文是旁路，坏了最多是这条查不到"从哪来"。
    #   幂等（主键 from_id+to_id+kind），重索引跑多次不会长出重复边。
    try:
        from services.B8_catalog.citations import link_many as _link_citations
        result["citations"] = _link_citations(memories)
    except Exception as error:  # noqa: BLE001
        result["citations"] = f"failed: {type(error).__name__}: {error}"
        print(f"  [citation] 引文边写入失败（不影响索引）: {error}", file=sys.stderr)

    # 新记忆落库 → 实体倒排索引必须重建（否则覆盖面自检数的是旧库）。
    # 见 collocate._entity_index 的注释：缓存不能用 write_epoch 失效，
    # 因为检索升温也会写库；只有**新增记忆**才需要重建。
    try:
        from services.B8_catalog.collocate import invalidate_entity_index
        invalidate_entity_index()
        result["entity_index_invalidated"] = True
    except Exception:  # noqa: BLE001 - 缓存清不掉最多陈旧两分钟（有 TTL 兜底）
        result["entity_index_invalidated"] = False

    # 2026-09-24 A4 修复：重索引后清 B4 进程内候选向量缓存。
    # 原缓存键只用 memory_id，重提炼后 summary/core_entities 变了但还拿旧向量。
    # 现在缓存文本感知，但整个 index_warm 跑完后全清一次最稳 —— 进程内缓存重建一次比
    # 逐条 invalidate 简单，而且只在重索引时触发（正常检索不走这里）。
    try:
        from services.B4_dual_gate_retrieval.retrieval import invalidate_candidate_cache
        invalidate_candidate_cache()
        result["cache_invalidated"] = True
    except Exception as e:
        result["cache_invalidated"] = False
        result["cache_invalidate_error"] = str(e)

    if prune:
        result["pruned"] = _prune(memories)

    # ★ P1 心跳（2026-10-12）：索引也是一个工位，跑完留一条。
    #   `counts` 里带上 `collapsed`（同 ref 被折叠的组数）—— 那个数字异常增大
    #   就是"上游出现了重复"的最早信号，比人肉数条数快得多。
    try:
        from services.shared import heartbeat
        heartbeat.stamp('b3.index', 'ok',
                        counts={'warm_entries': stats['warm_entries'],
                                'written': written,
                                'before': before, 'after': after,
                                'collapsed': len(collapsed),
                                'pruned': result.get('pruned', 0)},
                        note='重索引温层')
    except Exception:                       # noqa: BLE001 - 心跳失败不许影响索引
        pass

    return result


def _prune(memories: list[dict]) -> int:
    """删除 B3 里温层已不存在的自动索引行（默认不启用）"""
    from services.B3_header_engine import get_conn

    keep = {m["memory_id"] for m in memories}
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT memory_id FROM memories WHERE data_sovereignty = 'app'"
        ).fetchall()
        stale = [r["memory_id"] for r in rows if r["memory_id"] not in keep]
        if stale:
            conn.executemany("DELETE FROM memories WHERE memory_id = ?", [(m,) for m in stale])
        return len(stale)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="B2 温记忆 → B3 表头索引")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--prune", action="store_true", help="删除温层已不存在的自动索引行")
    args = parser.parse_args()

    result = index_warm(dry_run=args.dry_run, prune=args.prune)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
