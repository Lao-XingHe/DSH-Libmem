# -*- coding: utf-8 -*-
"""C1 · 文档编目（kb_docs）—— 2026-11-03

**为什么必须另做一份档案**
========================
> 「原文档可以不动，但编目和载体要重新做一份。」

载体（原文件）只有一个名字，而名字往往**不是题名**：
`《万情补天录》.docx` 是一本小说大纲，但文件名里既没有"大纲"也没有"修仙" ——
按体裁搜「大纲」永远搜不到它。这正是用户说的「人的记忆不精确，只记得大概名字」。

所以这里按图书馆的做法分三层：

```
载体 carrier   原文件（不动它一个字节）
款目 record    题名 / 体裁 / 主题词 / 所属项目 / 大小 / 修改时间   ← 本模块产出
索引 index     正文全文（kb_indexer，已有）
```

检索时**先查款目**（题名、体裁、主题词都能命中），再查正文 —— 两条路的命中分开标注
`matched_by: title | content`，不许混在一起假装是同一种证据。

**题名规则（刻意保守）**
- 默认：文件名去扩展名（`《万情补天录》`）
- 去掉模型前缀（`ds` / `gemin` / `千问` / `豆包`）—— 那是作者，不是题名
- **不编造**：文件名里没有的体裁词，只写进 `genre` 字段，不改题名
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

__all__ = ["ensure_table", "build", "lookup", "stats", "infer"]

# 体裁词 → 归类（顺序即优先级）
_GENRE_HINTS = [
    ("大纲", ["大纲", "梗概", "故事线", "分集", "章纲"]),
    ("设定集", ["设定集", "设定", "世界观", "人物表", "体系"]),
    ("拆解笔记", ["拆解", "设定汇总", "分析报告", "总结"]),
    ("方法论文", ["方法论", "七步", "方法", "框架", "模型"]),
    ("论文", ["论文", "摘要", "实验", "卷积"]),
    ("代码笔记", ["README", "架构", "接口", "配置"]),
]
_MODEL_PREFIX = re.compile(r"^(ds|gemin|gemini|千问|豆包|豆包版|chatgpt|claude)[-_ ]*", re.I)


def _infer_project(path: str, kb_dir: str) -> str:
    rel = os.path.relpath(path, kb_dir)
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else "根目录"


def _clean_title(name: str) -> str:
    stem = os.path.splitext(name)[0].strip()
    stem = _MODEL_PREFIX.sub("", stem).strip(" -_")
    # 去掉模型前缀后常留下开头的冒号/顿号（实测 `DS：突破…` → `：突破…`）
    stem = re.sub(r"^[\s:：、\-—_]+", "", stem).strip()
    return stem or name


def infer(name: str, head: str, kb_dir: str, path: str) -> dict:
    """给一份文档推断款目字段。`head` 是正文前几百字。"""
    title = _clean_title(name)
    hay = (name + " " + head)[:2000]
    # 体裁只取**最强的一个**：一份文档归三个体裁等于没归
    genre = "未分类"
    for g, words in _GENRE_HINTS:
        if any(w in hay for w in words):
            genre = g
            break
    # 主题词：从文件名里的书名号 + 正文小标题里捞
    subjects = []
    for m in re.findall(r"《([^》]{2,20})》", name):
        subjects.append(m)
    for m in re.findall(r"^#?\s*([\u4e00-\u9fa5]{2,8})[:：]", head[:600], re.M)[:5]:
        subjects.append(m)
    # 文字里出现的高频体裁/领域词（很轻，只做提示）
    for word in ("修仙", "赛博", "悬疑", "图书馆", "记忆", "检索", "评测", "小说"):
        if word in hay:
            subjects.append(word)
    seen, uniq = set(), []
    for s in subjects:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return {"title": title,
            "genre": genre,
            "subjects": uniq[:8],
            "project": _infer_project(path, kb_dir)}


def _ensure(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kb_docs (
          doc_id    TEXT PRIMARY KEY,      -- 载体路径的哈希
          path      TEXT NOT NULL,         -- 载体（原文件，不动）
          title     TEXT,                  -- 题名
          genre     TEXT,                  -- 体裁
          subjects  TEXT,                  -- JSON 数组
          project   TEXT,                  -- 所属馆（顶层目录）
          size_kb   REAL,
          mtime     TEXT,
          chunks    INTEGER DEFAULT 0,
          built_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_kbdocs_title ON kb_docs(title);
        CREATE INDEX IF NOT EXISTS idx_kbdocs_project ON kb_docs(project);
    """)


