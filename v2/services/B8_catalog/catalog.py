# -*- coding: utf-8 -*-
"""B8 · 著录导出（catalog_export）—— 2026-11-01，L3

**它是什么**
============
把一个主题/类目的**全部条目**，渲染成一份可读、可导出的**著录清单**
（极简都柏林核心 DC）。对应《图书馆学导论》p.42 图 2.4（MARC 编码）、
p.43（联机公共目录的显示格式）、p.32 描述法。

**它和 L1/L2 的关系（这是重点，别让它变成第三个平行工具）**
========================================================
::

    L2 collocate ──> 「这个主题里有哪些条目」（集合）
          │
    L1 citations ──> 「这些条目内部谁接着谁」（结构）
          │
          └──────> L3 catalog_export ──> 「一份带谱系的著录清单」

  * **集合来自 L2**：本模块**直接调用 `collocate()`**，不自己写一遍查询 ——
    否则两边口径一分叉，"导出 41 条、聚合说 40 条"这种打架迟早发生。
  * **结构来自 L1**：每条记录的 `relation` 不是数据库里那个几乎没人写的
    `memories.relations` 字段，而是**真实抽取出来的引文边**（cites / cited_by）。
  * **导出即验收**：`records == collocate.total` 是硬断言，见 `verify`。

所以 L3 不是第三个抽屉，是 L1×L2 的**合成品**：没有 L1，它只是一份清单；
没有 L2，它不知道该收哪些。
"""
from __future__ import annotations

import datetime
import json
import os
import re

__all__ = ["catalog_export"]

_SLUG = re.compile(r"[^\w\u4e00-\u9fff-]+")


def _slug(text: str) -> str:
    return (_SLUG.sub("-", (text or "").strip()) or "catalog")[:40].strip("-")


def _basename(path: str) -> str:
    return os.path.basename((path or "").replace("\\", "/"))


def _one_line(text: str) -> str:
    """去掉换行 —— 摘要是从模型输出里抄来的，可能自带换行，
    直接塞进 Markdown 的 `**题名**　…` 会把版面撕开（实测踩到）。
    """
    return re.sub(r"\s*\n+\s*", " ", (text or "").strip())


def _record(item: dict, edges: dict) -> dict:
    """一条记忆 → 一条 DC 著录记录。"""
    e = (edges or {}).get(item["memory_id"], {"out": [], "in": []})
    return {
        # ---- 都柏林核心的十五个元素里，本馆用得上的七个 ----
        "identifier": item["memory_id"],
        "title": item["summary"],
        "date": item["source_date"],
        "type": item["task_type"],
        "subject": {
            "class": item["class"],
            "sub_class": item["sub_class"],
            "topics": item.get("topics") or [],
        },
        # ⚠️ relation 用的是**引文边**，不是 memories.relations 那个自由文本字段。
        "relation": {
            "cites": [{"id": x["memory_id"], "kind": x["kind"],
                       "confidence": x["confidence"], "title": x["summary"]}
                      for x in e.get("out", [])],
            "cited_by": [{"id": x["memory_id"], "kind": x["kind"],
                          "confidence": x["confidence"], "title": x["summary"]}
                         for x in e.get("in", [])],
        },
        "source": item.get("source_file", ""),
        "provenance": {"layer": item["layer"], "heat": item["heat"],
                       "matched_by": item["matched_by"], "has_raw": bool(item["has_l3"])},
    }


def _md(meta: dict, records: list) -> str:
    out = [f"# 馆藏著录 · {meta['target']}", ""]
    out.append("| 项 | 值 |")
    out.append("|---|---|")
    out.append(f"| 主题 | {meta['target']} |")
    if meta.get("merged_spellings"):
        merged = "、".join(f"{k}×{v}" for k, v in meta["merged_spellings"].items())
        out.append(f"| 合并写法 | {merged} |")
    if meta.get("matched_terms"):
        out.append(f"| 规范词 | {'、'.join(meta['matched_terms'])} |")
    out.append(f"| 记录数 | **{meta['total']}** 条 |")
    out.append(f"| 时间跨度 | {meta['span'][0]} ~ {meta['span'][1]} |")
    out.append(f"| 有引文的记录 | {meta['linked']} / {meta['total']} |")
    out.append(f"| 生成时间 | {meta['generated_at']} |")
    out.append("")
    out.append("> 著录单位是「一条记忆」；`引文` 一栏是**实际抽取的谱系**（谁接着谁），"
               "不是语义相似度。标识符＝memory_id，可回 `libmem_drilldown` 取原文。")
    out.append("")
    out.append("---")
    out.append("")

    for i, r in enumerate(records, 1):
        out.append(f"## {i} · {r['date']} · `{r['identifier']}`")
        out.append(f"**题名**　{_one_line(r['title'])}")
        cls = " / ".join(x for x in (r["subject"]["class"], r["subject"]["sub_class"]) if x)
        out.append(f"**类目**　{cls or '—'}　·　**类型**　{r['type'] or '—'}")
        topics = "、".join(r["subject"]["topics"][:8])
        out.append(f"**主题词**　{topics or '—'}")
        out.append(f"**来源**　{r['source'] or '—'}　·　**层**　{r['provenance']['layer']}")
        rel = r["relation"]
        if rel["cites"] or rel["cited_by"]:
            bits = []
            for x in rel["cites"][:3]:
                bits.append(f"→ 引用了 `{x['id']}`（{x['kind']} {x['confidence']}）"
                            f"{_one_line(x['title'])[:24]}")
            for x in rel["cited_by"][:3]:
                bits.append(f"← 被 `{x['id']}` 引用（{x['kind']} {x['confidence']}）"
                            f"{_one_line(x['title'])[:24]}")
            out.append("**引文**　" + "；".join(bits))
        else:
            out.append("**引文**　—（这条没有抽取到前驱/后继）")
        out.append("")
    return "\n".join(out)


