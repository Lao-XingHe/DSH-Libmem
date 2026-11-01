# -*- coding: utf-8 -*-
"""B8 · 引文边（citations）—— 2026-11-01，L1

**为什么要有它（这是整套路线图的第一块地基）**
============================================
《图书馆学导论》p.32 把「知识组织整理」的基本方法列为四种：**分类、描述、编码、引文**。
前三种插件都有（分类表 / 摘要 / 向量与 id），只有**引文法**是零。

书 p.44 说得更直接：

> 引文与被引文献之间一般存在主题关联性，正是这种关联性，
> **使引文成为一种有效的检索入口**。

而插件现在唯一的入口是「这句话和哪条最像」——**相似度**。引文边给的是另一种关系：
「这条是从那条长出来的」——**谱系**。两者不是替代关系，是正交的两个入口。

**这条边表达什么**
================
不是「语义相近」，而是**发生学上的先后**：
  * `explicit`    后一条自己承认了（"上次/刚才/之前那个…"）+ 共享实体 → 0.9
  * `successor`   同一场会话里紧接在后面，且共享实体 → 0.6
  * `same_thread` 同月、同任务类型、实体重叠 ≥2（弱边，只用来串线索）→ 0.2

**红线**
========
1. **时间不倒流**：`to_id` 必须早于或等于 `from_id`，写入前校验。
2. **不接检索**：L1 只管建数据，检索一行都不改（验收就是这么定的）。
3. **绝不影响写入**：`link_many` 抛任何异常都必须被调用方吞掉。
4. 边可以有多个候选，但每条只保留**最强的前 N 条**（默认 3），避免变成全连接图。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re

__all__ = ["ensure_table", "link_many", "link_one", "neighbors", "stats", "backfill"]

_MAX_LINKS = 3
# 指代词：摘要里出现这些词，说明它在**转述**前面的内容。
# ⚠️ 只收"明确指向前面某条"的词。「继续」「再说」这类不收 ——
#    它们常常只是行文语气（实测把它们算进去，explicit 里混进一堆假阳）。
_REFER_WORDS = ("上次", "刚才", "之前那个", "上次那个", "接着", "上面说的",
                "前面说的", "刚才那个", "那个东西", "那次")


# ---------------------------------------------------------------- 基础设施
def _ensure(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS citations (
          from_id    TEXT NOT NULL,          -- 引用方（较新）
          to_id      TEXT NOT NULL,          -- 被引用方（较早）
          kind       TEXT NOT NULL,          -- explicit | successor | same_thread
          confidence REAL DEFAULT 0.5,
          evidence   TEXT,                   -- 抽取依据（共现实体 / 指代词）
          created_at TEXT,
          PRIMARY KEY (from_id, to_id, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_cit_from ON citations(from_id);
        CREATE INDEX IF NOT EXISTS idx_cit_to   ON citations(to_id);
    """)


def ensure_table(conn=None) -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    own = conn is None
    conn = conn or get_conn()
    try:
        _ensure(conn)
        n = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        return {"ok": True, "rows": n}
    finally:
        if own:
            conn.close()


def _parse(raw) -> list:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, str)]
    try:
        val = json.loads(raw or "[]")
        return [x for x in val if isinstance(x, str)]
    except Exception:
        return []


def _key(text: str) -> str:
    """与 collocate 同一口径：归一 + 抹分隔符（`书房 OS` == `书房OS`）。"""
    from services.B8_catalog import normalize_term
    return re.sub(r"[\s\-_·、/]+", "", normalize_term(text))


def _day(ts: str) -> str:
    return (ts or "")[:10]


def _earlier(a: dict, b: dict) -> bool:
    """a 是否严格早于 b。

    ⚠️ 顺序是 **source_date → created_at → memory_id**，不能反过来。
    理由（实测踩到）：`created_at` 是**提炼时间**，`source_date` 是**事情发生的时间**；
    补灌历史时，晚提炼的条目 created_at 更晚但 source_date 更早 —— 用 created_at
    排序会造出「后发生的引用了先发生的」这种时间倒流的边（L1 红线 1）。
    """
    ka = (_day(a.get("source_date")), a.get("created_at") or "", a.get("memory_id") or "")
    kb = (_day(b.get("source_date")), b.get("created_at") or "", b.get("memory_id") or "")
    return ka < kb


# ---------------------------------------------------------------- 打边
def _rows(conn) -> list[dict]:
    out = []
    for mid, summary, task_type, sdate, created, sess, path, ents in conn.execute(
            "SELECT memory_id, summary, task_type, source_date, created_at, "
            "session_id, content_path, core_entities FROM memories"):
        # ⚠️ 2026-11-01 实测：全库 `session_id` **一条都没有**（906/906 为空）。
        #   冷库是按天一个 `.jsonl`，一天之内就是一个连续对话流 ——
        #   所以在 session_id 缺失时，用 `content_path` 当会话键是可解释的替代
        #   （54 个文件 = 54 段会话流）。这条替代必须写在注释里，
        #   否则下一个人会以为"同一 session"是按真实会话分的。
        out.append({"memory_id": mid, "summary": summary or "",
                    "task_type": task_type or "", "source_date": sdate or "",
                    "created_at": created or "",
                    "session_key": (sess or path or ""),
                    "entities": _parse(ents)})
    return out


