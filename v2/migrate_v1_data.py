"""V1 → V2 冷/温记忆数据迁移

源目录：
  冷记忆: C:/Users/admin/Documents/trae_projects/admin/shufang-os/backend/data/cold_vault/
  温记忆: 同上 .../warm/

目标：services/B3_header_engine SQLite memories 表 + B7 audit_log

用法：python migrate_v1_data.py [--dry-run]
"""
import sys, os, json, argparse, uuid, hashlib, datetime, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COLD_DIR = r"C:\Users\admin\Documents\trae_projects\admin\shufang-os\backend\data\cold_vault"
WARM_DIR = r"C:\Users\admin\Documents\trae_projects\admin\shufang-os\backend\data\warm"


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"  [WARN] 读 {path} 失败: {e}")
    return rows


def _memory_id_from_date_entry(date_str: str, entry_id: str) -> str:
    """复用 B3 的 memory_id 生成逻辑"""
    return hashlib.md5(f"{date_str}#{entry_id}".encode()).hexdigest()[:12]


def _guess_task_type(summary: str, entities: list) -> str:
    """从 V1 温记忆的 type 字段或关键词推测 task_type"""
    text = summary or ""
    t = (entities or []) + [text]
    joined = " ".join(t).lower()
    if any(k in joined for k in ["决策", "选择", "决定", "方案"]): return "决策"
    if any(k in joined for k in ["研究", "论文", "技术", "调研", "分析"]): return "研究"
    if any(k in joined for k in ["验证", "测试", "对比", "性能", "效果"]): return "验证"
    if any(k in joined for k in ["规划", "计划", "设计", "架构", "路线"]): return "规划"
    if any(k in joined for k in ["闲聊", "聊天", "讨论"]): return "闲聊"
    if any(k in joined for k in ["帮助", "求助", "怎么办", "指导"]): return "求助"
    return "其他"


