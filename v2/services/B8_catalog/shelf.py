# -*- coding: utf-8 -*-
"""B8 · 排架（shelf）—— 2026-11-01，L6

**它是什么**
==========
图书馆按**分类号**在架上排书（《图书馆学导论》p.45）：同类挨着，
所以"逛书架"能发现相关的东西。插件里对应的是**类目的先后次序**。

现在的 `browse` 按类名字母序排 —— 那不是排架，是字典。本模块给它一套**公版次序**。

**两层，和款目表同一套思路**
=========================
::

    data/shelf/general.yaml    ← 公版：类目次序（跨用户成立）
    (私版)                      ← 二级类目由 B8.build_subclasses 从**这个用户的数据**长出来

公版只回答"哪一类排前面"；**二级类目名不在这里定义** ——
那必须从用户自己的馆藏里长（同一个"软件与代码"，写插件的人和做设计的人
子类完全不同）。这就是"通用表走天下不成立"的地方。
"""
from __future__ import annotations

import os

__all__ = ["skeleton", "rank"]

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "shelf")
_CACHE: dict = {"mtime": None, "order": None, "notes": None}


def _load() -> dict:
    import yaml
    path = os.path.join(_DATA, "general.yaml")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"order": [], "notes": {}}
    if _CACHE["order"] is not None and _CACHE["mtime"] == mtime:
        return {"order": _CACHE["order"], "notes": _CACHE["notes"]}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - 表坏了退回字母序，不能让浏览挂掉
        print(f"[shelf] 读 general.yaml 失败: {type(exc).__name__}: {exc}")
        data = {}
    _CACHE.update({"mtime": mtime,
                   "order": data.get("order") or [],
                   "notes": data.get("notes") or {}})
    return {"order": _CACHE["order"], "notes": _CACHE["notes"]}


def skeleton() -> dict:
    """公版排架骨架（给面板/目录页用）。"""
    data = _load()
    return {"order": data["order"],
            "notes": [{"class": c, "note": data["notes"].get(c, "")} for c in data["order"]]}


def rank(class_name: str) -> int:
    """类目的排架名次（越小越靠前）；未知类目排到最后。"""
    order = _load()["order"]
    try:
        return order.index(class_name)
    except ValueError:
        return len(order) + 1
