"""B3 重复行去重。

背景
----
`memories` 表里有成片**摘要完全相同**的行：「用户请求中断对话以使用工具」6 条、
「[冷] [Request interrupted by user]」6 条、「用户请求继续之前的对话…」3 条 …
来源是 v1 迁移时 jsonl / md 双份导入。

后果：检索时同一句话重复占位，浪费 token 预算、稀释结果，也让 Phase2 的
Top-K 名额被同一个意思吃掉。

策略
----
1. 按 `summary`（strip 后完全相同）分组；
2. 每组保留一条「最有价值」的：
   - 优先 `heat` 最高，其次 `count` 最多，
   - 再其次 `layer` 更热（hot > warm > cold），
   - 最后 `created_at` 更早（最早出现的那条）。
3. **把被删行的 heat / count 合并进幸存者**，避免丢掉使用历史
   （这些历史是 B5 的输入，丢了等于把记忆的热度抹平）；
4. 顺带清掉摘要为空的垃圾行（它们检索时毫无价值）。

安全
----
- `--dry-run` 默认行为：只报告不写入。
- 写入前自动用 SQLite backup API 备份到 `header.db.bak-<时间戳>`。
- `memory_id` 不在任何 `frames` 外键里，删除是安全的（已核对 schema）。

用法::

    python dedupe_b3.py              # 预览（不改）
    python dedupe_b3.py --apply      # 备份后执行
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'index', 'header.db',
)

_LAYER_RANK = {'hot': 2, 'warm': 1, 'cold': 0}


def _rank(row: sqlite3.Row) -> tuple:
    """排序键：越大越该留下。"""
    return (
        float(row['heat'] or 0.0),
        int(row['count'] or 0),
        _LAYER_RANK.get(str(row['layer'] or ''), 0),
        # created_at 越早越优先 → 取负号让「更早」排前面（配合 reverse 使用）
    )


def find_duplicates(conn: sqlite3.Connection):
    """返回 (分组列表, 空摘要行列表)。每组含全部同摘要行，已按保留优先级排序。"""
    rows = conn.execute(
        """SELECT memory_id, summary, heat, count, layer, task_type,
                  session_id, time_bucket, created_at
           FROM memories"""
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    empty = []
    for row in rows:
        summary = (row['summary'] or '').strip()
        if not summary:
            empty.append(row)
            continue
        groups.setdefault(summary, []).append(row)

    dupes = []
    for summary, members in groups.items():
        if len(members) < 2:
            continue
        # 先按 created_at 升序（早的在前），再按价值降序 → 稳定可预期
        members.sort(key=lambda r: (str(r['created_at'] or ''),))
        members.sort(key=_rank, reverse=True)
        dupes.append((summary, members))

    dupes.sort(key=lambda item: -len(item[1]))
    return dupes, empty


def backup(path: str) -> str:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = f'{path}.bak-{stamp}'
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)          # SQLite 在线备份，包含 WAL 中的已提交数据
    finally:
        dst.close()
        src.close()
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description='B3 重复行去重')
    parser.add_argument('--apply', action='store_true', help='真正执行（默认只预览）')
    parser.add_argument('--db', default=DB, help=f'数据库路径（默认 {DB}）')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f'找不到数据库: {args.db}')
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    dupes, empty = find_duplicates(conn)
    doomed = sum(len(m) - 1 for _, m in dupes) + len(empty)

    print(f'数据库      : {args.db}')
    print(f'当前总行数  : {total}')
    print(f'重复组数    : {len(dupes)}')
    print(f'重复行总数  : {sum(len(m) for _, m in dupes)}')
    print(f'可删除行数  : {doomed}  （每组保留 1 条 + 清掉 {len(empty)} 条空摘要）')
    print(f'去重后      : {total - doomed}')
    print()

    print('--- 最大的 8 个重复组 ---')
    for summary, members in dupes[:8]:
        keep = members[0]
        print(f"  ×{len(members)}  {summary[:56]}")
        print(f"        保留 {keep['memory_id']} heat={keep['heat']} count={keep['count']} layer={keep['layer']}")
        for m in members[1:3]:
            print(f"        删除 {m['memory_id']} heat={m['heat']} count={m['count']}")
        if len(members) > 3:
            print(f"        …还有 {len(members) - 3} 条")

    if empty:
        print(f'\n--- 空摘要行 {len(empty)} 条（一并删除）---')
        for m in empty[:3]:
            print(f"  {m['memory_id']} layer={m['layer']}")

    if not args.apply:
        print('\n这是预览（未改动任何数据）。加 --apply 执行。')
        conn.close()
        return 0

    # ---- 执行 ----
    dest = backup(args.db)
    print(f'\n已备份 -> {dest}')

    merged_heat = 0.0
    merged_count = 0
    deleted = 0
    try:
        conn.execute('BEGIN')
        for _summary, members in dupes:
            keep = members[0]
            extra_heat = sum(float(m['heat'] or 0.0) for m in members[1:])
            extra_count = sum(int(m['count'] or 0) for m in members[1:])
            if extra_heat or extra_count:
                conn.execute(
                    'UPDATE memories SET heat = heat + ?, count = count + ? WHERE memory_id = ?',
                    (extra_heat, extra_count, keep['memory_id']),
                )
                merged_heat += extra_heat
                merged_count += extra_count
            for m in members[1:]:
                conn.execute('DELETE FROM memories WHERE memory_id = ?', (m['memory_id'],))
                deleted += 1
        for m in empty:
            conn.execute('DELETE FROM memories WHERE memory_id = ?', (m['memory_id'],))
            deleted += 1
        conn.commit()
    except Exception as error:  # noqa: BLE001
        conn.rollback()
        print(f'失败已回滚: {type(error).__name__}: {error}')
        conn.close()
        return 1

    after = conn.execute('SELECT count(*) FROM memories').fetchone()[0]
    layers = {
        r['layer']: r['n']
        for r in conn.execute('SELECT layer, count(*) n FROM memories GROUP BY layer')
    }
    conn.close()

    print(f'删除行数    : {deleted}')
    print(f'合并热度    : heat +{merged_heat}  count +{merged_count}（并入幸存者，不丢使用历史）')
    print(f'去重后行数  : {after}（原 {total}）')
    print(f'分层        : {layers}')
    print(f'\n如需回滚: copy "{dest}" "{args.db}"')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
