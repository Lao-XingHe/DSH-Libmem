# -*- coding: utf-8 -*-
"""整链体检 —— 冷库 → 温层 → 索引 三层对不上就喊（2026-10-12）。

## 为什么需要它（"管线没做好"的第③项）
工位各自都活着，但**没有任何一处能回答**"现在数据流到哪一步了、有没有掉件"。
此前唯一发现问题是靠人肉：把三层的条数一条条数出来对比，才发现"冷库 521 行、温层 446 条、
索引 838 行"这种对不上的局面，以及 134 行重复写入。

## 它检查什么（全部**只读**，不改任何东西）
| 断言 | 含义 | 不对时说明 |
|---|---|---|
| `cold_dup` | 同一文件里 `user+assistant` 完全相同的行 | 写入侧幂等失效（会被 B2 提炼成两条记忆） |
| `warm_no_cold` | 温层条目的 `ref` 指向不存在的冷库 id | 冷库被清理过 / 温层是脏的 |
| `cold_unrefined` | 冷库有行、温层没有对应 `ref` | 待提炼（正常会有少量；积压说明提炼卡住） |
| `index_orphan` | 索引里的 `memory_id` 不在温层集合里**且**该日期**有**温层文件 | 索引有孤儿行（温层已删、索引没跟上） |
| `index_legacy` | 同上但**该日期没有**温层文件 | v1 迁移来的历史行（**正常**，要报出来但不算错） |
| `index_dup` | 索引里 `summary` 完全相同的组 | 重复占位（稀释检索名额） |
| `index_empty` | `summary` 为空的行 | 垃圾行 |

⚠️ 区分 `index_orphan` 与 `index_legacy` 很要紧：4~7 月那 392 行**没有温层文件**是历史遗留，
用 `index_warm(prune=True)` 去处理会把它们全删（2026-10-12 实测踩到）。体检只**报**不删。

## 用法
    python -m services.B3_header_engine.check_chain          # 打印报告
    python -m services.B3_header_engine.check_chain --json   # 机器可读（给工具面用）
`check_chain()` 同时给 `libmem_status` / 面板当数据源。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

V2 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, V2)
sys.path.insert(0, os.path.join(V2, 'services'))

COLD = os.path.join(V2, 'data', 'cold_vault')
WARM = os.path.join(V2, 'data', 'warm')
DB = os.path.join(V2, 'data', 'index', 'header.db')


def _load_jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def _day_files(directory: str) -> dict:
    """`{日期: 文件路径}`（跳过 .bak / .lock / .tmp）。"""
    out = {}
    for path in glob.glob(os.path.join(directory, '20*.jsonl')):
        name = os.path.basename(path)
        if '.bak' in name or name.endswith('.lock') or '.tmp' in name:
            continue
        out[name[:-6]] = path
    return out


def check_chain(today: str | None = None) -> dict:
    """跑一遍三层对账，返回结构化结果（**只读**）。

    Args:
        today: 仅供参考的回显（不看它做判断），便于报告里写清"哪一天数的"。

    Returns:
        `{"ok": bool, "counts": {...}, "problems": [...], "legacy": {...}, "checked_at": str}`
    """
    import datetime
    import sqlite3

    problems: list[str] = []
    counts: dict = {}

    # ---- 冷库 ----
    cold_days = _day_files(COLD)
    cold_lines = 0
    cold_dup = 0
    cold_ids_by_day: dict[str, set] = {}
    for day, path in cold_days.items():
        entries = _load_jsonl(path)
        cold_lines += len(entries)
        seen = set()
        ids = set()
        for entry in entries:
            key = ((entry.get('user') or '').strip(), (entry.get('assistant') or '').strip())
            cid = str(entry.get('id') or '')
            if cid:
                ids.add(cid)
            if key[0] and key[1]:
                if key in seen:
                    cold_dup += 1
                else:
                    seen.add(key)
        cold_ids_by_day[day] = ids
    counts['cold_days'] = len(cold_days)
    counts['cold_lines'] = cold_lines
    counts['cold_dup'] = cold_dup
    if cold_dup:
        problems.append(f'冷库有 {cold_dup} 行重复写入（user+assistant 双同）—— 写入侧幂等可能失效')

    # ---- 温层 ----
    warm_days = _day_files(WARM)
    warm_refs: dict[str, set] = {}
    warm_entries = 0
    warm_no_cold = 0
    for day, path in warm_days.items():
        entries = _load_jsonl(path)
        warm_entries += len(entries)
        refs = set()
        for entry in entries:
            ref = str(entry.get('ref') or '')
            cid = ref.split('#', 1)[1] if '#' in ref else ''
            if cid:
                refs.add(cid)
                if day in cold_ids_by_day and cid not in cold_ids_by_day[day]:
                    warm_no_cold += 1
        warm_refs[day] = refs
    counts['warm_days'] = len(warm_days)
    counts['warm_entries'] = warm_entries
    counts['warm_no_cold'] = warm_no_cold
    if warm_no_cold:
        problems.append(f'温层有 {warm_no_cold} 条 ref 指向不存在的冷库 id（温层脏了或被清过冷库）')

    # 待提炼：冷库有、温层没有
    pending = 0
    for day, ids in cold_ids_by_day.items():
        refs = warm_refs.get(day)
        if refs is None:
            pending += len(ids)
        else:
            pending += len(ids - refs)
    counts['cold_unrefined'] = pending

    # ---- 索引 ----
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT memory_id, summary, source_date FROM memories').fetchall()
    index_rows = len(rows)
    index_empty = sum(1 for r in rows if not (r['summary'] or '').strip())
    by_summary: dict = {}
    for r in rows:
        s = (r['summary'] or '').strip()
        if s:
            by_summary[s] = by_summary.get(s, 0) + 1
    index_dup = sum(n - 1 for n in by_summary.values() if n > 1)

    # 孤儿：索引行的 memory_id 反算不出温层 ref
    # memory_id = md5("{日期}#{冷库id}")[:12]，所以按日期把温层 ref 也换算成 id 来比
    from services.B3_header_engine.cold_locate import memory_id_for
    warm_ids: dict[str, set] = {}
    for day, refs in warm_refs.items():
        warm_ids[day] = {memory_id_for(day, cid) for cid in refs}
    orphan = 0
    legacy = 0
    for r in rows:
        day = str(r['source_date'] or '')[:10]
        if day not in warm_days:
            legacy += 1                       # 该日期没有温层文件 → v1 迁移行，正常
            continue
        if r['memory_id'] not in warm_ids.get(day, set()):
            orphan += 1
    conn.close()

    counts['index_rows'] = index_rows
    counts['index_orphan'] = orphan
    counts['index_legacy'] = legacy
    counts['index_dup'] = index_dup
    counts['index_empty'] = index_empty
    if orphan:
        problems.append(f'索引有 {orphan} 行是孤儿（该日期有温层文件，但 memory_id 不在温层里）')
    if index_dup:
        problems.append(f'索引有 {index_dup} 行摘要完全重复（重复占位，稀释检索名额）')
    if index_empty:
        problems.append(f'索引有 {index_empty} 行空摘要（垃圾行）')
    if pending > max(50, int(cold_lines * 0.3)):
        problems.append(f'冷库有 {pending} 行还没提炼（超过冷库的 30%）—— 提炼可能卡住了')

    return {
        'ok': not problems,
        'checked_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'counts': counts,
        'problems': problems,
        'legacy': {'index_legacy_rows': legacy,
                   'note': '这些行没有对应温层文件（v1 迁移来的历史），**正常**；'
                           '不要用 index_warm(prune=True) 去处理，那会把它们全删'},
    }


def report(result: dict | None = None, limit_problems: int = 10) -> str:
    """把体检结果排成人看的报告（`libmem_status` 也用这份措辞）。"""
    res = result or check_chain()
    c = res['counts']
    lines = ['整链体检（冷库 → 温层 → 索引）', f"  时间    {res['checked_at']}",
             '  规模']
    lines.append(f"          冷库 {c['cold_days']} 天 / {c['cold_lines']} 行      "
                 f"温层 {c['warm_days']} 天 / {c['warm_entries']} 条      "
                 f"索引 {c['index_rows']} 行")
    lines.append('  对账')
    lines.append(f"          待提炼 {c['cold_unrefined']} 行  ·  索引孤儿 {c['index_orphan']} 行  ·  "
                 f"历史行 {c['index_legacy']} 行（无温层对应，正常）")
    lines.append(f"          重复：冷库 {c['cold_dup']} 行 · 索引同摘要 {c['index_dup']} 组 · "
                 f"温层 ref 失效 {c['warm_no_cold']} 条 · 空摘要 {c['index_empty']} 行")
    if res['problems']:
        lines.append(f"  结论    ❌ 发现 {len(res['problems'])} 处问题")
        for problem in res['problems'][:limit_problems]:
            lines.append(f"          - {problem}")
    else:
        lines.append('  结论    ✅ 三层对得上，没有发现掉件或重复')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description='记忆系统整链体检（只读）')
    ap.add_argument('--json', action='store_true', help='输出 JSON 而不是报告')
    ap.add_argument('--no-heartbeat', action='store_true', help='不写心跳记录')
    args = ap.parse_args()

    result = check_chain()

    # ★ P1：体检本身也是一个工位，写一条心跳（失败不影响体检）
    if not args.no_heartbeat:
        try:
            from services.shared import heartbeat
            heartbeat.stamp('chain.check', 'ok' if result['ok'] else 'partial',
                            counts=result['counts'],
                            note='; '.join(result['problems'][:3]) or '三层对得上')
        except Exception:                   # noqa: BLE001
            pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report(result))
        print(f"\n  历史行说明：{result['legacy']['note']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
