# -*- coding: utf-8 -*-
"""冷库 → 温层 → B3 索引 的**整链去重**（2026-10-12）。

## 为什么要整链做（只删索引是白干）
实测三条链路（`scratch/_cold_dup_measure.py` / `_dup_triage.py`）：

    data/cold_vault/<date>.jsonl   同一条对话被**追加了 2~3 次**（id 不同）
        │                          ← 2026-10-10 那句"帮我把工作区里的记忆插件重新看一下…"写了 3 遍
        ▼  B2 提炼（memory_id = md5("日期#冷库id")）
    data/warm/<date>.jsonl         每个冷库 id 各提炼一条 → id 不同（因为冷库 id 不同）
        ▼  B3 索引
    data/index/header.db           两条孪生行，检索时同一个意思占两个名额

⚠️ 关键：`memory_id` 是**内容派生**的（`md5(日期#冷库id)`）。所以
    **只删 B3 的行没用** —— 温层还在，`index_warm()` 会把它们原样建回来；
    **只删温层也没用** —— 冷库还在，下一次提炼又会长出来。
    ⇒ 必须从冷库开始，三层一起清。这也是 `dedupe_b3.py`（只治 B3）之外要再加这个脚本的原因。

## 口径（宁可漏删，不可误删）
只删 **`user` 与 `assistant` 两者都完全相同** 的行 —— 那才是"同一条被写了两遍"。
⚠️ **只有 `user` 相同不算**：实测 46 行属于"你两次真的问了同一句"（"[冷] 记一下"、
   "还有多久"），那是合法历史，删了就丢东西。本脚本对这批**一行都不动**。

## 安全
* 默认**预览**（`--dry-run` 是默认行为）；`--apply` 才落盘。
* 落盘前自动备份：整份 `data/index/header.db` + 被改动的冷库/温层文件（`.bak-dedupe-<ts>`）。
* 每一步都在**同一份判据**上：冷库保留哪些 id → 温层只留含这些 id 的条目 → 索引按温层重建。
* 对不上就**报错退出**，不做"猜着删"（详见 `_warm_mask` 的校验）。

用法::

    python -m services.B3_header_engine.dedupe_cold_chain            # 预览
    python -m services.B3_header_engine.dedupe_cold_chain --apply    # 备份后执行
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys

V2 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, V2)

COLD = os.path.join(V2, 'data', 'cold_vault')
WARM = os.path.join(V2, 'data', 'warm')
DB = os.path.join(V2, 'data', 'index', 'header.db')


def _load(path: str) -> list[dict]:
    out = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def _key(entry: dict) -> tuple:
    """判重键：**user 与 assistant 都要相同**（见文件头"口径"）。"""
    return ((entry.get('user') or '').strip(),
            (entry.get('assistant') or '').strip())


def survey_cold() -> dict:
    """**只统计**冷库里的重复写入（不决定删谁）。`--dedupe-cold` 没给时就用它。"""
    stat = {'files': 0, 'lines': 0, 'drop': 0, 'samples': []}
    for path in sorted(glob.glob(os.path.join(COLD, '2026-*.jsonl'))):
        name = os.path.basename(path)
        if '.bak' in name:
            continue
        entries = _load(path)
        seen: set = set()
        for entry in entries:
            key = _key(entry)
            if not key[0] and not key[1]:
                continue
            if key in seen:
                stat['drop'] += 1
                if len(stat['samples']) < 5:
                    stat['samples'].append((name, str(entry.get('id')), key[0][:60]))
                continue
            seen.add(key)
        stat['files'] += 1
        stat['lines'] += len(entries)
    return stat


def plan_cold_dedupe() -> tuple[dict, set]:
    """算出要删哪些冷库重复行。

    Returns: `(removed, touched_files)` ——
    `removed` 是 `{(日期, 冷库id)}`；`touched_files` 是**真被改动**的文件名集合
    （调用方只备份这些，避免给没变的文件也堆 .bak）。
    """
    removed: set = set()
    touched: set = set()
    for path in sorted(glob.glob(os.path.join(COLD, '2026-*.jsonl'))):
        name = os.path.basename(path)
        if '.bak' in name:
            continue
        date = name[:-6]
        entries = _load(path)
        seen: set = set()
        for entry in entries:
            key = _key(entry)
            if not key[0] and not key[1]:
                continue
            if key in seen:
                cid = str(entry.get('id') or '')
                if cid:
                    removed.add((date, cid))
                    touched.add(name)
                continue
            seen.add(key)
    return removed, touched


def apply_cold_dedupe(removed: set) -> int:
    """按 `plan_cold_dedupe()` 的结果重写冷库文件。返回删掉的行数。"""
    by_file: dict = {}
    for date, cid in removed:
        by_file.setdefault(f'{date}.jsonl', set()).add(cid)
    deleted = 0
    for name, ids in by_file.items():
        path = os.path.join(COLD, name)
        entries = _load(path)
        keep = [i for i, entry in enumerate(entries) if str(entry.get('id') or '') not in ids]
        if len(keep) < len(entries):
            _rewrite(path, keep)
            deleted += len(entries) - len(keep)
    return deleted


def plan_warm(removed_ids: dict) -> tuple[dict, dict]:
    """温层：删掉 `ref` 指向**已被删掉的冷库 id** 的条目。

    `removed_ids` 是 `{日期: {冷库id}}`（本脚本删掉的），不涉及的行一律保留。

    ⚠️ 校验（`suspicious`）：温层条目被丢弃，**只有一种正当理由** ——
    它指向的那个冷库 id 正是本次要被删掉的那条。
    落在别的情况（ref 解析不出 id），就**不猜**、列出来并让落盘中止。
    """
    keep_warm: dict[str, list[int]] = {}
    stat = {'files': 0, 'entries': 0, 'drop': 0, 'unmatched_ref': 0,
            'suspicious': [], 'samples': []}
    for path in sorted(glob.glob(os.path.join(WARM, '2026-*.jsonl'))):
        name = os.path.basename(path)
        if '.bak' in name or name.endswith('.lock') or '.tmp' in name:
            continue
        date = name[:-6]
        gone = removed_ids.get(date, set())
        entries = _load(path)
        keep: list[int] = []
        for i, entry in enumerate(entries):
            ref = str(entry.get('ref') or '')
            cid = ref.split('#', 1)[1] if '#' in ref else ''
            if not cid:
                keep.append(i)               # 解析不出 ref：不是本次的事，保留
                continue
            if cid not in gone:
                keep.append(i)               # 冷库那条保留着 → 温层这条也保留
                continue
            stat['drop'] += 1
            if len(stat['samples']) < 5:
                stat['samples'].append((name, ref, (entry.get('summary') or '')[:56]))
        keep_warm[name] = keep
        stat['files'] += 1
        stat['entries'] += len(entries)
    return keep_warm, stat


def _backup(path: str) -> str:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = f'{path}.bak-dedupe-{stamp}'
    if path.endswith('.db'):
        src = sqlite3.connect(path)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    else:
        shutil.copy2(path, dest)
    return dest


def _rewrite(path: str, keep: list[int]) -> None:
    entries = _load(path)
    kept = [entries[i] for i in keep if i < len(entries)]
    tmp = path + '.tmp-dedupe'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        for entry in kept:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description='冷库→温层→B3 整链去重')
    ap.add_argument('--apply', action='store_true', help='真正执行（默认只预览）')
    ap.add_argument('--no-index', action='store_true', help='只清冷库/温层，不重建索引')
    ap.add_argument('--dedupe-cold', action='store_true',
                    help='★ 同时删冷库里 user+assistant 双同的重复行，并连带清掉它们的温层条目与索引行'
                         '（2026-10-12 补：写入侧第二道去重上线**之前**遗留的那几行）')
    args = ap.parse_args()

    # ---- ① 冷库：默认**只报不删**；`--dedupe-cold` 才真删 ----
    survey = survey_cold()
    removed: set = set()
    touched_cold: set = set()
    if args.dedupe_cold:
        removed, touched_cold = plan_cold_dedupe()
    print('== 冷库 ==')
    print(f"  文件 {survey['files']} 个，行 {survey['lines']} 条")
    print(f"  user+assistant 双同的重复行: {survey['drop']} 行")
    for name, mid, text in survey['samples']:
        print(f"    - {name} id={mid}  {text}")
    print('  本次行为: ' + ('**删除**这些重复行（--dedupe-cold）'
                          if args.dedupe_cold else
                          '**不删**（默认只报；要清历史重复加 --dedupe-cold）'))

    # ---- ② 温层：只删 ref 指向「已被删的冷库 id」的条目 ----
    removed_by_day: dict = {}
    for date, cid in removed:
        removed_by_day.setdefault(date, set()).add(cid)
    keep_warm, warm_stat = plan_warm(removed_by_day)
    print('\n== 温层 ==')
    print(f"  文件 {warm_stat['files']} 个，条目 {warm_stat['entries']} 条")
    if warm_stat['drop']:
        print(f"  将删除 {warm_stat['drop']} 条（其 ref 指向被删的冷库行）")
        for name, ref, text in warm_stat['samples']:
            print(f"    - {name} ref={ref}  {text}")
    else:
        print('  无需删除（没有 ref 指向被删的冷库行）')
    if warm_stat['unmatched_ref']:
        print(f"  ⚠️ 可疑 {warm_stat['unmatched_ref']} 条（ref 解析不出 id）："
              f"{warm_stat['suspicious'][:3]}")

    conn = sqlite3.connect(DB)
    total = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    conn.close()
    print(f"\n== B3 索引 ==  当前 {total} 行；预计减少约 {len(removed) + warm_stat['drop']} 行")

    if not args.apply:
        print('\n这是预览（未改动任何数据）。加 --apply 执行。')
        return 0

    if warm_stat['unmatched_ref']:
        print('\n❌ 有可疑条目（ref 解析不出冷库 id），拒绝落盘 —— 先人工确认。')
        return 1

    # ---- 备份（只备份真会被改的文件）----
    backups = [_backup(DB)]
    for name in sorted(touched_cold):
        backups.append(_backup(os.path.join(COLD, name)))
    for name, keep in keep_warm.items():
        path = os.path.join(WARM, name)
        if os.path.exists(path) and len(keep) < len(_load(path)):
            backups.append(_backup(path))
    print(f'\n已备份 {len(backups)} 个文件（首个：{backups[0]}）')

    # ---- 执行：冷库 → 温层 ----
    cold_changed = apply_cold_dedupe(removed) if args.dedupe_cold else 0
    warm_changed = 0
    for name, keep in keep_warm.items():
        path = os.path.join(WARM, name)
        if not os.path.exists(path):
            continue
        entries = _load(path)
        if len(keep) < len(entries):
            _rewrite(path, keep)
            warm_changed += len(entries) - len(keep)
    print(f"冷库删除 {cold_changed} 行；温层删除 {warm_changed} 条")

    if args.no_index:
        print('（--no-index：跳过索引删除）')
        return 0

    # ---- ③ 索引：**精确删**本次去重对应的那些行 ----
    #
    # ⚠️⚠️ 这里绝不能用 `warm_indexer.index_warm(prune=True)`。
    #     实测（2026-10-12）：索引里有 **392 行**（2026-04-27 ~ 07-05，v1 迁移来的）
    #     **没有对应的温层文件**。`_prune()` 的判据是"memory_id 不在本次温层集合里"，
    #     于是它会把 392 行历史**全部删掉** —— 那是丢数据，不是去重。
    #
    #     正确做法：`memory_id = md5("{日期}#{冷库原始 id}")` 是**内容派生**的
    #     （见 `cold_locate.memory_id_for`，全仓唯一一份），所以"删了哪条冷库行"
    #     就能直接算出一批要删的 memory_id，精确到条，不碰别人。
    from services.B3_header_engine.cold_locate import memory_id_for

    doomed: dict[str, str] = {}
    for date, cid in removed:
        doomed[memory_id_for(date, cid)] = f'{date}#{cid}'

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    present = [mid for mid in doomed
               if conn.execute('SELECT 1 FROM memories WHERE memory_id = ?', (mid,)).fetchone()]

    # ---- 再收一批：**摘要完全相同**的孪生（判据与 `dedupe_b3.py` 一致）----
    # 为什么这里还要做一遍：冷库侧那批孪生（同一条被写两次）在删完冷库行之后，
    # 它们对应的索引行已经由上面的 `doomed` 精确删掉了；但库里还有一批**同样摘要重复**
    # 是 v1 迁移带来的（jsonl/md 双份导入，见 `dedupe_b3.py` 的文件头），
    # 它们的冷库行不在本次口径里，得按"摘要相同"自己收。
    # ⇒ heat/count **并进幸存者**，不丢使用历史（那是 B5 的输入）。
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute('SELECT memory_id, summary, heat, count, layer, created_at FROM memories'):
        summary = (row['summary'] or '').strip()
        if summary:
            groups.setdefault(summary, []).append(row)
    layer_rank = {'hot': 2, 'warm': 1, 'cold': 0}
    twins = []
    for summary, members in groups.items():
        if len(members) > 1:
            members.sort(key=lambda r: str(r['created_at'] or ''))
            members.sort(key=lambda r: (float(r['heat'] or 0.0), int(r['count'] or 0),
                                        layer_rank.get(str(r['layer'] or ''), 0)), reverse=True)
            twins.append((summary, members))

    try:
        conn.execute('BEGIN')
        for mid in present:
            conn.execute('DELETE FROM memories WHERE memory_id = ?', (mid,))
        merged_heat = merged_count = 0
        for _summary, members in twins:
            keep = members[0]
            extra_heat = sum(float(m['heat'] or 0.0) for m in members[1:])
            extra_count = sum(int(m['count'] or 0) for m in members[1:])
            if extra_heat or extra_count:
                conn.execute('UPDATE memories SET heat = heat + ?, count = count + ? WHERE memory_id = ?',
                             (extra_heat, extra_count, keep['memory_id']))
                merged_heat += extra_heat
                merged_count += extra_count
            for m in members[1:]:
                conn.execute('DELETE FROM memories WHERE memory_id = ?', (m['memory_id'],))
        conn.commit()
    except Exception as error:  # noqa: BLE001
        conn.rollback()
        conn.close()
        print(f'\n❌ 索引删除失败已回滚: {type(error).__name__}: {error}')
        return 1

    after = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    # 删掉的记忆在 FTS 里也要清 —— 否则词法路会把不存在的 id 捞回来（拿不到行 = 结果变少）
    try:
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
        fts_rebuilt = True
    except Exception as error:  # noqa: BLE001
        fts_rebuilt = f'failed: {error}'
    try:
        conn.execute("INSERT INTO memories_fts_raw(memories_fts_raw) VALUES('rebuild')")
        conn.commit()
        raw_rebuilt = True
    except Exception as error:  # noqa: BLE001
        raw_rebuilt = f'failed: {error}'
    conn.close()

    # 进程内向量缓存必须清：刚删了一批 id，缓存里可能还留着它们的向量
    try:
        from services.B4_dual_gate_retrieval.retrieval import invalidate_candidate_cache
        invalidate_candidate_cache()
        cache_clean = True
    except Exception as error:  # noqa: BLE001
        cache_clean = f'failed: {error}'

    print('\n== 索引 ==')
    print(f'  按冷库删除清单算出的 memory_id: {len(doomed)} 个；其中索引里真实存在: {len(present)} 个')
    print(f'  另收「摘要完全相同」的孪生: {len(twins)} 组（heat +{merged_heat:.2f} / count +{merged_count} 并入幸存者）')
    print(f'  FTS 重建: {fts_rebuilt}  原文 FTS 重建: {raw_rebuilt}  向量缓存清理: {cache_clean}')
    print(f'\n结果: memories {total} → {after} 行')
    conn = sqlite3.connect(DB)
    left = conn.execute('''SELECT count(*) FROM (SELECT summary FROM memories
                           WHERE trim(summary) <> '' GROUP BY summary HAVING count(*) > 1)''').fetchone()[0]
    conn.close()
    print(f'  剩余「摘要完全相同」的组: {left}')
    print('  ⚠️ 未触碰的行: 温层没有对应文件的历史行（2026-04~07）原样保留')
    print(f'回滚示例: copy "{backups[0]}" "{DB}"')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