def catalog_export(target: str, kind: str = "entity", format: str = "md",
                   with_citations: bool = True, save: bool = False,
                   limit: int = 0) -> dict:
    """把一个主题/类目的全部条目导出成**著录清单**。

    Args:
        target: 实体名（`书房OS`）或类目名（`软件与代码`）。
        kind:   `entity` | `class`（口径与 `collocate` 完全一致，因为它就是调它）。
        format: `md`（人读）| `json`（机器，DC 记录数组）。
        with_citations: 是否带上引文边（关掉 = 只要清单，不求谱系）。
        save:   True 时同时写一份到 `data/exports/`，返回里带 `path`。
        limit:  0 = 全部；>0 = 只导前 N 条（调试用）。

    Returns:
        `{ok, target, kind, total, records, linked, span, text, path?, format}`
        —— `total` 与 `len(json.loads/text 的记录数)` **必须相等**，这是本模块的硬约束。
    """
    from services.B8_catalog import collocate
    from services.B8_catalog.citations import edges_for_many

    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "target 不能为空"}
    fmt = (format or "md").lower()
    if fmt not in ("md", "json"):
        return {"ok": False, "error": f"format 只能是 md / json，收到 {format!r}"}

    res = collocate(target, kind=kind, order="time", limit=0)
    if not res.get("ok"):
        return res
    items = res["items"]
    if int(limit or 0) > 0:
        items = items[:int(limit)]

    ids = [i["memory_id"] for i in items]
    edges = edges_for_many(ids) if with_citations else {}

    # 来源文件名：collocate 不返回 content_path，这里一次补齐（避免逐条回表）
    files = {}
    if ids:
        from services.B3_header_engine.sqlite_store import get_conn
        conn = get_conn()
        try:
            ph = ",".join("?" * len(ids))
            for mid, path in conn.execute(
                    f"SELECT memory_id, content_path FROM memories WHERE memory_id IN ({ph})",
                    ids):
                files[mid] = _basename(path)
        finally:
            conn.close()
    for it in items:
        it["source_file"] = files.get(it["memory_id"], "")

    records = [_record(it, edges) for it in items]
    linked = sum(1 for r in records if r["relation"]["cites"] or r["relation"]["cited_by"])
    dates = sorted(r["date"] for r in records if r["date"])
    meta = {
        "target": target,
        "kind": kind,
        "total": len(records),
        "linked": linked,
        "span": [dates[0], dates[-1]] if dates else ["", ""],
        "merged_spellings": res.get("merged_spellings") or {},
        "matched_terms": res.get("matched_terms") or [],
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "with_citations": with_citations,
    }

    if fmt == "json":
        text = json.dumps({"meta": meta, "records": records}, ensure_ascii=False, indent=2)
    else:
        # 著录清单按**时间正序**排（书目习惯：从头读下来），collocate 给的是倒序
        text = _md(meta, list(reversed(records)))

    out = {"ok": True, "target": target, "kind": kind,
           "total": len(records), "linked": linked,
           "span": meta["span"], "format": fmt, "text": text}

    if save:
        from services.shared.config import loader
        base = ((loader.get_config() or {}).get("paths", {}) or {}).get("base_dir", ".")
        outdir = os.path.join(base, "data", "exports")
        os.makedirs(outdir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(outdir, f"catalog-{_slug(target)}-{stamp}.{fmt}")
        path = os.path.abspath(path)     # 返回给调用方的必须是绝对路径，否则点不开
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        out["path"] = path
    return out
