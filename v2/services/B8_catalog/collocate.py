# -*- coding: utf-8 -*-
"""B8 · 聚合清单（collocate）—— 2026-11-01，L2

**它解决什么**
------------
插件里所有入口都在做「选最好的 N 条」：`memory_search` 给 top-5/8，
`library browse` 给一页 20 条。而图书馆目录的第二个功能是**聚合**——
「关于这个主题，本馆究竟有哪些」(《图书馆学导论》p.46，卡特：collocation)。

这两个问题不一样：
  * 「哪条最像」→ 排序问题，top-k 就够；
  * 「一共有些什么」→ **完整性**问题，一条都不许少，也不能有重复。

所以本模块的契约是：**默认返回全集**（`limit=0`），且必须自报 `total` /
`returned` / `truncated` —— 少给了要能看见，不能像时间检索那样悄悄截断。

**数据源（三个叠加，取并集）**
------------------------
1. `terms` 规范词（含 aliases）→ `mem_terms` 倒排 —— 精确、认别名；
2. `memories.core_entities` 直接扫 —— 兜底，**不依赖词表是否覆盖全库**；
3. `descriptions.class / sub_class` —— 只在 `kind="class"` 时用。

为什么 2 不能省：`mem_terms` 是 `bin/build-vocab.py` 事后生成的，不是写入时同步的。
只走词表会在"新条目还没编目"时**静默少给**，而那正是本模块最不能犯的错。

**只读**：不写库、不升温、不写审计（它不改变任何状态，没有副作用可审）。
"""
from __future__ import annotations

import json
import re
from collections import Counter

__all__ = ["collocate"]

_MAX_ROWS = 500          # 单次硬上限：再大就不适合塞进上下文，宁可分页
_ORDERS = ("time", "heat", "class")


def _norm(text: str) -> str:
    """与 B8 同一个归一（全半角/空白/大小写），**复用它而不是再写一份**。"""
    from services.B8_catalog import normalize_term
    return normalize_term(text)


# 分隔符：中文全角顿号/间隔号也要算
_SEP = re.compile(r"[\s\-_·、/]+")


def _key(text: str) -> str:
    """**聚合用的匹配键**：在 B8 归一之上再抹掉分隔符。

    为什么需要多这一步（2026-11-01 实测踩到）：
    `normalize_term("书房 OS")` → `"书房 os"`（空格保留）
    `normalize_term("书房OS")`  → `"书房os"`
    两者**并不相等**，于是「同一实体的两种写法」在这个模块里就会各自成组 ——
    而聚合的全部意义就是「同一样东西只算一份」。

    这是一次**故意的弱匹配**：抹分隔符可能把不该合的合到一起（例如 `lib-mem`
    与 `libmem`），所以命中结果里保留 `merged_spellings` 逐条自报，
    让人能看见「这次合了哪些写法」。
    """
    return _SEP.sub("", _norm(text))