def migrate_warm(dry_run: bool = False) -> int:
    """迁移温记忆（已有 summary/type/entities，直接写 SQLite）"""
    from services.B3_header_engine import bulk_insert, init_db, count_total
    from services.B7_audit import AuditLogger

    init_db()
    audit = AuditLogger()

    if not os.path.isdir(WARM_DIR):
        print(f"[WARN] 温记忆目录不存在: {WARM_DIR}")
        return 0

    all_mems = []
    for fname in sorted(os.listdir(WARM_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        date_str = fname.replace(".jsonl", "")
        tb = date_str[:7]  # YYYY-MM
        path = os.path.join(WARM_DIR, fname)
        rows = _load_jsonl(path)
        for r in rows:
            # —— 坑 A 修复：从 ref 拆冷库原始 id ——
            # 温层条目格式: {id: 哈希, ref: "2026-04-27#202604270001"}
            # 不能用 r["id"]（已经是哈希），必须用 ref 里的冷库原始 id
            ref = r.get("ref") or ""
            cold_entry_id = ""
            if "#" in ref:
                cold_entry_id = ref.split("#", 1)[1].strip()
            if not cold_entry_id:
                # ref 缺失，退而用 r["id"]（会有双重哈希）
                cold_entry_id = str(r.get("id") or uuid.uuid4().hex[:12]).strip()
            mid = _memory_id_from_date_entry(date_str, cold_entry_id)

            summary = r.get("summary", "") or r.get("content", "") or ""
            if not summary.strip():
                continue

            entities = r.get("entities") or []
            topics = r.get("topics") or []
            core_entities = list(set(entities + topics))

            task_type = _guess_task_type(summary, core_entities)
            heat = r.get("heat", 0.0)
            count = r.get("callCount", 0) or r.get("call_count", 0)

            layer = "hot" if heat > 15 else ("warm" if heat > 5 else "cold")

            content_path = os.path.join(COLD_DIR, f"{date_str}.jsonl")

            all_mems.append({
                "memory_id": mid,
                "layer": layer,
                "content_path": content_path,
                "summary": summary[:500],
                "task_type": task_type,
                "session_id": r.get("sessionId", ""),
                "time_bucket": tb,
                "core_entities": core_entities[:8],
                "emotional_tone": "",
                "source_weight": 1.0,
                "heat": heat or 0.0,
                "count": count or 0,
                "confidence": 1.0,
                "created_at": r.get("created_at", ""),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

    print(f"[温记忆] 发现 {len(all_mems)} 条待迁移")

    if dry_run:
        for m in all_mems[:3]:
            print(f"  DRY-RUN: {m['memory_id']} [{m['task_type']}] heat={m['heat']} count={m['count']} | {m['summary'][:50]}")
        return len(all_mems)

    # 批量写入（用 INSERT OR REPLACE 跳过已存在）
    n = bulk_insert(all_mems)
    audit.log_write({"source": "v1_migration_warm", "count": n}, [m["memory_id"] for m in all_mems])
    print(f"[温记忆] 已迁移 {n} 条，总库存 {count_total()}")
    return n


def migrate_cold(dry_run: bool = False) -> int:
    """迁移冷记忆（对话落盘，提炼 summary + entities 再写 SQLite）"""
    from services.B3_header_engine import bulk_insert, init_db, count_total
    from services.B7_audit import AuditLogger

    init_db()
    audit = AuditLogger()

    if not os.path.isdir(COLD_DIR):
        print(f"[WARN] 冷记忆目录不存在: {COLD_DIR}")
        return 0

    all_mems = []
    for fname in sorted(os.listdir(COLD_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        date_str = fname.replace(".jsonl", "")
        tb = date_str[:7]
        path = os.path.join(COLD_DIR, fname)
        rows = _load_jsonl(path)

        for r in rows:
            entry_id = str(r.get("id") or uuid.uuid4().hex[:12])
            mid = _memory_id_from_date_entry(date_str, entry_id)

            user_msg = (r.get("user") or "")[:300]
            assistant_msg = (r.get("assistant") or "")[:300]

            # 冷记忆没 summary，用 user_msg 前 100 字 + assistant 关键词
            summary = f"[冷] {user_msg[:80]}"

            # 简单关键词提取
            combined = user_msg + " " + assistant_msg
            ents = []
            for kw in ["书房", "记忆", "知识库", "ollama", "模型", "配置", "代码", "python",
                       "api", "向量", "embedding", "检索", "B4", "B3", "MAP", "审计"]:
                if kw.lower() in combined.lower():
                    ents.append(kw)

            # 提取中文名词（简化版：取引号内或关键词后）
            cn_nouns = re.findall(r'[\u4e00-\u9fa5]{2,6}', user_msg)
            ents.extend([n for n in cn_nouns if n not in ents][:5])

            all_mems.append({
                "memory_id": mid,
                "layer": "cold",
                "content_path": path,
                "summary": summary,
                "task_type": "闲聊",
                "session_id": "",
                "time_bucket": tb,
                "core_entities": list(set(ents))[:8],
                "emotional_tone": "",
                "source_weight": 1.0,
                "heat": 0.0,
                "count": 0,
                "confidence": 0.5,  # 冷记忆提炼较少，置信度低
                "created_at": r.get("time", ""),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

    print(f"[冷记忆] 发现 {len(all_mems)} 条待迁移")

    if dry_run:
        for m in all_mems[:3]:
            print(f"  DRY-RUN: {m['memory_id']} [{m['task_type']}] | {m['summary'][:50]}")
        return len(all_mems)

    n = bulk_insert(all_mems)
    audit.log_write({"source": "v1_migration_cold", "count": n}, [m["memory_id"] for m in all_mems])
    print(f"[冷记忆] 已迁移 {n} 条，总库存 {count_total()}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不写库，只打印预览")
    ap.add_argument("--skip-cold", action="store_true")
    ap.add_argument("--skip-warm", action="store_true")
    args = ap.parse_args()

    print("=" * 60)
    print("V1 → V2 冷/温记忆迁移")
    print(f"冷记忆源: {COLD_DIR}")
    print(f"温记忆源: {WARM_DIR}")
    print(f"Dry-run: {args.dry_run}")
    print("=" * 60)

    total = 0
    if not args.skip_warm:
        total += migrate_warm(dry_run=args.dry_run)
    if not args.skip_cold:
        total += migrate_cold(dry_run=args.dry_run)

    print(f"\n完成！总计 {total} 条迁移。")
    if not args.dry_run:
        from services.B5_heat_state import HeatStateMachine
        sm = HeatStateMachine()
        updated, hot_t, warm_t = sm.migrate_layers()
        print(f"B5 层迁移: {updated} 条更新 (hot={hot_t:.1f}, warm={warm_t:.1f})")


if __name__ == "__main__":
    main()
