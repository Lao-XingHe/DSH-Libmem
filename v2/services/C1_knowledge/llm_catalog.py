# -*- coding: utf-8 -*-
"""C1 · 用本机小模型补编目（入库即编目的那一半）

**分工（用户定的）**：规则先行 → 本地 9B 补剩下的 → 大模型只定约束和验收。
这里就是「本地 9B 补剩下的」那一段，供 `reindex_knowledge()` 自动调用。

**为什么放服务里而不是只留脚本**：脚本只能人手跑；入库不编目，
就会出现「记得大概名字却搜不到」——那是用户最痛的一条。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

__all__ = ["enrich", "catalog_missing"]

BASE = os.environ.get("LIBMEM_LLM_BASE", "http://127.0.0.1:8089/v1")
MODEL = os.environ.get("LIBMEM_LLM_MODEL", "Qwen3.5-9B-Q4_K_M")

GENRES = ["大纲", "设定集", "论文", "拆解笔记", "方法论文", "代码笔记", "笔记", "其他"]

PROMPT = """你在给一个私人知识库做图书馆式的编目。读下面这份文档的开头，输出**一个 JSON 对象**，不要任何解释：

{{"genre": "从这些里选一个：{genres}", "subjects": ["3到5个具体主题词"], "abstract": "不超过30字的一句话"}}

要求：
- subjects 要具体（写"赛博修仙""高利贷骗局"，不要写"小说""内容"）
- genre 拿不准就选"其他"

文档名：{name}
开头：
{head}
"""


def _chat(prompt: str, timeout: int = 120) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 400,
        # ★ 本机 Qwen3.5 是**思考模型**：不关思考它会先写一大段 reasoning，
        #   400 token 全花在思考上、content 直接是空串（实测 finish_reason=length）。
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    genre = (obj.get("genre") or "").strip()
    if genre not in GENRES:
        genre = "其他"
    subs = [str(x).strip() for x in (obj.get("subjects") or []) if str(x).strip()][:5]
    return {"genre": genre, "subjects": subs,
            "abstract": (obj.get("abstract") or "").strip()[:60]}


def catalog_missing(limit: int = 0) -> list:
    """列出还没被模型编目过的文档（规则编目留下的"未分类/主题词为空"）。"""
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        sql = ("SELECT path FROM kb_docs WHERE genre='未分类' OR subjects='[]' "
               "OR COALESCE(abstract,'')='' ORDER BY path")
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [r[0] for r in conn.execute(sql)]
    finally:
        conn.close()


def enrich(paths: list, dry_run: bool = False) -> dict:
    """给一批文档补编目，写回 `kb_docs`。返回统计。"""
    from services.B3_header_engine.sqlite_store import get_conn
    from services.C1_knowledge.kb_indexer import read_any_file

    ok, fail = 0, []
    conn = get_conn()
    try:
        for path in paths:
            try:
                head = (read_any_file(path) or "")[:1200]
            except Exception as exc:  # noqa: BLE001
                fail.append({"path": path, "reason": f"{type(exc).__name__}"})
                continue
            try:
                got = _parse(_chat(PROMPT.format(genres="|".join(GENRES),
                                                 name=os.path.basename(path), head=head)))
            except Exception as exc:  # noqa: BLE001
                fail.append({"path": path, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if not got or not got.get("subjects"):
                fail.append({"path": path, "reason": "模型没给出主题词"})
                continue
            ok += 1
            if not dry_run:
                conn.execute(
                    "UPDATE kb_docs SET genre=?, subjects=?, abstract=? WHERE path=?",
                    (got["genre"], json.dumps(got["subjects"], ensure_ascii=False),
                     got["abstract"], path))
        if not dry_run:
            conn.commit()
        return {"ok": ok, "fail": len(fail), "errors": fail[:5]}
    finally:
        conn.close()