def _parse_entities(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except Exception:
        return []


# 实体倒排索引缓存：{归一键: (memory_id 集合, 原始写法计数)}
#
# ⚠️ 2026-11-01 踩到的坑：第一版用 `write_epoch` 做失效键 —— 看起来最"正确"，
#    实际上**每次检索都失效**，因为检索会升温（heat+1）写库，纪元每次都变。
#    结果就是每次 search 都重建 906 行的索引，实测给检索加了 ~70ms（188→258）。
#    → 改成 **TTL + 显式失效**：热路径上零重建；新记忆落库时由写入侧
#    `invalidate_entity_index()` 主动清一次（warm_indexer 里已接）。
_ENT_CACHE: dict = {"index": None, "ts": 0.0}
_ENT_TTL = 120.0        # 秒；显式失效漏了也最多陈旧两分钟


def invalidate_entity_index() -> None:
    """清空实体索引缓存（新记忆落库后由写入侧调用）。"""
    _ENT_CACHE.update({"index": None, "ts": 0.0})


def _entity_index(conn) -> dict:
    import time as _time
    now = _time.time()
    if _ENT_CACHE["index"] is not None and now - _ENT_CACHE["ts"] < _ENT_TTL:
        return _ENT_CACHE["index"]
    index: dict = {}
    for mid, ents in conn.execute("SELECT memory_id, core_entities FROM memories"):
        seen = set()
        for e in _parse_entities(ents):
            if not isinstance(e, str):
                continue
            k = _key(e)
            if not k or k in seen:     # 同一条里重复的写法只记一次
                continue
            seen.add(k)
            slot = index.get(k)
            if slot is None:
                slot = (set(), Counter())
                index[k] = slot
            slot[0].add(mid)
            slot[1][e] += 1
    _ENT_CACHE.update({"index": index, "ts": now})
    return index


def collocate(target: str, kind: str = "entity", order: str = "time",
              limit: int = 0, offset: int = 0) -> dict:
    """聚合清单：给出**属于 `target` 的全部条目**，不是前 N 条。

    Args:
        target: 实体名（`书房OS`）、类目名（`软件与代码`）。
        kind:   `entity`（按实体/主题词）| `class`（按一级或二级类目）。
        order:  `time`（时间倒序，默认）| `heat`（热度高在前）| `class`（按类目，再按时间）。
        limit:  `0` = 全部（受 `_MAX_ROWS` 硬上限保护）；>0 = 只取这么多。
        offset: 分页偏移。

    Returns:
        `{ok, target, kind, total, returned, truncated, offset, next_offset,
          matched_terms, merged_spellings, items[]}`

        `merged_spellings` 是本模块**故意暴露**的一个读数：
        `{"书房OS": 9, "书房 OS": 28}` 表示这次聚合把两种写法合并了。
        它是 L5（规范控制）的输入材料，不是错误。

        `items[]`: `{memory_id, source_date, class, sub_class, task_type,
                     summary, heat, layer, has_l3, matched_by}`
        `matched_by`: `term`（词表命中）| `entity`（实体直扫）| `both`。
    """
    from services.B3_header_engine.sqlite_store import get_conn

    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "target 不能为空"}
    kind = (kind or "entity").lower()
    order = (order or "time").lower()
    if kind not in ("entity", "class"):
        return {"ok": False, "error": f"kind 只能是 entity / class，收到 {kind!r}"}
    if order not in _ORDERS:
        return {"ok": False, "error": f"order 只能是 {'/'.join(_ORDERS)}，收到 {order!r}"}

    t_key = _key(target)
    matched_terms: list[str] = []
    hit: dict[str, set] = {}          # memory_id -> {"term","entity"}
    spellings: Counter = Counter()    # 被合并掉的原始写法

    conn = get_conn()
    try:
        if kind == "entity":
            # ① 词表：canonical / aliases 归一后相等
            #    复用 B8 的 `_vocab_cached`（按 write_epoch 缓存 2385 条词表），
            #    而不是每次都把 terms 全表读出来再归一 —— 那是 6~8ms 的纯浪费，
            #    而本函数现在**每次检索之后都会被调一次**做覆盖面自检。
            from services.B8_catalog import _vocab_cached
            term_ids = []
            for tid, canon, aliases, _df in _vocab_cached(conn):
                if _key(canon) == t_key:
                    term_ids.append(tid)
                elif any(_SEP.sub("", a) == t_key for a in aliases if a):
                    term_ids.append(tid)
                else:
                    continue
                matched_terms.append(canon)
            for tid in term_ids:
                for (mid,) in conn.execute(
                        "SELECT item_id FROM mem_terms WHERE term_id=?", (tid,)):
                    hit.setdefault(mid, set()).add("term")

            # ② 实体索引：不依赖词表覆盖率
            #    ⚠️ 2026-11-01：原先是每次调用**全表扫**（906 行 JSON 解析 ≈ 12ms）。
            #    现在检索会**在每次 search 之后**调一次 collocate 做覆盖面自检（见
            #    libmem_tools._enrich_search），12ms 就变成了检索的固定税。
            #    改成按 `write_epoch` 缓存的倒排索引：稳态下查一次是字典命中（<1ms），
            #    只有库被写过之后才重建一次。
            index = _entity_index(conn)
            got = index.get(t_key)
            if got:
                ids_hit, spell = got
                for mid in ids_hit:
                    hit.setdefault(mid, set()).add("entity")
                spellings.update(spell)

            if not hit:
                return {"ok": True, "target": target, "kind": kind,
                        "total": 0, "returned": 0, "truncated": False,
                        "offset": 0, "next_offset": None,
                        "matched_terms": [], "merged_spellings": {}, "items": [],
                        "hint": "没有条目带这个实体。可能是写法不同（试试别名），"
                                "或这条记忆从没被提炼出实体。"}

            ids = list(hit)
            ph = ",".join("?" * len(ids))
            rows = list(conn.execute(
                "SELECT m.memory_id, m.summary, m.task_type, m.source_date, m.heat, "
                "m.layer, m.has_l3, d.class, d.sub_class, d.terms "
                "FROM memories m LEFT JOIN descriptions d ON d.item_id = m.memory_id "
                f"WHERE m.memory_id IN ({ph})", ids))
        else:  # kind == "class"
            rows = list(conn.execute(
                "SELECT m.memory_id, m.summary, m.task_type, m.source_date, m.heat, "
                "m.layer, m.has_l3, d.class, d.sub_class, d.terms "
                "FROM descriptions d JOIN memories m ON m.memory_id = d.item_id "
                "WHERE d.class = ? OR d.sub_class = ?", (target, target)))
            for r in rows:
                hit.setdefault(r[0], set()).add("class")
            matched_terms = [target]

        items = []
        for mid, summary, task_type, sdate, heat, layer, has_l3, cls, sub, terms in rows:
            try:
                topics = [t for t in json.loads(terms or "[]") if isinstance(t, str)]
            except Exception:
                topics = []
            items.append({
                "memory_id": mid,
                "source_date": sdate or "",
                "class": cls or "",
                "sub_class": sub or "",
                "task_type": task_type or "",
                "summary": summary or "",
                "topics": topics,
                "heat": round(float(heat or 0.0), 2),
                "layer": layer or "",
                "has_l3": int(has_l3 or 0),
                "matched_by": "+".join(sorted(hit.get(mid, {"?"}))),
            })
    finally:
        conn.close()

    if order == "time":
        items.sort(key=lambda x: (x["source_date"], x["memory_id"]), reverse=True)
    elif order == "heat":
        items.sort(key=lambda x: (-x["heat"], x["source_date"]), reverse=False)
    else:  # class：类目升序，同类的按时间倒序（稳定排序两趟，不用复合键）
        items.sort(key=lambda x: x["source_date"], reverse=True)
        items.sort(key=lambda x: x["class"] or "~")

    total = len(items)
    offset = max(0, int(offset or 0))
    want = int(limit or 0)
    page = items[offset:offset + want] if want > 0 else items[offset:offset + _MAX_ROWS]
    truncated = (offset + len(page)) < total

    # L4：参见（只在结果里"附带"，不新增入口 —— 否则又变成平行抽屉）
    see = []
    try:
        from services.shared import features
        if features.enabled("see_also") and matched_terms:
            from services.B8_catalog.see_also import see_also as _see
            for term in matched_terms[:1]:
                see = _see(term, k=5)
    except Exception:  # noqa: BLE001 - 参见是旁路，坏了不影响聚合
        see = []

    return {
        "ok": True,
        "target": target,
        "kind": kind,
        "total": total,
        "returned": len(page),
        "truncated": truncated,
        "offset": offset,
        "next_offset": (offset + len(page)) if truncated else None,
        "matched_terms": matched_terms,
        "merged_spellings": dict(spellings),
        "see_also": see,
        "items": page,
    }
