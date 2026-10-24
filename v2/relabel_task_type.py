# -*- coding: utf-8 -*-
"""v1.3.2 · 重标 `task_type`（回填工具）。

为什么需要它
------------
实测 `header.db` 里 **380/482 = 78.8% 的 task_type 都是"闲聊"**，而它是 Phase1 的
过滤维度 —— **拿它过滤等于不过滤**。逐层查下来（详见 `task_types.yaml` 文件头）：

  * 392 条老数据（v1 迁移）的值**不是当前代码写的**，用当前规则重标一致率仅 **1.8%**；
  * 而当前规则本身（关键词匹配 + 兜底"其他"）重标后 **56% 落进兜底**，也是坏的。

所以修法是：**把取值收成封闭词表（`task_types.yaml`）+ 用模型按判据标引**。

用法::

    python relabel_task_type.py                      # dry-run（默认，不写库）
    python relabel_task_type.py --limit 50           # dry-run 且只跑 50 条
    python relabel_task_type.py --model Qwen3.5-9B-Q4_K_M   # 换模型对比
    python relabel_task_type.py --apply              # ★ 真写库（先自动备份）

安全约定
--------
* **默认 dry-run**：不写库。要写必须显式 `--apply`。
* `--apply` 前**强制备份** `header.db`，并把 `memory_id → (旧值, 新值)` 落成 undo JSON，
  所以这次操作是**可回滚**的。
* **空返回不许静默兜底**：调用返回空串时**计入 `empty` 并告警**，而不是算作"其他"。
  （这条是踩出来的：第一次试标 30 条全空——因为 Qwen3.5 的思维链把 max_tokens 吃光了，
   `content` 返回空串。若不显式报出来，就会得出"模型 100% 标不出来"的假结论。）
* 标引视角是**回顾式**（这条记忆记录了哪种活动），不是"用户想干什么" ——
  第一版判据按"意图"写，导致"决定采用…"这类记录全漏进兜底（「其他」占 33%）。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.environ.setdefault("SHUFANG_DATA_DIR", _HERE)

from services.shared import task_types as TT          # noqa: E402

DB = os.path.join(_HERE, "data", "index", "header.db")
BASE = "http://127.0.0.1:8089/v1"
_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def ask(model: str, prompt: str, timeout: int = 180):
    """返回 (类型文本, finish_reason, 思维链长度)。空返回必须能被上层看见。"""
    def _call(extra):
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0, "max_tokens": 64}
        body.update(extra)
        req = urllib.request.Request(
            BASE + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return ((msg.get("content") or "").strip(),
                ch.get("finish_reason") or "",
                len(msg.get("reasoning_content") or ""))
    try:
        return _call(_NO_THINK)
    except urllib.error.HTTPError as e:
        if e.code == 400:                 # 服务端不认 chat_template_kwargs
            return _call({})
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只 dry-run）")
    ap.add_argument("--model", default="Qwen3.5-4B-Q4_K_M")
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"找不到 {DB}")
        return 2

    print(f"[task_types] {TT.dump()}")
    print(f"模式={'★ 写库' if args.apply else 'dry-run（不写库）'}  模型={args.model}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select memory_id, summary, core_entities, task_type, source_date "
        "from memories order by source_date, memory_id").fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"待标引 {len(rows)} 条\n")

    t0 = time.time()
    dist, old_dist = collections.Counter(), collections.Counter()
    changes, empty, failed = [], 0, 0
    for i, r in enumerate(rows):
        try:
            ents = json.loads(r["core_entities"] or "[]")
        except Exception:                                        # noqa: BLE001
            ents = []
        raw = ""
        try:
            raw, fr, _ = ask(args.model, TT.prompt_for(r["summary"], ents))
        except Exception as e:                                   # noqa: BLE001
            failed += 1
            print(f"  [{i+1}/{len(rows)}] 调用失败: {type(e).__name__}: {e}")
            continue
        if not raw:
            empty += 1
            print(f"  [{i+1}/{len(rows)}] ⚠️ 空返回（finish={fr}）—— 不计入分布")
            continue
        new = TT.normalize(raw)
        dist[new] += 1
        old_dist[r["task_type"]] += 1
        if new != r["task_type"]:
            changes.append((r["memory_id"], r["task_type"], new))
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            print(f"  [{i+1}/{len(rows)}] {el:.0f}s  {el/(i+1):.2f}s/条  "
                  f"剩余 ~{(len(rows)-i-1)*el/(i+1)/60:.1f}min", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    print()
    print("旧分布:", json.dumps(old_dist.most_common(), ensure_ascii=False))
    print("新分布:", json.dumps(dist.most_common(), ensure_ascii=False))
    v = TT.validate(dist)
    print("判据:", "通过" if v["ok"] else "不通过", v.get("why") or "",
          f"| 最大单值 {v['max_share']*100:.1f}%  兜底 {v['fallback_share']*100:.1f}%")
    print(f"会改动 {len(changes)} 条  空返回 {empty}  调用失败 {failed}")
    if empty or failed:
        print("⚠️ 有空返回/失败就先别写库 —— 分布是不完整的")

    out = os.path.join(_HERE, "docs", "relabel-task_type-"
                       + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"model": args.model, "applied": bool(args.apply),
               "task_types": TT.dump(), "old": old_dist, "new": dist,
               "validate": v, "empty": empty, "failed": failed,
               "changes": changes},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("报告:", out)

    if not args.apply:
        print("\n（dry-run 结束，库里一个字没改。要写库加 --apply）")
        return 0

    if empty or failed:
        print("\n✗ 空返回/失败不为 0，拒绝写库。")
        return 3

    # ---- 强制备份 + 可回滚 ----
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB + f".bak-tasktype-{stamp}"
    shutil.copy2(DB, bak)
    undo = os.path.join(_HERE, "docs", f"undo-task_type-{stamp}.json")
    json.dump([{"memory_id": m, "old": o} for m, o, _ in changes],
              open(undo, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with conn:
        conn.executemany("update memories set task_type = ? where memory_id = ?",
                         [(n, m) for m, _, n in changes])
        n = conn.total_changes
    print(f"\n✓ 已写库，改动 {n} 条")
    print(f"  备份: {bak}")
    print(f"  回滚表: {undo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
