# -*- coding: utf-8 -*-
"""用本机 9B 给知识库文档补编目（体裁 + 主题词 + 一句话摘要）。

**为什么用本地小模型**（用户问的）
================================
编目是「读 1000 字 → 吐 5 个字段」的重复活：
  · 量大（158 份）、活得重复、判断难度低 → 正好是小模型的舒适区；
  · 9B 在本机 llama-server 上跑，**零 API 费用**，失败了可以整批重跑；
  · 大模型（我）只做三件事：**定字段约束、抽检合格率、写规则兜底**。

**规则先行**：文件名/首行/书名号已经能判一部分，只有判不出来的才送给模型。

用法：
    python bin/catalog-llm.py --limit 5 --dry-run     # 先看 5 份的效果
    python bin/catalog-llm.py --all                   # 全量补
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

V2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "v2")
sys.path.insert(0, V2)

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
    subs = [str(s).strip() for s in (obj.get("subjects") or []) if str(s).strip()][:5]
    return {"genre": genre, "subjects": subs,
            "abstract": (obj.get("abstract") or "").strip()[:60]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from services.C1_knowledge.kb_indexer import read_any_file
    conn = sqlite3.connect(os.path.join(V2, "data", "index", "header.db"))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT path, title, genre, subjects FROM kb_docs "
        "WHERE genre='未分类' OR subjects='[]' ORDER BY path"))
    todo = rows if args.all else rows[:args.limit]
    print("待编目 %d 份（共 %d 份缺口）→ 用 %s" % (len(todo), len(rows), MODEL))

    ok, bad = 0, 0
    for r in todo:
        try:
            head = (read_any_file(r["path"]) or "")[:1200]
        except Exception as exc:
            print("  [跳] %s: %s" % (os.path.basename(r["path"]), exc))
            continue
        try:
            raw = _chat(PROMPT.format(genres="|".join(GENRES),
                                      name=os.path.basename(r["path"]), head=head))
            got = _parse(raw)
        except Exception as exc:
            print("  [错] %s: %s" % (os.path.basename(r["path"]), exc))
            bad += 1
            continue
        if not got:
            bad += 1
            print("  [空] %s → %r" % (os.path.basename(r["path"]), raw[:60]))
            continue
        ok += 1
        print("  %s\n      体裁=%s  主题词=%s\n      摘要=%s"
              % (r["title"][:40], got["genre"], "、".join(got["subjects"]), got["abstract"]))
        if not args.dry_run:
            conn.execute("UPDATE kb_docs SET genre=?, subjects=?, abstract=? WHERE path=?",
                         (got["genre"], json.dumps(got["subjects"], ensure_ascii=False),
                          got["abstract"], r["path"]))
    if not args.dry_run:
        conn.commit()
        print("已写回 %d 条" % ok)
    conn.close()
    print("成功 %d / 失败 %d" % (ok, bad))


if __name__ == "__main__":
    main()
