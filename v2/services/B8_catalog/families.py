# -*- coding: utf-8 -*-
"""B8 · 词族（families）—— 2026-11-03，L4 的真关系来源

**为什么要它**：现在 `see_also` 的上下位（S/F）是**从分类号倒推**的 ——
`倒排索引` 的上位之所以是 `检索与索引`，只是因为它们落在同一个子类里。
那是"推断"，不是"词表里写着"。

词族给的是**真关系**：族首词（顶层，只有分项 F）+ 属项 S + 族项 Z。
依据是《汉语主题词表》的词族索引（第三卷只是附表，词族索引在第二卷第五分册），
这里按同一套规则做了一个**本馆专用的小种子** `data/authority/families.yaml`。

**设计上守的两条**
================
1. **按需加载**：文件小、只在 `lookup()` 第一次调用时读，读完缓存 ——
   不参与检索热路径，也不进任何索引。
2. **不覆盖 S/F**：词族给的关系**优先**于分类号倒推的（`source` 字段区分），
   但倒推的那套仍然保留 —— 词族没收进来的词，照样有兜底。
"""
from __future__ import annotations

import os

__all__ = ["lookup", "stats", "all_heads"]

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "authority")
_CACHE: dict = {"mtime": None, "index": None}


def _index() -> dict:
    """{词: {head, parent, siblings}} —— 懒加载 + 按 mtime 失效。"""
    import yaml
    path = os.path.join(_DATA, "families.yaml")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CACHE["index"] is not None and _CACHE["mtime"] == mtime:
        return _CACHE["index"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - 词族坏了只是少一路参见
        print(f"[families] 读 families.yaml 失败: {type(exc).__name__}: {exc}")
        return {}
    idx: dict = {}
    for fam in data.get("families") or []:
        head = (fam.get("head") or "").strip()
        if not head:
            continue
        members = [m for m in (fam.get("members") or []) if m]
        idx.setdefault(head, {"head": head, "parent": None, "siblings": members})
        for m in members:
            idx.setdefault(m, {"head": head, "parent": head, "siblings": members})
    _CACHE.update({"mtime": mtime, "index": idx})
    return idx


def lookup(term: str) -> dict:
    """某词在词族里的位置：`{head, parent, children} | {}`。

    * 族首词：`parent=None`，`children` 是它的分项；
    * 普通词：`parent=族首词`，`children=[]`。
    """
    idx = _index()
    node = idx.get((term or "").strip())
    if not node:
        return {}
    head = node["head"]
    return {
        "head": head,
        "parent": node["parent"],
        "children": node["siblings"] if node["parent"] is None else [],
        "source": "families.yaml",
    }


def all_heads() -> list:
    return [t for t, n in _index().items() if n["parent"] is None]


def stats() -> dict:
    idx = _index()
    heads = all_heads()
    return {"terms": len(idx), "families": len(heads), "heads": heads}
