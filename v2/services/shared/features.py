# -*- coding: utf-8 -*-
"""功能开关（2026-11-01，随 L1~L6 一起加）

**为什么需要它**
==============
L1~L6 有六个新功能，其中 L5（查询改写）会改变排序、L6（排架）会改变浏览顺序。
一旦线上出问题，必须能**一句话关回去**，而不是去改代码。

三层优先级（高 → 低）
====================
1. **环境变量 `LIBMEM_DISABLE`** —— 最高优先，用于"立刻止血"。
   进程级、不碰配置、重启即生效：
   ```
   set LIBMEM_DISABLE=all                 # 全关（回到接线前的行为）
   set LIBMEM_DISABLE=citations,collocate # 只关这两个
   ```
2. **`config.yaml` 的 `features:` 段** —— 持久选择。
   ```yaml
   features:
     citations: true
     collocate: true
     catalog: true
     see_also: true
     authority_rewrite: false    # 有风险，默认关
     subject_shelf: false        # 有风险，默认关
     enrich: true                # memory_search 的覆盖面/引文增强
   ```
3. **代码里的 DEFAULTS** —— 兜底。

⚠️ 关掉一个功能时，**它的数据不会消失**（表还在、边还在），只是不再被使用/输出。
   重新打开就恢复。这是"可回滚"的关键：回滚的是**行为**，不是数据。
"""
from __future__ import annotations

import os

# 默认值就是"发布时的安全值"：只读增强全开，会改排序/输出的全关。
DEFAULTS = {
    "citations": True,           # L1：引文边（写入时建边、citations 工具）
    "collocate": True,           # L2：聚合清单
    "catalog": True,             # L3：著录导出
    "see_also": True,            # L4：参见（只影响输出，不改排序）
    "authority_rewrite": False,  # L5：查询改写（**会改排序**，默认关）
    "subject_shelf": False,      # L6：主题排架（默认关，browse 仍按时间倒序）
    "enrich": True,              # 接线：search 结果里的覆盖提示 + 引文 id
}

_CACHE: dict = {"cfg": None, "ts": 0.0}
_TTL = 5.0


def _all_off() -> set:
    raw = (os.environ.get("LIBMEM_DISABLE") or "").strip().lower()
    if not raw:
        return set()
    if raw in ("all", "*", "1", "true"):
        return set(DEFAULTS)
    return {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def _config_section() -> dict:
    """读 config.yaml 的 `features:` 段（5 秒缓存，改配置不用重启也能生效）。"""
    import time
    now = time.time()
    if _CACHE["cfg"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["cfg"]
    try:
        from services.shared.config import loader
        cfg = loader.get_config() or {}
        section = cfg.get("features") or {}
        if not isinstance(section, dict):
            section = {}
    except Exception:            # noqa: BLE001 - 读不到配置就只用默认值
        section = {}
    _CACHE.update({"cfg": section, "ts": now})
    return section


def enabled(name: str) -> bool:
    """某个功能现在开不开。未知名字按关闭处理（宁可不做，也别误开）。"""
    if name not in DEFAULTS:
        return False
    if name in _all_off():
        return False
    val = _config_section().get(name)
    if isinstance(val, bool):
        return val
    return bool(DEFAULTS[name])


def all_flags() -> dict:
    """给面板 / 诊断用：每个功能的最终状态 + 来源。"""
    off = _all_off()
    section = _config_section()
    out = {}
    for name, default in DEFAULTS.items():
        src = "default"
        if name in off:
            val, src = False, "env:LIBMEM_DISABLE"
        elif isinstance(section.get(name), bool):
            val, src = bool(section[name]), "config"
        else:
            val = bool(default)
        out[name] = {"enabled": val, "source": src}
    return out


def reset_cache() -> None:
    """改完配置或环境变量后手动刷新（测试用）。"""
    _CACHE.update({"cfg": None, "ts": 0.0})
