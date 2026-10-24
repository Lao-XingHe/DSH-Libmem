import os, json, re, hashlib, sys, threading
from datetime import datetime

from services.shared.atomic_io import atomic_write_text, file_lock


def _eprint(*args, **kwargs):
    """后台日志一律走 stderr（stdio MCP 的 stdout 是 JSON-RPC 通道）。

    提炼现在由 MCP 进程内的冷库轮询自动触发，被 `extract_warm_from_date` 与
    `filter_warm_by_relevance` 调到的模型日志都会经过这里。
    """
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


# ---------------------------------------------------------------- 写入去重（2026-09-23）
#
# 为什么需要：`extract_warm_from_date` 是「先把文件里已有的 id 读进 existing_ids，
# 再逐条提炼并追加」。如果两个线程同时跑同一天（冷库轮询与手动 `memory_extract`
# 是会并发的），两边都在**对方写盘之前**读完了 existing_ids，于是同一个 ref 被写两遍。
# 实测：2026-09-23 的 `ref=2026-09-23#125917342003` 写了两条，createdAt 只差 131ms，
# 两条摘要还不一样 —— 温层就此多出一条重复行（白烧一次小模型调用，
# 而且哪条摘要最终生效变成了「谁后写谁赢」）。
#
# 修法：模块级锁 + 每个日期一份「已写 id」集合。锁只圈住
# 「查重 → 追加 → 记账」这一小段，不圈住慢吞吞的模型调用（那是 6s/条）。
_WRITE_LOCK = threading.Lock()
_WRITTEN_UIDS: dict[str, set] = {}

# 助手回复中的错误模式——这些回复不应该被提炼为记忆
_ERROR_PATTERNS = [
    "抱歉", "请检查", "空回复", "API 配置", "API Key", "模型返回空",
    "backend.exe", "无法连接", "连接失败", "超时", "错误",
    "请确认后端", "未初始化", "请先配置",
]

def _is_error_response(text: str) -> bool:
    """检查助手回复是否属于错误/无意义回复"""
    text_short = text[:100]
    for pattern in _ERROR_PATTERNS:
        if pattern in text_short:
            return True
    if len(text.strip()) < 15:
        return True
    return False

def _get_warm_dir() -> str:
    from services.shared.config.loader import get_data_path
    path = get_data_path("warm_dir", "./data/warm")
    os.makedirs(path, exist_ok=True)
    return path

EXTRACT_PROMPT = """从以下对话记录中提取关键信息，生成一条温记忆 JSON。

格式: {{
  "task_type": "下面七类中的**一个**",
  "summary": "一句话摘要",
  "topics": ["关键词1","关键词2"],
  "entities": ["核心名词/项目/技术，最多4个"],
  "relations": ["主语→谓语→宾语因果短句，最多3条"],
  "temporal": ["时间/阶段标签，最多2个"],
  "drafted": false
}}

★ task_type 的封闭取值（只能用这些，**不许自造**）：{task_values}

★ 标引视角：**这条对话记录的是哪一种活动**（回顾式），不是"用户想干什么"。
  摘要里的动词是有用的线索（决定/改为/采用 → 决策；测试/对比/确认 → 验证）。

task_type 判据：
{task_judges}

规则:
- task_type: 必须从上面七类里选**一个**，不许自造、不许留空
- entities: 禁止提取"AI、项目、东西"等泛词
- relations: 必须是"主语→谓语→宾语"的因果链
- temporal: 优先提取"版本号、月份、阶段名"
- 数组字段如果没有内容则填空数组[]

只返回 JSON，不要其他内容。

对话:
用户: {user}
助手: {assistant}
JSON:"""


def _task_type_slots() -> dict:
    """把封闭取值与判据从 `task_types.yaml` 取出来填进 prompt。

    为什么不在 prompt 里写死：**没有编目规则的字段等于没有字段**。
    取值与判据是「数据」，改 YAML 就能改标引口径，不用动代码。
    取不到时退回一份最小占位（绝不静默编造词表）。
    """
    try:
        from services.shared import task_types as TT
        return {"task_values": " / ".join(TT.values()), "task_judges": TT.judge_block()}
    except Exception:                                            # noqa: BLE001
        return {"task_values": "研究 / 决策 / 验证 / 规划 / 闲聊 / 求助 / 其他",
                "task_judges": "（判据文件不可用，按上面取值就近选择）"}


