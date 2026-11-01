# -*- coding: utf-8 -*-
"""B8 · 参见边（see_also）—— 2026-11-01，L4 临时版

**依据**：《图书馆学导论》p.46，Svenonius 给目录加的**导航功能** ——
「通过书目数据间的**等同、相关、等级**关系，从已知作品导向其他作品」。

**临时版只做「相关」这一类**（等级/等同留给正式版）：

| 边 | 来源 | 本版 |
|---|---|---|
| 等同（同义） | `terms.aliases_json`（已有） | 直接**输出**，不建边（同一条 term 的别名） |
| **相关** | `mem_terms` 共现 → **PMI** | ✅ 本版要算的就是它 |
| 等级（上下位） | 分类层级 | ❌ 留给正式版 |

**为什么用 PMI 而不是"共现次数"**：高频词会跟所有词共现（"记忆"和什么都一起出现），
按次数排等于把泛词推到第一。PMI 会**惩罚高频**，把"只在这两个词之间才出现的关联"顶上来。

**为什么它必须嵌进 L2/L3 而不是新开一个工具**：
参见只有在「你正在看某个主题时」才有意义。单独一个 `libmem_seealso` 工具
又要靠人记得调 —— 那就退回了"三个平行抽屉"的老问题。
"""
from __future__ import annotations

import json
import math

__all__ = ["build", "see_also", "stats", "ensure_table"]

_MAX_PER_TERM = 5
_MIN_COOC = 2


def _ensure(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS term_edges (
          from_term  TEXT NOT NULL,
          to_term    TEXT NOT NULL,
          kind       TEXT NOT NULL,          -- related | broader | narrower
          weight     REAL DEFAULT 0.0,       -- PMI
          cooc       INTEGER DEFAULT 0,      -- 共现次数
          created_at TEXT,
          PRIMARY KEY (from_term, to_term, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_tedge_from ON term_edges(from_term);
    """)


def ensure_table(conn=None) -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    own = conn is None
    conn = conn or get_conn()
    try:
        _ensure(conn)
        return {"ok": True, "rows": conn.execute(
            "SELECT COUNT(*) FROM term_edges").fetchone()[0]}
    finally:
        if own:
            conn.close()


def build(dry_run: bool = True, max_per_term: int = _MAX_PER_TERM) -> dict:
    """算相关边（PMI）。`dry_run=True` 只统计不写。"""
    import datetime
    from services.B3_header_engine.sqlite_store import get_conn

    conn = get_conn()
    try:
        _ensure(conn)
        # 词 → 出现它的条目集合
        term_items: dict[str, set] = {}
        for item_id, canon in conn.execute(
                "SELECT mt.item_id, t.canonical FROM mem_terms mt "
                "JOIN terms t ON t.term_id = mt.term_id"):
            if canon:
                term_items.setdefault(canon, set()).add(item_id)
        n_items = conn.execute("SELECT COUNT(DISTINCT item_id) FROM mem_terms").fetchone()[0] or 1

        # 条目 → 它带的词（用来枚举共现对）
        item_terms: dict[str, list] = {}
        for item_id, canon in conn.execute(
                "SELECT mt.item_id, t.canonical FROM mem_terms mt "
                "JOIN terms t ON t.term_id = mt.term_id"):
            if canon:
                item_terms.setdefault(item_id, []).append(canon)

        cooc: dict[tuple, int] = {}
        for terms in item_terms.values():
            uniq = sorted(set(terms))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    cooc[(uniq[i], uniq[j])] = cooc.get((uniq[i], uniq[j]), 0) + 1

        edges: list[tuple] = []
        for (a, b), c in cooc.items():
            if c < _MIN_COOC:
                continue
            da, db = len(term_items.get(a, ())), len(term_items.get(b, ()))
            if not da or not db:
                continue
            # PMI（自然对数），再按共现次数做一点缩放，避免只共现 2 次的高分霸榜
            pmi = math.log((c * n_items) / (da * db)) if c * n_items > 0 else 0.0
            weight = round(pmi * math.log1p(c), 3)
            edges.append((a, b, weight, c))
            edges.append((b, a, weight, c))

        # 每个词只留最强的 max_per_term 条
        by_term: dict[str, list] = {}
        for a, b, w, c in edges:
            by_term.setdefault(a, []).append((w, c, b))
        picked = []
        for a, lst in by_term.items():
            lst.sort(key=lambda x: (-x[0], -x[1]))
            for w, c, b in lst[:max_per_term]:
                picked.append((a, b, "related", w, c))

        out = {"dry_run": dry_run, "terms": len(term_items), "pairs": len(cooc),
               "edges": len(picked)}
        if dry_run:
            return out
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM term_edges WHERE kind='related'")
        conn.executemany(
            "INSERT OR REPLACE INTO term_edges"
            "(from_term, to_term, kind, weight, cooc, created_at) VALUES(?,?,?,?,?,?)",
            [(a, b, k, w, c, now) for a, b, k, w, c in picked])
        conn.execute("COMMIT")
        out["rows"] = conn.execute("SELECT COUNT(*) FROM term_edges").fetchone()[0]
        return out
    finally:
        conn.close()


def see_also(term: str, k: int = _MAX_PER_TERM) -> list:
    """某词的参见：相关词（PMI）+ 同义写法（别名）。取不到就是空列表。"""
    from services.B3_header_engine.sqlite_store import get_conn
    term = (term or "").strip()
    if not term:
        return []
    conn = get_conn()
    try:
        _ensure(conn)
        out = []
        # 同义：同一条 term 记录的其它写法
        row = conn.execute(
            "SELECT canonical, aliases_json FROM terms WHERE lower(canonical)=lower(?)",
            (term,)).fetchone()
        if row:
            try:
                aliases = [a for a in json.loads(row[1] or "[]") if a]
            except Exception:
                aliases = []
            for a in aliases[:k]:
                if a.strip().lower() != (row[0] or "").strip().lower():
                    out.append({"term": a, "kind": "synonym", "weight": 1.0, "cooc": 0})
        # 相关：PMI 边
        for to_term, weight, cooc in conn.execute(
                "SELECT to_term, weight, cooc FROM term_edges "
                "WHERE from_term=? AND kind='related' "
                "ORDER BY weight DESC, cooc DESC LIMIT ?", (term, k)):
            out.append({"term": to_term, "kind": "related",
                        "weight": round(float(weight), 2), "cooc": int(cooc)})
        return out[:k]
    finally:
        conn.close()


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        rows = conn.execute("SELECT COUNT(*) FROM term_edges").fetchone()[0]
        terms = conn.execute("SELECT COUNT(DISTINCT from_term) FROM term_edges").fetchone()[0]
        sample = [dict(zip(("from", "to", "weight", "cooc"), r)) for r in conn.execute(
            "SELECT from_term, to_term, weight, cooc FROM term_edges "
            "ORDER BY weight DESC LIMIT 5")]
        return {"rows": rows, "terms": terms, "sample": sample}
    finally:
        conn.close()
