"""把 audit_log 的主键从 `trace_id` 单列改成 `(trace_id, action)` 复合键。

为什么必须改
------------
架构书 §5.9 要求审计「覆盖 search/write/drilldown/export/delete 全动作」。
一条 trace 里本来就会发生多个动作 —— 例如「检索命中(search) + 命中升温改库(write)」
是同一条因果链的两条证据。

但原 schema 是 `trace_id TEXT PRIMARY KEY`，而 `AuditLogger.log()` 用
`INSERT OR REPLACE`，于是**一个 trace 只能留一条记录**：
实测把升温和检索放进同一 trace_id 后，后写的把先写的顶掉 —— 检索记录直接消失
（audit_log 只 +1 条，且 action=write）。

改成复合主键后：
- 同一条 trace 可以留 search + write 两条，互为证据；
- 同一 (trace_id, action) 重复写仍然是覆盖（幂等），迁移/重跑安全。

SQLite 不支持改主键，所以走标准的「建新表 → 拷数据 → 换名」流程。

用法::

    python fix_audit_log_pk.py            # 预览（不改）
    python fix_audit_log_pk.py --apply    # 备份后执行
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'index', 'header.db',
)

_NEW_DDL = """
CREATE TABLE audit_log__new (
    trace_id          TEXT NOT NULL,
    request_id        TEXT,
    session_id        TEXT,
    ts                TEXT NOT NULL,
    actor             TEXT,
    action            TEXT NOT NULL,
    header_snapshot   TEXT,
    payload_ref       TEXT,
    decided_by        TEXT NOT NULL DEFAULT 'user',
    integrity_hash    TEXT NOT NULL,
    PRIMARY KEY (trace_id, action)
)
"""

_COLS = ('trace_id', 'request_id', 'session_id', 'ts', 'actor', 'action',
         'header_snapshot', 'payload_ref', 'decided_by', 'integrity_hash')


def current_pk(conn: sqlite3.Connection) -> str:
    """返回当前主键描述。"""
    rows = conn.execute('PRAGMA table_info(audit_log)').fetchall()
    pk = [r[1] for r in rows if r[5]]          # r[5] = pk 序号
    return ','.join(pk)


def backup(path: str) -> str:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = f'{path}.bak-auditpk-{stamp}'
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description='修正 audit_log 主键')
    parser.add_argument('--apply', action='store_true', help='真正执行（默认只预览）')
    parser.add_argument('--db', default=DB)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f'找不到数据库: {args.db}')
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    pk_before = current_pk(conn)
    rows = conn.execute('SELECT * FROM audit_log').fetchall()
    print(f'数据库      : {args.db}')
    print(f'当前主键    : {pk_before or "(无)"}')
    print(f'当前记录数  : {len(rows)}')

    if pk_before == 'trace_id,action':
        print('\n主键已经是 (trace_id, action)，无需迁移。')
        conn.close()
        return 0

    print('\n--- 迁移后的效果 ---')
    print('  search 与 write 可以共用同一 trace_id，各留一条，不再互相覆盖')
    print('  同一 (trace_id, action) 重复写仍是覆盖（幂等）')

    if not args.apply:
        print('\n这是预览（未改动任何数据）。加 --apply 执行。')
        conn.close()
        return 0

    dest = backup(args.db)
    print(f'\n已备份 -> {dest}')

    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute('BEGIN')
        conn.execute('DROP TABLE IF EXISTS audit_log__new')
        conn.execute(_NEW_DDL)
        placeholders = ','.join('?' * len(_COLS))
        conn.executemany(
            f'INSERT OR REPLACE INTO audit_log__new ({",".join(_COLS)}) VALUES ({placeholders})',
            [tuple(r[c] for c in _COLS) for r in rows],
        )
        conn.execute('DROP TABLE audit_log')
        conn.execute('ALTER TABLE audit_log__new RENAME TO audit_log')
        # 重建索引（DROP TABLE 时旧索引一起没了）
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_ts     ON audit_log(ts)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_trace  ON audit_log(trace_id)')
        conn.execute('COMMIT')
        conn.execute('PRAGMA foreign_keys = ON')
    except Exception as error:  # noqa: BLE001
        conn.execute('ROLLBACK')
        print(f'失败已回滚: {type(error).__name__}: {error}')
        conn.close()
        return 1

    pk_after = current_pk(conn)
    after = conn.execute('SELECT count(*) FROM audit_log').fetchone()[0]
    print(f'新主键      : {pk_after}')
    print(f'迁移后记录数: {after}（原 {len(rows)}）')

    ok = pk_after == 'trace_id,action' and after == len(rows)
    print('\n' + ('✅ 迁移成功' if ok else '❌ 结果不符预期，请用备份回滚'))
    print(f'如需回滚: copy "{dest}" "{args.db}"')
    conn.close()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
