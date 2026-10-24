# -*- coding: utf-8 -*-
"""管线心跳 —— 每个工位跑完写一条"我干完了、结果如何"（2026-10-12）。

## 为什么需要它（这条是"管线没做好"的第②项）
管线有六个工位（冷库 → 提炼 → 索引 → 检索 → 反馈 → 衰减），此前它们**卡住时都不吭声**。
最典型的一次事故：用户让把 llama-server 改成"中午/晚上定时开"，
**记忆里只留下"正在勘察"，做没做成、什么时候做成的，库里查不到** ——
只能靠人去翻 Windows 计划任务才确认。工位之间"货交出去没有、对面收了没有"，
没有任何一处统一记录。

## 设计（刻意做得很小）
* 一个 **JSONL 追加文件**（默认 `data/index/chain_heartbeat.jsonl`），一行一个事件：
  `{"at": "...", "stage": "b2.extract", "status": "ok", "counts": {...}, "note": ""}`
* 追加时用 `shared.atomic_io.file_lock` 跨进程互斥（计划任务与桥进程可能同时跑）；
  锁拿不到就**直接放弃写心跳** —— 观测绝不能拖垮主流程。
* 文件超过 `MAX_RECORDS` 行就原子重写为最后 `KEEP_RECORDS` 行（truncate），防止无限增长。
* 读取按**文件尾部**倒着解析，只取需要的那几行（不整文件读进内存）。

## 谁写 / 谁读
写：B2 提炼、B3 索引、`check_chain` 体检、DSH 写入侧（Node 侧也追加同一个文件）
读：`libmem_status` 的「管线心跳」段、右下角「记忆」面板、`check_chain` 报告

⚠️ 心跳文件是**观测数据**，不是真相源。删了不影响任何功能。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES = os.path.dirname(_HERE)
_V2 = os.path.dirname(_SERVICES)
for _p in (_V2, _SERVICES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 相对 `v2/` 的心跳文件位置。**Node 侧也写同一个文件**，所以路径要能从两边拼出来。
REL_PATH = os.path.join('data', 'index', 'chain_heartbeat.jsonl')
#: 超过这个行数就截断。
MAX_RECORDS = 2000
#: 截断后保留多少行。
KEEP_RECORDS = 500
#: 未知 stage 也照写（观测不该因为多了一个工位就报错）。


def path() -> str:
    """心跳文件绝对路径。

    ⚠️ 优先 `SHUFANG_DATA_DIR`（与 `shared/config/loader.py` 的覆盖语义一致），
    否则按本文件往上两级推出 `v2/`。
    """
    override = os.environ.get('SHUFANG_DATA_DIR')
    base = override if override else _V2
    return os.path.join(base, REL_PATH)


def stamp(stage: str, status: str = 'ok', counts: dict | None = None,
          note: str = '', **extra) -> bool:
    """记一条心跳。**永远不抛**（观测失败不许影响主流程）。

    Args:
        stage: 工位名，建议 `b1.write` / `b2.extract` / `b3.index` / `b4.retrieve` /
            `b7.decay` / `chain.check`。`libmem_status` 按这个名字分组显示。
        status: `ok` / `skipped` / `failed` / `partial`。
        counts: 数字型结果（写了几条、跳过几条…）。值必须是 int/float，
            非数字一律转成字符串塞进 `note`，避免读侧拿到意料外的类型。
        note: 一句话说明（人看的）。
        **extra: 想额外留的键，合并进记录的 `extra` 字段。

    Returns: 是否写成功（False 只表示"这次心跳没留下"，不代表主流程出错）。
    """
    record = {
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'stage': str(stage or 'unknown'),
        'status': str(status or 'ok'),
        'counts': {},
        'note': str(note or '')[:400],
        'pid': os.getpid(),
    }
    for key, value in (counts or {}).items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            record['counts'][str(key)] = value
        else:
            record['note'] = (record['note'] + f' {key}={value}').strip()[:400]
    if extra:
        record['extra'] = {str(k): (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
                           for k, v in extra.items()}

    target = path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + '\n'
        _append(target, line)
        _rotate_if_needed(target)
        return True
    except Exception:                       # noqa: BLE001 - 观测失败绝不外溢
        return False


def _append(target: str, line: str) -> None:
    """追加一行；跨进程锁拿不到就退化成本进程直接追加。

    ⚠️ 锁超时必须**比主流程的锁等待短得多**：`file_lock` 默认 20 秒，
    而心跳是观测数据 —— 为了留一条心跳把提炼/索引卡住 20 秒是本末倒置。
    所以这里 1.5 秒拿不到就直接写（追加单行本身在 Windows 上也是原子的）。
    """
    from services.shared.atomic_io import file_lock
    try:
        with file_lock(target, timeout=1.5):
            with open(target, 'a', encoding='utf-8', newline='\n') as fh:
                fh.write(line)
    except Exception:                       # noqa: BLE001
        # 锁被占用超时 / 锁机制本身出问题：裸追加一次，失败就让上层吞掉
        with open(target, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(line)


def _rotate_if_needed(target: str) -> None:
    """行数超上限就原子截断成最后 KEEP_RECORDS 行。失败静默。"""
    try:
        with open(target, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        if len(lines) <= MAX_RECORDS:
            return
        from services.shared.atomic_io import atomic_write_text
        atomic_write_text(target, ''.join(lines[-KEEP_RECORDS:]))
    except Exception:                       # noqa: BLE001
        pass


def _tail_lines(target: str, limit: int) -> list[str]:
    """从文件尾部倒着读，最多取 limit 行（避免把整个文件读进内存）。"""
    if not os.path.exists(target):
        return []
    size = os.path.getsize(target)
    block = 8192
    data = b''
    with open(target, 'rb') as fh:
        pos = size
        while pos > 0 and data.count(b'\n') <= limit:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    text = data.decode('utf-8', errors='replace')
    lines = [ln for ln in text.split('\n') if ln.strip()]
    return lines[-limit:]


def recent(limit: int = 60) -> list[dict]:
    """最近 limit 条心跳（新的在前）。坏行直接跳过 —— 心跳不该让读侧崩。"""
    out: list[dict] = []
    for line in reversed(_tail_lines(path(), max(1, int(limit)))):
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def latest_per_stage(limit: int = 200) -> dict:
    """每个 stage 的**最后一条**心跳 + 出现次数，给状态面板用。

    Returns: `{stage: {"last": {...}, "seen": n}}`。
    """
    seen: dict = {}
    for record in recent(limit):
        stage = str(record.get('stage') or 'unknown')
        slot = seen.setdefault(stage, {'last': None, 'seen': 0})
        slot['seen'] += 1
        if slot['last'] is None:
            slot['last'] = record
    return seen


def summarize(limit: int = 200) -> list[str]:
    """给人看的心跳摘要行（`libmem_status` / `check_chain` 共用一份措辞）。"""
    stages = latest_per_stage(limit)
    if not stages:
        return ['  管线心跳  还没有记录（各工位跑过一次之后就会出现）',
                f'          文件 {path()}']
    order = ['b1.write', 'b2.extract', 'b3.index', 'b4.retrieve', 'b5.heat', 'chain.check']
    keys = sorted(stages, key=lambda k: (order.index(k) if k in order else 99, k))
    lines = []
    for key in keys:
        last = stages[key]['last'] or {}
        counts = last.get('counts') or {}
        ctext = ' '.join(f'{k}={v}' for k, v in counts.items() if v not in (0, None)) or '-'
        lines.append(f"  {key:<14} {str(last.get('status')):<8} {str(last.get('at'))[:19]}  "
                     f"{ctext}  {str(last.get('note') or '')[:60]}")
    lines.append(f"          文件 {path()}")
    return lines


if __name__ == '__main__':
    target = path()
    print(f'心跳文件: {target}')
    if not os.path.exists(target):
        print('（还不存在）')
        raise SystemExit(0)
    print(f'总行数  : {sum(1 for _ in open(target, encoding="utf-8", errors="replace"))}')
    print('\n各工位最近一次:')
    for line in summarize():
        print(line)
    print('\n最近 10 条原始记录:')
    for rec in recent(10):
        print('  ' + json.dumps(rec, ensure_ascii=False))