def _score(prev: dict, cur: dict) -> tuple | None:
    """返回 (kind, confidence, evidence) 或 None。"""
    pe = {_key(e) for e in prev["entities"] if e}
    ce = {_key(e) for e in cur["entities"] if e}
    shared = pe & ce
    if not shared:
        return None
    same_session = bool(prev["session_key"]) and prev["session_key"] == cur["session_key"]
    same_month = _day(prev["source_date"])[:7] == _day(cur["source_date"])[:7]
    same_task = prev["task_type"] == cur["task_type"]
    refers = any(w in cur["summary"] for w in _REFER_WORDS)
    ev = "共现:" + ",".join(sorted(shared)[:3])

    if same_session and refers:
        return "explicit", 0.9, ev + "|指代:" + next(
            (w for w in _REFER_WORDS if w in cur["summary"]), "")
    if same_session:
        return "successor", 0.6, ev
    if len(shared) >= 2 and same_task and same_month:
        return "same_thread", 0.2, ev
    return None


def _edges_for(cur: dict, pool: list[dict], max_links: int) -> list[tuple]:
    """给当前条目挑前驱边。

    2026-11-01 第一版把「同会话的所有前驱」都连上 → 当天 9 条就长出 3×9 条边，
    全库 840 条 successor，**等于把一天之内所有条目两两连成一张网** ——
    那不是谱系，是噪音。改成：

      * `explicit`：最多 2 条（它自己承认了，是强证据，允许多个来源）；
      * `successor`：**只连最近的那一条**前驱 —— 谱系要的是一条链，不是一张网；
      * `same_thread`：最多 2 条弱边，用来跨天串线索。
    """
    explicit, successor, weak = [], [], []
    for prev in pool:
        if prev["memory_id"] == cur["memory_id"]:
            continue
        if not _earlier(prev, cur):          # 红线 1：时间不能倒流
            continue
        got = _score(prev, cur)
        if not got:
            continue
        kind, conf, ev = got
        key = (prev.get("created_at") or prev.get("source_date") or "",
               prev["memory_id"])
        row = (key[0], prev["memory_id"], kind, conf, ev)
        if kind == "explicit":
            explicit.append(row)
        elif kind == "successor":
            successor.append(row)
        else:
            weak.append(row)

    out = []
    for group, keep in ((explicit, 2), (successor, 1), (weak, 2)):
        group.sort(key=lambda x: (x[0], x[1]))   # 按时间，取最近的
        out += group[-keep:]
    out.sort(key=lambda x: (-x[3], x[0]))
    return [(r[1], r[2], r[3], r[4]) for r in out[:max_links]]


def link_many(memories: list[dict] | None = None, max_links: int = _MAX_LINKS,
              conn=None) -> dict:
    """给 `memories` 里的每一条打「谁引用了谁」的边（幂等）。

    `memories=None` = 全库重建（backfill 用）。传入列表 = 只处理这一批（写入钩子用）。

    ⚠️ 调用方**必须**用 try/except 包住：引文是旁路，坏了不能让写入失败。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    own = conn is None
    conn = conn or get_conn()
    stats = {"processed": 0, "edges": 0, "by_kind": {}, "skipped_no_time": 0}
    try:
        _ensure(conn)
        pool = _rows(conn)
        targets = pool if memories is None else [
            m for m in pool if m["memory_id"] in {x.get("memory_id") for x in memories}]
        if not targets:
            return stats
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for cur in targets:
            stats["processed"] += 1
            edges = _edges_for(cur, pool, max_links)
            for to_id, kind, conf, ev in edges:
                conn.execute(
                    "INSERT OR REPLACE INTO citations"
                    "(from_id, to_id, kind, confidence, evidence, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (cur["memory_id"], to_id, kind, conf, ev, now))
                stats["edges"] += 1
                stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        return stats
    finally:
        if own:
            conn.close()


def link_one(memory_id: str, max_links: int = _MAX_LINKS) -> dict:
    """单条打边（写入钩子用）。失败由调用方吞掉。"""
    return link_many([{"memory_id": memory_id}], max_links=max_links)


# ---------------------------------------------------------------- 查边
def neighbors(memory_id: str, direction: str = "both", limit: int = 20) -> dict:
    """查一条记忆的引文邻居：谁引用了它（下游）、它引用了谁（上游）。

    Args:
        direction: `both` | `out`（它引用了谁）| `in`（谁引用了它）
    Returns:
        `{ok, memory_id, out[], in[], total}`；每条带 `kind/confidence/evidence`
        以及对方那条的 `summary/source_date`，方便直接读。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    memory_id = (memory_id or "").strip()
    if not memory_id:
        return {"ok": False, "error": "memory_id 不能为空"}
    direction = (direction or "both").lower()
    conn = get_conn()
    try:
        _ensure(conn)
        out, inc = [], []
        if direction in ("both", "out"):
            for to_id, kind, conf, ev in conn.execute(
                    "SELECT to_id, kind, confidence, evidence FROM citations "
                    "WHERE from_id=? ORDER BY confidence DESC, to_id LIMIT ?",
                    (memory_id, int(limit))):
                out.append(_decorate(conn, to_id, kind, conf, ev))
        if direction in ("both", "in"):
            for from_id, kind, conf, ev in conn.execute(
                    "SELECT from_id, kind, confidence, evidence FROM citations "
                    "WHERE to_id=? ORDER BY confidence DESC, from_id LIMIT ?",
                    (memory_id, int(limit))):
                inc.append(_decorate(conn, from_id, kind, conf, ev))
        me = conn.execute("SELECT summary, source_date FROM memories WHERE memory_id=?",
                          (memory_id,)).fetchone()
        return {"ok": True, "memory_id": memory_id,
                "summary": (me[0] if me else "") or "",
                "source_date": (me[1] if me else "") or "",
                "out": out, "in": inc, "total": len(out) + len(inc)}
    finally:
        conn.close()


