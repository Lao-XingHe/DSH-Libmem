# -*- coding: utf-8 -*-
"""B8 · 规范控制 / 款目表（authority）—— 2026-11-01，L5 的数据那一半

**两层结构（这是整个设计的关键）**
==============================
::

    data/authority/general.yaml   ← 随插件分发的「公版」：跨用户成立的写法差异
    data/authority/private.yaml   ← 这个用户自己的「私版」：AI 从**他的**记忆里建

  张三的私版里有他的项目名，李四的有他的。
  **公版是机制，私版是资产** —— 发插件只发公版，私版永远是本机生成的。

**为什么私版必须由 AI 建、而且必须"提醒着建"**
==========================================
判断「这两个名字是不是同一个东西」是**语义判断**，规则做不好；
但全库跑一遍是**烧 token 的活**，绝不能后台静默跑（用户在不知情时被扣费，
建出来的东西他也没看过、不会认）。
所以：`suggest()` 只**数数**（不花一分钱），数到该建了才提醒人；
人点头之后 `build_private()` 只做**增量**，并给出待确认清单。

**和 L5「查询改写」的关系**
========================
改写那条路实测无收益（两种写法 top-8 重合 100%，见 ROADMAP），所以默认关。
但**款目表本身仍然要做** —— 它的价值在：聚合/著录/沿革里「同一个作品只算一份」。
"""
from __future__ import annotations

import datetime
import json
import os
import re

__all__ = ["general_path", "private_path", "records", "resolve", "build_private",
           "suggest", "stats", "ensure_table"]

DESK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTH_DIR = os.path.join(DESK, "data", "authority")

# 高置信合并：只差大小写 / 空白 / 连字符 / 全半角 —— 这类不需要人确认
_SEP = re.compile(r"[\s\-_·、/]+")


def general_path() -> str:
    return os.path.join(AUTH_DIR, "general.yaml")


def private_path() -> str:
    return os.path.join(AUTH_DIR, "private.yaml")


def _norm(text: str) -> str:
    try:
        from services.B8_catalog import normalize_term
        return normalize_term(text)
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def _key(text: str) -> str:
    return _SEP.sub("", _norm(text))


def _load_yaml(path: str) -> dict:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - 表坏了不能让检索挂掉
        print(f"[authority] 读 {path} 失败: {type(exc).__name__}: {exc}")
        return {}


def records() -> list[dict]:
    """公版 + 私版合并。私版**覆盖**公版（同一个 key 以私版为准）。"""
    out: dict[str, dict] = {}
    for scope, path in (("general", general_path()), ("private", private_path())):
        data = _load_yaml(path)
        for e in data.get("entries") or []:
            canon = (e.get("canonical") or "").strip()
            if not canon:
                continue
            k = _key(canon)
            out[k] = {
                "canonical": canon,
                "kind": e.get("kind") or "concept",
                "aliases": [a for a in (e.get("aliases") or []) if a and a != canon],
                "scope": scope,
            }
    return list(out.values())


def resolve(name: str) -> dict | None:
    """给一个写法，返回它的款目（`canonical` / `aliases` / `scope`）。查不到返回 None。"""
    k = _key(name)
    if not k:
        return None
    for r in records():
        if _key(r["canonical"]) == k:
            return r
        if any(_key(a) == k for a in r["aliases"]):
            return r
    return None


