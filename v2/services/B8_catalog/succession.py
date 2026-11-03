# -*- coding: utf-8 -*-
"""B8 · 沿革（succession）—— 2026-11-03，让"现在是多少"能答出来

**为什么必须做它**
================
库里最早的形态是"很多条各说各话"：问「记忆库现在有多少条」，会同时返回 267、902、
774 这些历史数字，**没有一条知道自己过期了**。那是 RAG 的行为，不是记忆的行为。

**沿革 = 给每条挂上「有效期」**
============================
::

    valid_from    从什么日期起这句话是对的
    valid_to      到哪天为止它还成立（被后一条推翻那天）
    superseded_by 被谁推翻
    is_current    现在是否仍有效（每个作品只该有一条为真）

**刻意的保守：只在"看得出被改写"时才连边**
======================================
链错比不连更糟 —— 连错一条，"现在的状态"就永远答错。所以只认两种明确信号：
  1. 摘要里出现**版本号**（`v1.2` / `V1.3.4` / `第 3 版`）；
  2. 摘要里出现**改写动词**（改成 / 改为 / 上调 / 下调 / 替换为 / 现在用）。
其余保持 `is_current=True`、`superseded_by=NULL` —— 宁可不连。
"""
from __future__ import annotations

import datetime
import json
import re

__all__ = ["build", "state", "stats", "ensure_table"]

_VERSION = re.compile(r"(?:^|[^a-z])(v|V)\d+(?:\.\d+){0,3}|第[一二三四五六七八九十\d]+版|版本号")
_REWRITE = ("改成", "改为", "上调", "下调", "替换为", "现在用", "已改为", "调整为", "更新为")