RELEVANCE_FILTER_PROMPT = """从以下记忆摘要中，选出与用户问题真正相关的条目。返回 JSON 数组，包含相关条目的序号(从0开始)。

用户问题: {question}

{entries}

相关序号数组:"""

def _read_warm_ids(filepath: str) -> set:
    """读文件里现存的 id 集合（写入前的查重用）。"""
    ids = set()
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    ids.add(json.loads(line.strip()).get("id", ""))
                except Exception:
                    pass
    return ids


def _append_warm_entry(filepath: str, payload: dict, date_str: str,
                       uid: str, existing_ids: set) -> str:
    """把一条温记忆追加进当天 jsonl，返回 "ok" / "dup"。

    为什么是「开→写→关」而不是在 for 循环外开一个长期 `open(..., "a")`：
    持有追加句柄时，`update_warm_entry_call_count()` 的整文件重写会踩上来 ——
    重写在 POSIX 上把文件换成新 inode，追加句柄还指着旧的，之后写的全部落进
    一个没人读的文件；在 Windows 上重写直接撞共享冲突失败。每次追加多开一次
    文件的代价，相对每条 6s 的小模型调用可以忽略。

    查重必须在**锁里重读文件**：`existing_ids` 是循环开始时的快照，而模型调用
    期间另一个进程完全可能已经写进了同一条（跨进程去重是 2026-09-25 补的，
    此前只有进程内的 `_WRITE_LOCK`，挡不住另一个解释器实例）。
    """
    with file_lock(filepath):
        with _WRITE_LOCK:
            if uid in _read_warm_ids(filepath):
                return "dup"
            with open(filepath, "a", encoding="utf-8") as wf:
                wf.write(json.dumps(payload, ensure_ascii=False) + "\n")
                wf.flush()
                os.fsync(wf.fileno())
            _WRITTEN_UIDS.setdefault(date_str, set()).add(uid)
            existing_ids.add(uid)
    return "ok"


def extract_warm_from_date(date_str: str, llm) -> int:
    from services.B1_cold_memory.cold_watcher import read_cold_memory
    entries = read_cold_memory(date_str, max_entries=99999)
    if not entries:
        return 0
    warm_dir = _get_warm_dir()
    filepath = os.path.join(warm_dir, f"{date_str}.jsonl")

    existing_ids = set()
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    existing_ids.add(e.get("id", ""))
                except: pass
    # 把「文件里的」和「本进程里别的线程刚写进去的」合起来看 —— 否则并发时
    # 两边都只看到文件里的旧内容，各写一遍（2026-09-23 实测踩到）。
    with _WRITE_LOCK:
        shared = _WRITTEN_UIDS.setdefault(date_str, set())
        shared |= existing_ids
        existing_ids = set(shared)

    new_count = 0
    dup_inflight = 0
    total = len(entries)
    for i, entry in enumerate(entries):
        user = entry.get("user", ""); assistant = entry.get("assistant", "")
        if len(user) < 10:
            if i % 50 == 0:
                _eprint(f"  [{i+1}/{total}] 跳过（用户消息过短）")
            continue
        if _is_error_response(assistant):
            if i % 20 == 0:
                _eprint(f"  [{i+1}/{total}] 跳过（助手回复包含错误信息）")
            continue
        uid = hashlib.md5(f"{date_str}#{entry.get('id','')}".encode()).hexdigest()[:12]
        if uid in existing_ids:
            if i % 20 == 0:
                _eprint(f"  [{i+1}/{total}] 跳过（已存在）")
            continue
        prompt = EXTRACT_PROMPT.format(user=user[:500], assistant=assistant[:500],
                                       **_task_type_slots())
        _eprint(f"  [{i+1}/{total}] 调用小模型 ({llm.small.name})...", end=" ", flush=True)
        result = llm.infer_small_json(prompt, task_type="warm_extract") if llm else {}
        if result and isinstance(result, dict) and "summary" in result:
            payload = dict(result)
            payload["id"] = uid
            payload["ref"] = f"{date_str}#{entry.get('id','')}"
            payload["callCount"] = 0
            payload["createdAt"] = datetime.now().isoformat()
            payload["source_date"] = date_str
            if _append_warm_entry(filepath, payload, date_str, uid, existing_ids) == "dup":
                dup_inflight += 1
                _eprint(f"重复（另一进程/线程已写入同一条 {uid}），丢弃")
                continue
            new_count += 1
            entities = payload.get('entities', [])
            _eprint(f"OK {payload.get('task_type') or payload.get('type','?')} | {payload.get('summary','')[:40]}" + (f" | entities={entities}" if entities else ""))
        else:
            _eprint(f"WARN 未提取到有效摘要")
    if dup_inflight:
        _eprint(f"  [去重] 本轮的并发重复写入被拦下 {dup_inflight} 条（否则温层会多出重复行）")
    # ★ P1 心跳（2026-10-12）：提炼是一个工位，跑完留一条"我干完了、结果如何"。
    #   为什么需要：此前工位卡住时不吭声 —— 实测有过"记忆里只留下'正在勘察'，
    #   做没做成查不到"的事故。心跳失败绝不影响提炼结果。
    try:
        from services.shared import heartbeat
        heartbeat.stamp('b2.extract', 'ok' if new_count or not total else 'skipped',
                        counts={'cold_lines': total, 'new': new_count,
                                'already': len(existing_ids), 'dup_blocked': dup_inflight},
                        note=f'{date_str}')
    except Exception:                       # noqa: BLE001
        pass
    return new_count

