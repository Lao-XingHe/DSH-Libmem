# -*- coding: utf-8 -*-
"""索引孤儿行清理（2026-10-12）。

## 什么是"孤儿"
`memories` 里有一行的 `memory_id` 既不在温层集合里、又反查不出任何冷库 id。
它们不是"找不到原文"，而是**同一个 `ref` 被提炼了两次**（旧版 `id` 规则 / 早期 schema
版本各留了一行）：温层重提炼后算出新的 id，`bulk_insert` 是 **UPSERT**，
新行加进去、**旧行没人删** ⇒ 同一个意思在库里占两行。

`check_chain` 会把它们报出来（`index_orphan`）。这个脚本负责**安全地**收掉。

## 判据（**宁可留着报出来，也不猜着删**）
对每条孤儿，只在**同一天**的温层条目里找最相似的一条，要求：
1. 字符级相似度（`difflib`）≥ `--threshold`（默认 0.72）；**且**
2. 词元级 Jaccard（`jieba` 分词后集合交并比）≥ 0.5。
两条都过才认定"这就是同一条的老版本"，删孤儿、并把它的 `heat/count` **并进幸存者**。
两条不过 → **保留**，计入 `unmatched` 并在报告里列出来（那说明它可能是独立信息，
需要人看一眼，不是脚本该替他决定的）。

⚠️ 与 `index_warm(prune=True)` 的区别：`prune` 的判据是"不在温层集合里就删"，
会把 2026-04~07 那 392 行**没有温层文件**的历史行一起删掉（2026-10-12 实测踩到）。
这里只删"**能在温层里找到对应那条**"的孤儿，历史行天然不满足条件，所以安全。

## 用法
    python -m services.B3_header_engine.repair_orphans                 # 预览
    python -m services.B3_header_engine.repair_orphans --apply         # 备份后执行
    python -m services.B3_header_engine.repair_orphans --dedupe-summary  # 顺带收同摘要重复
"""

from __future__ import annotations

import argparse
import collections
import datetime
import difflib
import glob
import json
import os
import shutil
import sqlite3
import sys

V2 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, V2)
sys.path.insert(0, os.path.join(V2, 'services'))

COLD = os.path.join(V2, 'data', 'cold_vault')
WARM = os.path.join(V2, 'data', 'warm')
DB = os.path.join(V2, 'data', 'index', 'header.db')

_LAYER_RANK = {'hot': 2, 'warm': 1, 'cold': 0}


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


def _tokens(text: str) -> set:
    """词元集合（jieba 可用就用，否则退化为字符 bigram）。"""
    text = (text or '').strip()
    if not text:
        return set()
    try:
        import jieba
        return {t for t in jieba.cut(text) if t.strip()}
    except Exception:                        # noqa: BLE001
        return {text[i:i + 2] for i in range(max(1, len(text) - 1))}


def _similar(a: str, b: str) -> tuple[float, float]:
    """返回 `(字符相似度, 词元 Jaccard)`。"""
    ratio = difflib.SequenceMatcher(None, a or '', b or '').ratio()
    ta, tb = _tokens(a), _tokens(b)
    jac = (len(ta & tb) / len(ta | tb)) if (ta or tb) else 0.0
    return ratio, jac


def _warm_index() -> tuple[dict, dict]:
    """`({日期: [(memory_id, summary)]}, {日期: 文件 mtime})`。"""
    by_day: dict = {}
    mtimes: dict = {}
    for path in glob.glob(os.path.join(WARM, '20*.jsonl')):
        name = os.path.basename(path)
        if '.bak' in name or name.endswith('.lock') or '.tmp' in name:
            continue
        day = name[:-6]
        mtimes[day] = os.path.getmtime(path)
        entries = []
        for entry in _load_jsonl(path):
            mid = str(entry.get('id') or '')
            if mid:
                entries.append((mid, (entry.get('summary') or '').strip()))
        by_day[day] = entries
    return by_day, mtimes


def plan(threshold: float = 0.72, jaccard: float = 0.5) -> dict:
    """算出该删哪些孤儿行、配对的幸存者是谁。**只读。**"""
    warm_by_day, warm_mtime = _warm_index()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT memory_id, summary, source_date, heat, count, layer, created_at FROM memories').fetchall()

    warm_ids = {day: {mid for mid, _ in entries} for day, entries in warm_by_day.items()}
    plan_rows = []
    unmatched = []
    legacy_skipped = 0
    for row in rows:
        day = str(row['source_date'] or '')[:10]
        if day not in warm_mtime:
            legacy_skipped += 1               # 没有温层文件 → v1 历史行，不动
            continue
        if row['memory_id'] in warm_ids.get(day, set()):
            continue                          # 正常行
        summary = (row['summary'] or '').strip()
        best = None
        for mid, wsummary in warm_by_day.get(day, []):
            ratio, jac = _similar(summary, wsummary)
            score = (ratio + jac) / 2
            if best is None or score > best[0]:
                best = (score, ratio, jac, mid, wsummary)
        if best and best[1] >= threshold and best[2] >= jaccard:
            plan_rows.append({'orphan': dict(row), 'keep': best[3],
                              'ratio': round(best[1], 3), 'jaccard': round(best[2], 3),
                              'keep_summary': best[4][:56]})
        else:
            unmatched.append({'memory_id': row['memory_id'], 'day': day,
                              'summary': summary[:56],
                              'best_ratio': round(best[1], 3) if best else 0.0})
    conn.close()
    return {'matched': plan_rows, 'unmatched': unmatched,
            'legacy_skipped': legacy_skipped}