def _ensure(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS succession (
          memory_id    TEXT PRIMARY KEY,
          work         TEXT NOT NULL,      -- 作品键（暂用该条的主主题词）
          valid_from   TEXT,
          valid_to     TEXT,
          superseded_by TEXT,
          is_current   INTEGER DEFAULT 1,
          built_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_succ_work ON succession(work);
    """)


def _work_of(terms_json: str) -> str:
    try:
        ts = json.loads(terms_json or "[]")
        return (ts[0] if ts else "") or ""
    except Exception:
        return ""


def build(dry_run: bool = True) -> dict:
    """算沿革。`dry_run=True` 只统计不写。"""
    from services.B3_header_engine.sqlite_store import get_conn

    conn = get_conn()
    try:
        _ensure(conn)
        rows = list(conn.execute(
            "SELECT d.item_id, d.terms, m.summary, m.source_date "
            "FROM descriptions d JOIN memories m ON m.memory_id = d.item_id "
            "ORDER BY m.source_date, d.item_id"))
        by_work: dict[str, list] = {}
        for iid, terms, summary, sdate in rows:
            w = _work_of(terms)
            if not w:
                continue
            by_work.setdefault(w, []).append(
                {"id": iid, "summary": summary or "", "date": sdate or ""})

        chains, versions = 0, 0
        plan = []
        for work, items in by_work.items():
            if len(items) < 2:
                continue
            marked = [x for x in items
                      if _VERSION.search(x["summary"]) or any(v in x["summary"] for v in _REWRITE)]
            versions += len(marked)
            if len(marked) < 2:
                continue
            chains += 1
            marked.sort(key=lambda x: (x["date"], x["id"]))
            for i, cur in enumerate(marked):
                nxt = marked[i + 1] if i + 1 < len(marked) else None
                plan.append({
                    "id": cur["id"], "work": work,
                    "valid_from": cur["date"],
                    "valid_to": (nxt["date"] if nxt else None),
                    "superseded_by": (nxt["id"] if nxt else None),
                    "is_current": 0 if nxt else 1,
                })

        out = {"dry_run": dry_run, "works": len(by_work), "versioned": versions,
               "chains": chains, "rows": len(plan)}
        if dry_run:
            out["sample"] = [{"work": p["work"], "from": p["valid_from"],
                              "to": p["valid_to"]} for p in plan[:5]]
            return out

        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM succession")
        # 所有条目先落一行（默认 current、无前驱）—— 这样 state() 永远有东西可查
        conn.executemany(
            "INSERT OR REPLACE INTO succession(memory_id, work, valid_from, valid_to, "
            "superseded_by, is_current, built_at) VALUES(?,?,?,?,?,?,?)",
            [(iid, _work_of(terms), sdate, None, None, 1, now)
             for iid, terms, _s, sdate in rows if _work_of(terms)])
        for p in plan:
            conn.execute(
                "UPDATE succession SET valid_to=?, superseded_by=?, is_current=? "
                "WHERE memory_id=?",
                (p["valid_to"], p["superseded_by"], p["is_current"], p["id"]))

        # ★ 2026-11-03 修：**一个作品只能有一条 current**。
        #   第一版只把"带版本号/改写动词"的那些收进链，于是同一作品下
        #   没被收进链的条目仍然是 is_current=1 —— 实测「自动记录」出现两条"至今"。
        #   现在的规则：同一作品键下，只有**日期最新**的那条为 current；
        #   其余置 0，但 valid_to 留空（没有明确的推翻日期，不编造）。
        latest: dict[str, tuple] = {}
        for mid, w, vf in conn.execute(
                "SELECT memory_id, work, COALESCE(valid_from,'') FROM succession"):
            cur = latest.get(w)
            if cur is None or (vf, mid) > cur:
                latest[w] = (vf, mid)
        keep = {v[1] for v in latest.values()}
        all_ids = [r[0] for r in conn.execute("SELECT memory_id FROM succession")]
        stale = [(m,) for m in all_ids if m not in keep]
        if stale:
            conn.executemany(
                "UPDATE succession SET is_current=0 WHERE memory_id=?", stale)
        conn.execute("COMMIT")
        out["written"] = conn.execute("SELECT COUNT(*) FROM succession").fetchone()[0]
        return out
    finally:
        conn.close()


def state(work: str, limit: int = 20) -> dict:
    """**这个作品现在的状态**：当前有效的那条 + 沿革 + 证据。

    这是"信息管理中心"该出的东西 —— 不是一堆清单，是一句话的现状加它怎么来的。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    work = (work or "").strip()
    if not work:
        return {"ok": False, "error": "work 不能为空"}
    conn = get_conn()
    try:
        _ensure(conn)
        rows = list(conn.execute(
            "SELECT s.memory_id, s.work, s.valid_from, s.valid_to, s.superseded_by, "
            "s.is_current, m.summary, m.task_type, m.heat, m.has_l3 "
            "FROM succession s JOIN memories m ON m.memory_id = s.memory_id "
            "WHERE s.work = ? ORDER BY s.valid_from DESC LIMIT ?", (work, int(limit))))
        if not rows:
            # 退一步：没有沿革记录就去主题词里找（用户可能给的是别名）
            hit = conn.execute(
                "SELECT work FROM succession WHERE work LIKE ? LIMIT 1",
                ("%" + work + "%",)).fetchone()
            if not hit:
                return {"ok": True, "work": work, "found": False,
                        "hint": "沿革表里没有这个作品键。可以先用 collocate / authority 确认真名。"}
            rows = list(conn.execute(
                "SELECT s.memory_id, s.work, s.valid_from, s.valid_to, s.superseded_by, "
                "s.is_current, m.summary, m.task_type, m.heat, m.has_l3 "
                "FROM succession s JOIN memories m ON m.memory_id = s.memory_id "
                "WHERE s.work = ? ORDER BY s.valid_from DESC LIMIT ?", (hit[0], int(limit))))

        items = [{
            "memory_id": r[0], "valid_from": r[2] or "", "valid_to": r[3] or "",
            "superseded_by": r[4] or "", "is_current": bool(r[5]),
            "summary": r[6] or "", "task_type": r[7] or "",
            "heat": round(float(r[8] or 0), 2), "has_l3": int(r[9] or 0),
        } for r in rows]
        current = next((x for x in items if x["is_current"]), None)
        # 诚实标注：被降级但**没有明确继承者**的条目（同一作品键下只是更早的，
        # 不代表它说的东西错了）。不标出来，"历史"会被误读成"已被推翻"。
        uncertain = [x["memory_id"] for x in items
                     if not x["is_current"] and not x["superseded_by"]]
        return {"ok": True, "work": rows[0][1], "found": True,
                "current": current, "history": items,
                "count": len(items), "uncertain": uncertain,
                "note": "current 是现在仍有效的那条；history 按时间倒序；"
                        "`uncertain` 里的条目只是同键更早，**没有确认被取代**。"}
    finally:
        conn.close()


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        total = conn.execute("SELECT COUNT(*) FROM succession").fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM succession WHERE is_current=0").fetchone()[0]
        works = conn.execute(
            "SELECT COUNT(DISTINCT work) FROM succession").fetchone()[0]
        multi = conn.execute(
            "SELECT COUNT(*) FROM (SELECT work FROM succession WHERE is_current=0 "
            "GROUP BY work)").fetchone()[0]
        return {"rows": total, "works": works, "superseded": superseded,
                "works_with_history": multi}
    finally:
        conn.close()
