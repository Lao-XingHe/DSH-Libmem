"""Phase 2 L1+L3 迁移（正确版）

核心修复：坑 A（双重哈希）+ 坑 B（342 组冷/温重复合并）+ 坑 C（V2 温层无原文）

做法（按顺序，每步独立验证）：
1. warm_indexer 修完后重新 index → V2 温层 39 行 upsert 进 B3（正确 id + content_path）
2. 读 V1 温层文件 + V2 温层文件，对每条算出：
   - 旧错误 memory_id = md5(date#entry["id"])[:12]  （双重哈希，B3 孤儿行用的）
   - 新正确 memory_id = md5(date#ref里的cold_id)[:12]  （B3 冷层行用的）
   → 建立 {旧id: {新id, heat, count, summary, content_path}} 映射
3. B3 里：对每个旧 id 行 heat/count 加到新 id 行 → 删旧 id 行
4. 新 id 行 upsert：summary 用温层版（L1 补强），content_path 用冷库路径，heat/count 合并值
5. 重分层

默认 dry-run，--apply 才写库；--force 跳过回滚确认。
"""
import sys, os, json, hashlib, sqlite3, datetime, argparse, shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

V1_WARM_DIR = r"<OLD_DATA_ROOT>\backend\data\warm"
V1_COLD_DIR = r"<OLD_DATA_ROOT>\backend\data\cold_vault"


