# -*- coding: utf-8 -*-
"""B8 · 分类号与属分边（call_no / S-F）—— 2026-11-03，L6 + L4

**为什么是「分类号」而不是「类目排序」**
====================================
《图书馆学导论》p.45：排架是按**分类号**排的 —— 同号相邻、同子类更相近，
于是"浏览能逛出东西"。只排一级类目的先后，那是**抽屉顺序**，不是排架。

**分类号长什么样**
================
::

    02          一级类（来自 data/shelf/general.yaml 的 order）
    02-01       二级类（sub_class 在该类内的序号）
    02-01-003   三级（主题词在该子类内的序号）

**顺带解决 L4 的上位/下位**
========================
分类号天然带层级 → 层级天然给出 属项(S, 上位) / 分项(F, 下位)：
- `向量召回` 的 S 是 `检索与索引`，F 是它下一层的具体词
- `检索与索引` 的 S 是 `软件与代码`

书里（第二章）说受控语言规范化有三件，第三件就是"语义网位置（上下隶属 + 相关）"。
C（相关）已经在 `see_also.py` 里用 PMI 做了；这个模块补的就是 **S / F**。
"""
from __future__ import annotations

import datetime
import json
import os
import re

__all__ = ["build", "call_no_of", "hierarchy", "stats"]

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "shelf")


def _class_order() -> list:
    import yaml
    try:
        with open(os.path.join(_DATA, "general.yaml"), "r", encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("order") or []
    except Exception:  # noqa: BLE001
        return []


def _clc_mapping() -> dict:
    """本馆类目 → 中图法号（来自 data/shelf/clc.yaml 的 `mapping`）。

    取不到就返回空 —— call_no 会退回纯数字序（02-08-023），行为退化但不崩。
    """
    import yaml
    try:
        with open(os.path.join(_DATA, "clc.yaml"), "r", encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("mapping") or {}
    except Exception:  # noqa: BLE001
        return {}


def _ensure(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS term_edges (
          from_term  TEXT NOT NULL,
          to_term    TEXT NOT NULL,
          kind       TEXT NOT NULL,
          weight     REAL DEFAULT 0.0,
          cooc       INTEGER DEFAULT 0,
          created_at TEXT,
          PRIMARY KEY (from_term, to_term, kind)
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(descriptions)")}
    if "call_no" not in cols:
        conn.execute("ALTER TABLE descriptions ADD COLUMN call_no TEXT")


def build(dry_run: bool = True) -> dict:
    """算分类号 + 写 S/F 边。`dry_run=True` 只统计。"""
    from services.B3_header_engine.sqlite_store import get_conn

    conn = get_conn()
    try:
        _ensure(conn)
        order = _class_order()
        rank = {c: i + 1 for i, c in enumerate(order)}

        rows = list(conn.execute(
            "SELECT item_id, class, sub_class, terms FROM descriptions"))
        if not rows:
            return {"dry_run": dry_run, "rows": 0, "note": "descriptions 是空的"}

        # 子类在该类内的序号（按出现次数降序，多的在前）
        from collections import Counter, defaultdict
        sub_count = defaultdict(Counter)
        for _iid, cls, sub, _t in rows:
            sub_count[cls][sub or ""] += 1
        sub_rank = {cls: {s: j + 1 for j, (s, _n) in enumerate(c.most_common())}
                    for cls, c in sub_count.items()}

        # 主题词在子类内的序号
        term_count = defaultdict(Counter)
        for _iid, cls, sub, t in rows:
            try:
                terms = json.loads(t or "[]")
            except Exception:
                terms = []
            for x in terms[:1]:
                term_count[(cls, sub or "")][x] += 1
        term_rank = {k: {x: j + 1 for j, (x, _n) in enumerate(c.most_common())}
                     for k, c in term_count.items()}

        updates, edges = [], []
        clc = _clc_mapping()
        for iid, cls, sub, t in rows:
            cr = rank.get(cls or "", len(order) + 1)
            # 2026-11-03：有中图法映射就用**真分类号**（TP31），否则退回两位类序（02）
            prefix = clc.get(cls or "") or "%02d" % cr
            sr = sub_rank.get(cls, {}).get(sub or "", 99)
            try:
                terms = json.loads(t or "[]")
            except Exception:
                terms = []
            tr = term_rank.get((cls, sub or ""), {}).get(terms[0] if terms else "", 999)
            call_no = ("%s-%02d-%03d" % (prefix, sr, tr)) if terms else ("%s-%02d" % (prefix, sr))
            updates.append((call_no, iid))
            # S/F：主题词 → 子类 → 类
            if terms:
                for x in terms[:3]:
                    edges.append((x, sub or cls, "S", 1.0, 0))          # 上位
                    edges.append((sub or cls, x, "F", 1.0, 0))          # 下位
            if sub:
                edges.append((sub, cls or "其他", "S", 1.0, 0))
                edges.append((cls or "其他", sub, "F", 1.0, 0))

        info = {"dry_run": dry_run, "rows": len(rows), "with_call_no": len(updates),
                "edges": len(set(edges)), "classes": len(order)}
        if dry_run:
            info["sample"] = [{"item": i, "call_no": c} for c, i in updates[:5]]
            return info

        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute("BEGIN")
        conn.executemany("UPDATE descriptions SET call_no=? WHERE item_id=?", updates)
        conn.execute("DELETE FROM term_edges WHERE kind IN ('S','F')")
        seen = set()
        for a, b, k, w, c in edges:
            if not a or not b or (a, b, k) in seen:
                continue
            seen.add((a, b, k))
            conn.execute(
                "INSERT OR REPLACE INTO term_edges"
                "(from_term,to_term,kind,weight,cooc,created_at) VALUES(?,?,?,?,?,?)",
                (a, b, k, w, c, now))
        conn.execute("COMMIT")
        info["written_edges"] = len(seen)
        info["updated_rows"] = len(updates)
        return info
    finally:
        conn.close()


def call_no_of(item_id: str) -> str:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT call_no FROM descriptions WHERE item_id=?",
                           (item_id,)).fetchone()
        return (row[0] if row else "") or ""
    finally:
        conn.close()


def hierarchy(term: str) -> dict:
    """某个词/类目的 上位(S) / 下位(F)。"""
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        up = [r[0] for r in conn.execute(
            "SELECT to_term FROM term_edges WHERE from_term=? AND kind='S'", (term,))]
        down = [r[0] for r in conn.execute(
            "SELECT to_term FROM term_edges WHERE from_term=? AND kind='F'", (term,))]
        return {"term": term, "broader": up[:10], "narrower": down[:10]}
    finally:
        conn.close()


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        with_no = conn.execute(
            "SELECT COUNT(*) FROM descriptions WHERE COALESCE(call_no,'')<>''").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0]
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM term_edges GROUP BY kind").fetchall())
        sample = [dict(zip(("item", "class", "sub", "call_no"), r)) for r in conn.execute(
            "SELECT item_id, class, sub_class, call_no FROM descriptions "
            "WHERE COALESCE(call_no,'')<>'' ORDER BY call_no LIMIT 5")]
        return {"descriptions": total, "with_call_no": with_no,
                "edges_by_kind": by_kind, "sample": sample}
    finally:
        conn.close()