def _decorate(conn, mid: str, kind: str, conf: float, ev: str) -> dict:
    row = conn.execute("SELECT summary, source_date, task_type FROM memories "
                       "WHERE memory_id=?", (mid,)).fetchone()
    return {"memory_id": mid, "kind": kind, "confidence": round(float(conf or 0), 2),
            "evidence": ev or "",
            "summary": (row[0] if row else "") or "",
            "source_date": (row[1] if row else "") or "",
            "task_type": (row[2] if row else "") or ""}


def edges_for_many(memory_ids: list[str], limit: int = 20) -> dict:
    """**批量**取引文邻居 —— 一次查询，给 L3 著录导出用。

    为什么必须有这个：L3 要对「一个主题的全部条目」逐条取邻居，
    如果每条各调一次 `neighbors()`，41 条就是 41 次连接往返 ——
    在 453 条的大集合上会直接卡住。这里用两条 `IN (...)` 查询拿全部。

    Returns: `{memory_id: {"out": [...], "in": [...]}}`，没有边的 id 也会出现（空列表），
    这样调用方不用 `get` 兜底。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    ids = [i for i in dict.fromkeys(memory_ids or []) if i]
    out = {mid: {"out": [], "in": []} for mid in ids}
    if not ids:
        return out
    conn = get_conn()
    try:
        _ensure(conn)
        ph = ",".join("?" * len(ids))
        rows = list(conn.execute(
            f"SELECT from_id, to_id, kind, confidence, evidence FROM citations "
            f"WHERE from_id IN ({ph}) OR to_id IN ({ph})", ids + ids))
        # 一次把涉及到的对方条目查出来，避免逐条回表
        other = set()
        for r in rows:
            other.add(r[0] if r[0] not in out else r[1])
        meta = {}
        if other:
            oph = ",".join("?" * len(other))
            for mid, summary, sdate, task in conn.execute(
                    f"SELECT memory_id, summary, source_date, task_type FROM memories "
                    f"WHERE memory_id IN ({oph})", list(other)):
                meta[mid] = (summary or "", sdate or "", task or "")

        def pack(mid, kind, conf, ev):
            s, d, t = meta.get(mid, ("", "", ""))
            return {"memory_id": mid, "kind": kind,
                    "confidence": round(float(conf or 0), 2), "evidence": ev or "",
                    "summary": s, "source_date": d, "task_type": t}

        for from_id, to_id, kind, conf, ev in rows:
            if from_id in out:
                out[from_id]["out"].append(pack(to_id, kind, conf, ev))
            if to_id in out:
                out[to_id]["in"].append(pack(from_id, kind, conf, ev))
        for mid in out:
            out[mid]["out"].sort(key=lambda x: -x["confidence"])
            out[mid]["in"].sort(key=lambda x: -x["confidence"])
            out[mid]["out"] = out[mid]["out"][:limit]
            out[mid]["in"] = out[mid]["in"][:limit]
        return out
    finally:
        conn.close()


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        total = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM citations GROUP BY kind").fetchall())
        covered = conn.execute(
            "SELECT COUNT(DISTINCT from_id) FROM citations").fetchone()[0]
        mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {"rows": total, "by_kind": by_kind, "memories": mem,
                "covered_memories": covered,
                "coverage": round(covered / mem, 4) if mem else 0}
    finally:
        conn.close()


def backfill(dry_run: bool = True, max_links: int = _MAX_LINKS) -> dict:
    """全库重建。`dry_run=True` 只算不写（默认，防手滑）。"""
    if dry_run:
        from services.B3_header_engine.sqlite_store import get_conn
        conn = get_conn()
        try:
            pool = _rows(conn)
            n = 0
            kinds = {}
            for cur in pool:
                for _to, kind, _c, _e in _edges_for(cur, pool, max_links):
                    n += 1
                    kinds[kind] = kinds.get(kind, 0) + 1
            return {"dry_run": True, "memories": len(pool), "would_write": n,
                    "by_kind": kinds}
        finally:
            conn.close()
    return {"dry_run": False, **link_many(None, max_links=max_links)}