def build(dry_run: bool = True, refresh: bool = False) -> dict:
    """扫知识库目录，生成/更新款目表。**只读原文件**，写的是 `kb_docs`。

    ⚠️ 2026-11-03 修：第一版每次 `DELETE` 全表重写，于是**大模型补的主题词/摘要
    会被下一次 reindex 冲掉**（"入库即编目"变成"入库即重编"）。
    现在默认**只补新款目**（已存在的 doc_id 原样保留），要整表重算才传 `refresh=True`。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    from services.C1_knowledge.kb_indexer import (_get_kb_dir, read_any_file,
                                                  chunk_text)

    kb_dir = _get_kb_dir()
    conn = get_conn()
    rows, skipped = [], []
    try:
        _ensure(conn)
        have = {r[0] for r in conn.execute("SELECT doc_id FROM kb_docs")} if not refresh else set()
        for root, _dirs, names in os.walk(kb_dir):
            for fname in sorted(names):
                fp = os.path.join(root, fname)
                try:
                    text = read_any_file(fp)
                except Exception as exc:  # noqa: BLE001 - 读不了的进 skipped，不静默
                    skipped.append({"name": fname, "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                info = infer(fname, (text or "")[:800], kb_dir, fp)
                doc_id = hashlib.md5(fp.encode("utf-8")).hexdigest()[:16]
                if doc_id in have:
                    continue          # 已编过：保留（可能已被大模型补过主题词/摘要）
                rows.append({
                    "doc_id": doc_id,
                    "path": fp,
                    "title": info["title"],
                    "genre": info["genre"],
                    "subjects": json.dumps(info["subjects"], ensure_ascii=False),
                    "project": info["project"],
                    "size_kb": round(os.path.getsize(fp) / 1024, 1),
                    "mtime": datetime.datetime.fromtimestamp(
                        os.path.getmtime(fp)).isoformat(timespec="seconds"),
                    "chunks": len(chunk_text(text or "")),
                })
        out = {"dry_run": dry_run, "kb_dir": kb_dir, "docs": len(rows),
               "skipped": len(skipped)}
        if dry_run:
            out["sample"] = [{k: r[k] for k in ("title", "genre", "project")} for r in rows[:6]]
            return out
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute("BEGIN")
        if refresh:
            conn.execute("DELETE FROM kb_docs")
        conn.executemany(
            "INSERT OR REPLACE INTO kb_docs(doc_id, path, title, genre, subjects, "
            "project, size_kb, mtime, chunks, built_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(r["doc_id"], r["path"], r["title"], r["genre"], r["subjects"],
              r["project"], r["size_kb"], r["mtime"], r["chunks"], now) for r in rows])
        conn.execute("COMMIT")
        out["written"] = conn.execute("SELECT COUNT(*) FROM kb_docs").fetchone()[0]
        return out
    finally:
        conn.close()


def lookup(query: str, top_k: int = 5) -> list:
    """**先查款目**：题名 / 体裁 / 主题词 / 项目 的模糊匹配。

    人的记忆不精确 —— 记得"那个修仙大纲"就够了，不必记得题名全称。
    所以这里做的是 `LIKE %词%` 的多字段 OR，再按"题名命中优先"排序。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    q = (query or "").strip()
    if not q:
        return []
    conn = get_conn()
    try:
        _ensure(conn)
        words = [w for w in re.split(r"[\s,，]+", q) if len(w) >= 2] or [q]
        scored: dict[str, float] = {}
        for w in words:
            for r in conn.execute(
                    "SELECT path, title, genre, subjects, project, chunks FROM kb_docs "
                    "WHERE title LIKE ? OR genre LIKE ? OR subjects LIKE ? OR project LIKE ?",
                    ("%" + w + "%", "%" + w + "%", "%" + w + "%", "%" + w + "%")):
                # 2026-11-03 调权：**题名 > 主题词 > 体裁 > 项目**。
                # 依据很直白：人报馆藏时先报书名（"那个万情补天录"），
                # 其次说主题（"修仙的"），最后才是类别（"大纲"）。
                # 旧权重 title=3 / genre=2 / 其它=1 的后果：一份"体裁恰好是大纲、
                # 正文里提过修仙"的方法论文，能压过真正的那本修仙大纲。
                title, genre = r[1] or "", r[2] or ""
                subs, proj = r[3] or "", r[4] or ""
                gain = 0.0
                if w in title:
                    gain += 6.0
                elif w in subs:
                    gain += 3.0
                if genre == w:            # 用户直接说了体裁词（大纲/设定集…）
                    gain += 4.0
                elif w in genre:
                    gain += 2.0
                if w in proj:
                    gain += 1.0
                if gain == 0.0:
                    gain = 0.5           # 只是四个字段之一命中，给个底分
                scored[r[0]] = scored.get(r[0], 0.0) + gain
        out = []
        for path, score in sorted(scored.items(), key=lambda kv: -kv[1])[:top_k]:
            r = conn.execute(
                "SELECT title, genre, subjects, project, chunks FROM kb_docs WHERE path=?",
                (path,)).fetchone()
            if not r:
                continue
            out.append({"path": path, "title": r[0], "genre": r[1],
                        "subjects": json.loads(r[2] or "[]"), "project": r[3],
                        "chunks": r[4], "catalog_score": round(score, 2)})
        return out
    finally:
        conn.close()


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        _ensure(conn)
        total = conn.execute("SELECT COUNT(*) FROM kb_docs").fetchone()[0]
        by_genre = dict(conn.execute(
            "SELECT genre, COUNT(*) FROM kb_docs GROUP BY genre").fetchall())
        by_project = dict(conn.execute(
            "SELECT project, COUNT(*) FROM kb_docs GROUP BY project").fetchall())
        return {"docs": total, "by_genre": by_genre, "by_project": by_project}
    finally:
        conn.close()