def _md5_prefix(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def _build_warm_mappings() -> tuple[dict, int]:
    """读 V1 + V2 温层文件，算出 {旧错误id: {new_id, heat, count, summary, content_path}}"""
    from services.shared.config.loader import get_config
    cfg = get_config()
    v2_warm_dir = cfg.get("paths", {}).get("warm_dir", "")
    v2_cold_dir = cfg.get("paths", {}).get("cold_vault", "")

    mappings = {}  # old_bad_id -> {new_id, heat, count, summary, content_path, date_str}
    total_entries = 0

    # V1 温层（指向 V1 冷库）
    v1_count = 0
    if os.path.isdir(V1_WARM_DIR):
        for fname in os.listdir(V1_WARM_DIR):
            if not fname.endswith(".jsonl"):
                continue
            date_str = fname.replace(".jsonl", "")
            cp = os.path.join(V1_COLD_DIR, f"{date_str}.jsonl")
            for entry in _load_jsonl(os.path.join(V1_WARM_DIR, fname)):
                ref = entry.get("ref") or ""
                if "#" not in ref:
                    continue
                _, cold_id = ref.split("#", 1)
                old_id = _md5_prefix(f"{date_str}#{entry.get('id', '')}")
                new_id = _md5_prefix(f"{date_str}#{cold_id}")
                heat = entry.get("heat") or 0.0
                count = entry.get("callCount") or entry.get("call_count") or 0
                summary = (entry.get("summary") or "")[:500]
                mappings[old_id] = {
                    "new_id": new_id, "heat": float(heat), "count": int(count),
                    "summary": summary, "content_path": cp, "date_str": date_str,
                    "source": "V1",
                }
                v1_count += 1

    # V2 温层（指向 V2 冷库）
    v2_count = 0
    if v2_warm_dir and os.path.isdir(v2_warm_dir):
        for fname in os.listdir(v2_warm_dir):
            if not fname.endswith(".jsonl"):
                continue
            date_str = fname.replace(".jsonl", "")
            cp = os.path.join(v2_cold_dir, f"{date_str}.jsonl")
            for entry in _load_jsonl(os.path.join(v2_warm_dir, fname)):
                ref = entry.get("ref") or ""
                if "#" not in ref:
                    continue
                _, cold_id = ref.split("#", 1)
                old_id = _md5_prefix(f"{date_str}#{entry.get('id', '')}")
                new_id = _md5_prefix(f"{date_str}#{cold_id}")
                heat = entry.get("heat") or 0.0
                count = entry.get("callCount") or entry.get("call_count") or 0
                summary = (entry.get("summary") or "")[:500]
                mappings[old_id] = {
                    "new_id": new_id, "heat": float(heat), "count": int(count),
                    "summary": summary, "content_path": cp, "date_str": date_str,
                    "source": "V2",
                }
                v2_count += 1

    print(f"  V1 温层映射: {v1_count} 条")
    print(f"  V2 温层映射: {v2_count} 条")
    return mappings, v1_count + v2_count


def run(dry_run: bool = True):
    from services.B3_header_engine import init_db, get_conn, count_total
    from services.shared.config.loader import get_config

    cfg = get_config()
    idx_dir = cfg.get("paths", {}).get("index_dir", "./data/index")
    db_path = os.path.join(idx_dir, "header.db")

    print(f"=== Phase 2 L1+L3 迁移（正确版）[{ 'DRY-RUN' if dry_run else 'APPLY' }] ===\n")
    init_db()

    conn = get_conn()
    before = conn.execute("SELECT COUNT(*), COALESCE(SUM(heat),0), COALESCE(SUM(count),0) FROM memories").fetchone()
    before_rows, before_heat, before_count = before
    before_cp = conn.execute("SELECT COUNT(*) FROM memories WHERE content_path != '' AND content_path IS NOT NULL").fetchone()[0]
    print(f"迁移前: rows={before_rows}  heat={before_heat:.3f}  count={before_count}  有cp={before_cp}")

    # Step 0: 建映射
    print(f"\n--- Step 0: 从温层文件建 old→new memory_id 映射 ---")
    mappings, _ = _build_warm_mappings()
    print(f"  映射总数: {len(mappings)}")

    # Step 1: 重跑 warm_indexer（修完坑 A + content_path）
    print(f"\n--- Step 1: warm_indexer.index_warm()（修坑 A + 补 content_path）---")
    from services.B3_header_engine.warm_indexer import index_warm
    idx_result = index_warm(dry_run=dry_run, prune=False)
    print(f"  warm_entries={idx_result['warm_entries']} written={idx_result.get('written',0)}")

    # Step 2: 把孤儿行的 heat/count 加回新 id 行，然后删孤儿行
    print(f"\n--- Step 2: 合并孤儿行 heat/count → 新 id 行 ---")
    orphans = conn.execute(
        "SELECT memory_id, heat, count FROM memories WHERE (content_path IS NULL OR content_path = '')"
    ).fetchall()
    orphan_ids = [r["memory_id"] for r in orphans]
    print(f"  B3 孤儿行（无 content_path）: {len(orphans)}")

    merge_heat = 0.0
    merge_count = 0
    merged_rows = 0
    unmatched_orphans = []

    for r in orphans:
        old_id = r["memory_id"]
        info = mappings.get(old_id)
        if info:
            new_id = info["new_id"]
            heat_from_orphan = r["heat"] or 0
            count_from_orphan = r["count"] or 0
            merge_heat += heat_from_orphan
            merge_count += count_from_orphan
            merged_rows += 1

            # 把 heat/count 加到新 id 行，同时覆盖 summary/content_path（温层提炼版更精炼）
            target = conn.execute("SELECT heat, count, summary FROM memories WHERE memory_id=?", (new_id,)).fetchone()
            if target:
                new_heat = (target["heat"] or 0) + heat_from_orphan
                new_count = (target["count"] or 0) + count_from_orphan
                old_summary = target["summary"] or ""
                # 只有当温层 summary 明显优于冷层时才覆盖（冷层常带 [冷] 前缀）
                warm_summary = info["summary"]
                use_warm = (warm_summary and
                            (not old_summary or "[冷]" in old_summary or len(warm_summary) > len(old_summary) * 0.5))
                final_summary = warm_summary if use_warm else old_summary
                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET heat=?, count=?, summary=?, content_path=? WHERE memory_id=?",
                        (new_heat, new_count, final_summary, info["content_path"], new_id)
                    )
            else:
                # 新 id 行还不存在——用温层文件的信息插入
                if not dry_run:
                    # 先拿新 id 行的所有字段（可能冷层行有），或者从温层映射构造
                    # 检查有没有冷层行
                    existing = conn.execute(
                        "SELECT * FROM memories WHERE memory_id=?", (new_id,)
                    ).fetchone()
                    if existing:
                        # 冷层行已存在——加 heat/count，更新 summary/content_path
                        new_heat = (existing["heat"] or 0) + heat_from_orphan
                        new_count = (existing["count"] or 0) + count_from_orphan
                        conn.execute(
                            "UPDATE memories SET heat=?, count=?, summary=?, content_path=? WHERE memory_id=?",
                            (new_heat, new_count, info["summary"], info["content_path"], new_id)
                        )
                    else:
                        # 冷层行也不存在——构造新行
                        # 从 content_path 的日期推 source_date
                        date_str = info["date_str"]
                        conn.execute("""INSERT INTO memories
                            (memory_id, layer, content_path, summary, task_type, session_id,
                             time_bucket, core_entities, heat, count, confidence,
                             created_at, updated_at, data_sovereignty)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (new_id, "cold", info["content_path"], info["summary"],
                             info.get("task_type", "其他"), "",
                             date_str[:7], "[]", new_heat, new_count, 1.0,
                             datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             "user"))
        else:
            unmatched_orphans.append(old_id)

    print(f"  能映射并合并: {merged_rows}  加回 heat={merge_heat:.3f}  count={merge_count}")
    print(f"  找不到映射（孤儿里 id 不在温层文件）: {len(unmatched_orphans)}")

    if not dry_run and orphan_ids:
        # 备份
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak_path = f"{db_path}.bak-layers-{ts}"
        shutil.copy2(db_path, bak_path)
        bak_conn = sqlite3.connect(bak_path)
        bak_conn.execute("PRAGMA journal_mode = DELETE")
        bak_conn.close()
        print(f"\n  备份: {bak_path}")

        # 删孤儿行
        conn.executemany("DELETE FROM memories WHERE memory_id = ?", [(mid,) for mid in orphan_ids])
        conn.commit()
        print(f"  已删除 {len(orphan_ids)} 行孤儿")

    # Step 3: 重分层
    print(f"\n--- Step 3: 重分层（migrate_layers）---")
    if not dry_run:
        from services.B5_heat_state import HeatStateMachine
        sm = HeatStateMachine()
        updated, hot_t, warm_t = sm.migrate_layers()
        print(f"  重分层: {updated} 行更新")
    else:
        print(f"  DRY-RUN: 跳过重分层")

    # Step 4: 最终统计
    after = conn.execute("SELECT COUNT(*), COALESCE(SUM(heat),0), COALESCE(SUM(count),0) FROM memories").fetchone()
    after_rows, after_heat, after_count = after
    after_cp = conn.execute("SELECT COUNT(*) FROM memories WHERE content_path != '' AND content_path IS NOT NULL").fetchone()[0]
    from services.B3_header_engine import get_layer_distribution
    layers = get_layer_distribution() if not dry_run else {}

    print(f"\n=== 迁移后 ===")
    print(f"rows: {before_rows} → {after_rows}")
    print(f"heat: {before_heat:.3f} → {after_heat:.3f}")
    print(f"count: {before_count} → {after_count}")
    print(f"有 content_path: {before_cp} → {after_cp}")
    if layers:
        print(f"分层: {dict(layers)}")

    conn.close()

    if dry_run:
        print("\n⚠️ DRY-RUN: 没有写库。要真跑：加 --apply")
        print(f"   要加回的 heat={merge_heat:.3f} count={merge_count}")
    else:
        print(f"\n✅ 迁移完成！heat 保留: {before_heat:.3f} → {after_heat:.3f} {'✅' if abs(after_heat - before_heat) < 0.01 else '⚠️ 有丢失'}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2 L1+L3 迁移（正确版：合并孤儿 heat/count 后再删）")
    ap.add_argument("--apply", action="store_true", help="真跑（默认 dry-run）")
    args = ap.parse_args()
    run(dry_run=not args.apply)


if __name__ == "__main__":
    main()