def _backup(path: str) -> str:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = f'{path}.bak-orphanrepair-{stamp}'
    if path.endswith('.db'):
        src, dst = sqlite3.connect(path), sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    else:
        shutil.copy2(path, dest)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description='索引孤儿行清理（保守判据）')
    ap.add_argument('--apply', action='store_true', help='真正执行（默认预览）')
    ap.add_argument('--threshold', type=float, default=0.72, help='字符相似度下限')
    ap.add_argument('--jaccard', type=float, default=0.5, help='词元 Jaccard 下限')
    ap.add_argument('--dedupe-summary', action='store_true',
                    help='顺带收「摘要完全相同」的重复行（判据同 dedupe_b3）')
    args = ap.parse_args()

    result = plan(args.threshold, args.jaccard)
    matched = result['matched']

    conn = sqlite3.connect(DB)
    total = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    conn.close()

    print('== 孤儿行（memory_id 不在同一天温层里）==')
    print(f"  可配对（同天温层里有高度相似的一条）: {len(matched)} 行")
    for item in matched[:8]:
        print(f"    - {item['orphan']['memory_id']}  ratio={item['ratio']} jac={item['jaccard']}")
        print(f"        删: {(item['orphan']['summary'] or '')[:56]}")
        print(f"        留: {item['keep_summary']}")
    if result['unmatched']:
        print(f"  ⚠️ 认不出对应关系（**保留**，需人看一眼）: {len(result['unmatched'])} 行")
        for item in result['unmatched'][:8]:
            print(f"    - [{item['day']}] {item['memory_id']} best_ratio={item['best_ratio']}  {item['summary']}")
    print(f"  跳过：{result['legacy_skipped']} 行没有温层文件的历史行（正常，不动）")

    twins = []
    if args.dedupe_summary:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        groups: dict = {}
        for row in conn.execute('SELECT memory_id, summary, heat, count, layer, created_at FROM memories'):
            s = (row['summary'] or '').strip()
            if s:
                groups.setdefault(s, []).append(row)
        for summary, members in groups.items():
            if len(members) > 1:
                members.sort(key=lambda r: str(r['created_at'] or ''))
                members.sort(key=lambda r: (float(r['heat'] or 0.0), int(r['count'] or 0),
                                            _LAYER_RANK.get(str(r['layer'] or ''), 0)), reverse=True)
                twins.append((summary, members))
        conn.close()
        print(f"\n== 同摘要重复 ==   {len(twins)} 组（保留最热的，heat/count 并入幸存者）")
        for summary, members in twins[:5]:
            print(f"    x{len(members)}  {summary[:56]}")

    doomed = len(matched) + sum(len(m) - 1 for _, m in twins)
    print(f"\n索引 {total} 行 → 预计删除 {doomed} 行")

    if not args.apply:
        print('\n这是预览（未改动任何数据）。加 --apply 执行。')
        return 0

    dest = _backup(DB)
    print(f'\n已备份 -> {dest}')
    conn = sqlite3.connect(DB)
    try:
        conn.execute('BEGIN')
        merged_heat = merged_count = deleted = 0
        for item in matched:
            orphan = item['orphan']
            if float(orphan['heat'] or 0) or int(orphan['count'] or 0):
                conn.execute('UPDATE memories SET heat = heat + ?, count = count + ? WHERE memory_id = ?',
                             (float(orphan['heat'] or 0), int(orphan['count'] or 0), item['keep']))
                merged_heat += float(orphan['heat'] or 0)
                merged_count += int(orphan['count'] or 0)
            conn.execute('DELETE FROM memories WHERE memory_id = ?', (orphan['memory_id'],))
            deleted += 1
        for _summary, members in twins:
            keep = members[0]
            extra_heat = sum(float(m['heat'] or 0.0) for m in members[1:])
            extra_count = sum(int(m['count'] or 0) for m in members[1:])
            if extra_heat or extra_count:
                conn.execute('UPDATE memories SET heat = heat + ?, count = count + ? WHERE memory_id = ?',
                             (extra_heat, extra_count, keep['memory_id']))
                merged_heat += extra_heat
                merged_count += extra_count
            for member in members[1:]:
                conn.execute('DELETE FROM memories WHERE memory_id = ?', (member['memory_id'],))
                deleted += 1
        conn.commit()
    except Exception as error:                 # noqa: BLE001
        conn.rollback()
        conn.close()
        print(f'❌ 失败已回滚: {type(error).__name__}: {error}')
        return 1

    after = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    for table in ('memories_fts', 'memories_fts_raw'):
        try:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
            conn.commit()
        except Exception as error:             # noqa: BLE001
            print(f'  ⚠️ {table} 重建失败: {error}')
    conn.close()
    try:
        from services.B4_dual_gate_retrieval.retrieval import invalidate_candidate_cache
        invalidate_candidate_cache()
    except Exception:                          # noqa: BLE001
        pass

    print(f'删除 {deleted} 行；heat +{merged_heat:.2f} / count +{merged_count} 并入幸存者')
    print(f'索引 {total} → {after} 行')
    print(f'回滚: copy "{dest}" "{DB}"')

    # 写一条心跳（清理本身也是一个工位事件）
    try:
        from services.shared import heartbeat
        heartbeat.stamp('chain.repair', 'ok', counts={'deleted': deleted, 'before': total, 'after': after},
                        note='孤儿行清理 + 同摘要去重')
    except Exception:                          # noqa: BLE001
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