def load_all_warm_entries() -> list:
    warm_dir = _get_warm_dir()
    entries = []
    if os.path.exists(warm_dir):
        for fname in sorted(os.listdir(warm_dir)):
            if fname.endswith(".jsonl"):
                date_str = fname.replace(".jsonl", "")
                try:
                    with open(os.path.join(warm_dir, fname), "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                e = json.loads(line.strip())
                                if isinstance(e, dict):
                                    if "source_date" not in e:
                                        e["source_date"] = date_str
                                    entries.append(e)
                            except: pass
                except: pass
    return entries

def update_warm_entry_call_count(entry_id: str, delta: int = 1, ref: str = ""):
    """把某条温记忆的 callCount 加 delta（会影响热度的"常用浮现"回路）。

    2026-09-25 两处加固：
      * **原子写** —— 原先读全文后直接 `open(path, "w")` 重写，写一半进程被杀
        就把当天温层截断（jsonl 一行一条记忆，截断即丢记忆，且没有任何报错）。
        现在走 `atomic_write_text()`（临时文件 + `os.replace()`）；
      * **跨进程锁** —— 与 `_append_warm_entry()` 的追加互斥。此前只有进程内的
        `threading.Lock`，挡不住另一个解释器实例（例如计划任务与 MCP 进程同时跑）。
    """
    warm_dir = _get_warm_dir()
    date_str = ref.split("#")[0] if ref else ""
    if not date_str:
        return
    filepath = os.path.join(warm_dir, f"{date_str}.jsonl")
    if not os.path.exists(filepath):
        return
    try:
        with file_lock(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            modified = False
            for i, line in enumerate(lines):
                try:
                    entry = json.loads(line.strip())
                    if entry.get("id") == entry_id:
                        entry["callCount"] = entry.get("callCount", 0) + delta
                        lines[i] = json.dumps(entry, ensure_ascii=False) + "\n"
                        modified = True
                        break
                except Exception:
                    pass
            if modified:
                atomic_write_text(filepath, "".join(lines))
    except Exception:
        pass

def filter_warm_by_relevance(question: str, candidates: list, llm) -> list:
    if not candidates:
        return []
    if len(candidates) <= 5:
        return candidates
    if not llm or not llm.small:
        return candidates[:5]

    limit = min(len(candidates), 6)
    lines = []
    for i, c in enumerate(candidates[:limit]):
        lines.append(f"{i}. [{c.get('task_type') or c.get('type','')}] {c.get('summary','')}")

    prompt = RELEVANCE_FILTER_PROMPT.format(
        question=question,
        entries="\n".join(lines)
    )

    _eprint(f"  [相关性过滤] {len(lines)} 条摘要...", end=" ", flush=True)
    try:
        result = llm.infer_small_json(prompt, task_type="relevance_filter") if llm else None
    except Exception as e:
        _eprint(f"失败: {e}")
        return candidates[:5]

    if not result:
        _eprint(f"无返回，取前5条")
        return candidates[:5]

    if isinstance(result, list):
        indices = []
        for x in result:
            try:
                if isinstance(x, (int, float)):
                    indices.append(int(x))
                elif isinstance(x, str) and x.strip().isdigit():
                    indices.append(int(x.strip()))
            except: pass
    elif isinstance(result, dict):
        indices = [int(v) for v in result.values() if isinstance(v, (int, float))]
    else:
        indices = []

    filtered = [candidates[i] for i in indices if 0 <= i < len(candidates)]
    _eprint(f"{len(filtered)} 条相关")
    if not filtered:
        return candidates[:3]
    return filtered[:5]