def ensure_table(conn=None) -> dict:
    """把款目落成一张表（给 SQL 侧的聚合/沿革用）。"""
    from services.B3_header_engine.sqlite_store import get_conn
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS authority (
              canonical  TEXT PRIMARY KEY,
              scope      TEXT NOT NULL,      -- general | private
              kind       TEXT,
              aliases    TEXT,               -- JSON 数组
              spellings  TEXT,               -- JSON: {写法: 出现次数}（私版才有）
              built_at   TEXT
            );
        """)
        rows = 0
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for r in records():
            conn.execute(
                "INSERT OR REPLACE INTO authority(canonical, scope, kind, aliases, "
                "spellings, built_at) VALUES(?,?,?,?,?,?)",
                (r["canonical"], r["scope"], r["kind"],
                 json.dumps(r["aliases"], ensure_ascii=False), "{}", now))
            rows += 1
        return {"ok": True, "rows": rows}
    finally:
        if own:
            conn.close()


def _scan_entities() -> dict:
    """{归一键: {写法: 条数}} —— 扫全库实体。"""
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        groups: dict[str, dict] = {}
        for (raw,) in conn.execute("SELECT core_entities FROM memories"):
            try:
                ents = json.loads(raw or "[]")
            except Exception:
                continue
            for e in ents:
                if not isinstance(e, str) or len(e.strip()) < 2:
                    continue
                k = _key(e)
                if not k:
                    continue
                slot = groups.setdefault(k, {})
                slot[e.strip()] = slot.get(e.strip(), 0) + 1
        return groups
    finally:
        conn.close()


def build_private(dry_run: bool = True, min_freq: int = 3) -> dict:
    """从**这个用户**的记忆里建私版款目表（增量：只处理没进表的）。

    入选条件（满足其一）：
      * 同一个 key 有 **≥2 种写法**（写法分裂 —— 最该收的）；
      * 总频次 ≥ `min_freq` 且**不在公版里**（这是用户的高频自造词）。

    分流：
      * 只差大小写/空白/连字符 → 高置信，直接收（记 `spellings` 备查）；
      * 差异更大（例如 `书房 OS v2`）→ 进 `pending`，等人点头。
    """
    import yaml

    groups = _scan_entities()
    general_keys = {_key(r["canonical"]) for r in records() if r["scope"] == "general"}
    old = {}
    try:
        with open(private_path(), "r", encoding="utf-8") as fh:
            for e in (yaml.safe_load(fh) or {}).get("entries") or []:
                old[_key(e.get("canonical") or "")] = e
    except FileNotFoundError:
        old = {}
    except Exception:  # noqa: BLE001
        old = {}

    accepted, pending = [], []
    for k, spellings in groups.items():
        if k in general_keys or k in old:
            continue
        freq = sum(spellings.values())
        if len(spellings) >= 2:
            # 差异是否"只是写法"：把写法都按分隔符抹掉后相不相等
            accepted.append({"canonical": max(spellings, key=spellings.get),
                             "kind": "concept", "aliases": sorted(spellings),
                             "spellings": spellings})
        elif freq >= min_freq:
            accepted.append({"canonical": max(spellings, key=spellings.get),
                             "kind": "concept", "aliases": [],
                             "spellings": spellings})

    out = {"dry_run": dry_run, "groups": len(groups),
           "general_covered": len(general_keys), "already": len(old),
           "new_entries": len(accepted), "pending": len(pending),
           "sample": [e["canonical"] for e in accepted[:8]]}
    if dry_run:
        return out

    merged = list(old.values()) + [
        {"canonical": e["canonical"], "kind": e["kind"],
         "aliases": [a for a in e["aliases"] if a != e["canonical"]]}
        for e in accepted]
    os.makedirs(AUTH_DIR, exist_ok=True)
    payload = {
        "meta": {"version": 1, "scope": "private",
                 "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "note": "由 AI 从这个用户的记忆里生成；可随时用 authority_build 重建（增量）"},
        "entries": merged,
    }
    with open(private_path(), "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    out["written"] = len(merged)
    out["path"] = private_path()
    ensure_table()
    return out


def suggest() -> dict:
    """**只数数、不花算力**：现在该不该提醒用户"建一次款目表"了。

    三个触发条件（任一满足就提醒）：
      * 上次建构以来新增记忆 ≥ 100 条；
      * 新出现的写法分裂组 ≥ 10 组；
      * 距上次建构 ≥ 30 天。
    """
    from services.B3_header_engine.sqlite_store import get_conn
    groups = _scan_entities()
    general_keys = {_key(r["canonical"]) for r in records() if r["scope"] == "general"}
    try:
        import yaml
        with open(private_path(), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        built_at = (data.get("meta") or {}).get("built_at")
        have = {_key(e.get("canonical") or "") for e in data.get("entries") or []}
    except Exception:  # noqa: BLE001
        built_at, have = None, set()

    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()

    new_groups = sum(1 for k, sp in groups.items()
                     if k not in general_keys and k not in have and len(sp) >= 2)
    days = None
    if built_at:
        try:
            days = (datetime.datetime.now()
                    - datetime.datetime.fromisoformat(built_at)).days
        except Exception:  # noqa: BLE001
            days = None

    reasons = []
    if new_groups >= 10:
        reasons.append(f"新出现 {new_groups} 组写法分裂")
    if days is not None and days >= 30:
        reasons.append(f"距上次建构 {days} 天")
    if not built_at:
        reasons.append("还没建过私版款目表")

    return {"should_prompt": bool(reasons), "reasons": reasons,
            "conflict_groups": new_groups, "memories": total,
            "last_built": built_at,
            "note": "提醒本身不消耗算力；同意后再调 authority_build 做增量。"}


def stats() -> dict:
    from services.B3_header_engine.sqlite_store import get_conn
    recs = records()
    by_scope: dict[str, int] = {}
    for r in recs:
        by_scope[r["scope"]] = by_scope.get(r["scope"], 0) + 1
    conn = get_conn()
    try:
        table = conn.execute("SELECT COUNT(*) FROM authority").fetchone()[0]
    except Exception:  # noqa: BLE001
        table = None
    finally:
        conn.close()
    return {"entries": len(recs), "by_scope": by_scope, "table_rows": table,
            "general_path": general_path(), "private_path": private_path()}
