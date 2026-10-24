"""mcp_tools.memory — 记忆域：冷记忆 CRUD、温记忆提炼、检索、统计、导出。

从 V1 routes.py 拆分而来，保留原 FastAPI endpoint 签名。
"""
import os, json, sys, threading, time as time_mod

from services.shared.atomic_io import atomic_write_text, file_lock


def _eprint(*args, **kwargs):
    """后台日志一律走 stderr。

    stdio MCP 模式下 stdout 是 JSON-RPC 通道，裸 `print` 会插进协议流里。
    提炼现在由 `start_cold_vault_poller()` 在 MCP 进程内自动触发，所以这条
    路径上的输出必须改道。
    """
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)
from fastapi import Request, UploadFile, File, Form
from datetime import datetime

# === 延迟导入，避免启动循环 ===
def _get():
    from services.shared.config import get_config
    from services.B1_cold_memory import (
        record_conversation, get_cold_status, read_cold_memory,
        export_as_markdown, export_all_cold_as_markdown,
        import_from_file, delete_cold_entry,
    )
    from services.B2_warm_extract import (
        extract_warm_from_date, load_all_warm_entries,
        update_warm_entry_call_count, filter_warm_by_relevance,
    )
    from services.shared.errors import log_exception
    return locals()


# ---------- 提炼状态（跨请求共享，沿用 V1 全局变量模式） ----------
_extract_running = False
_extract_progress = {"status": "idle", "current_date": "", "processed": 0, "total_dates": 0, "new_count": 0}
_auto_extract_timer = None


def _unload_small_model_after_extract():
    """提炼批次结束就把 4B 的显存还回去（★ 2026-10-24）。

    为什么落在这里，而不是检索链路
    ------------------------------
    * 4B（`llm.small`）**只**服务 B2 温提炼 —— 既不在检索链路上，也不在对话链路上。
      提炼是后台批处理，下次冷加载多花 ~8s **没有任何人感知**。
    * 它原先靠 router 的 `sleep-idle-seconds 300` 自己睡，可活跃期冷库轮询每 10 分钟
      就可能触发一次提炼，"醒着 = 批处理时长 + 5 分钟尾巴"，比真正干活的时间长得多。
      实测加载态约 3.9GB（2026-10-24：三模型全醒 7132MiB → 4B 睡下后 3217MiB）。
    * 走 router 原生 `POST /models/unload`（与面板「卸载模型」**同一个接口**）：
      HTTP 一次、不杀进程、不碰两个 bge。

    两个约束
    --------
    * **绝不抛**：卸载失败只是多占一会儿显存，不许影响提炼结果、更不许把提炼标成 error。
    * 调用点必须在 `_extract_running = False` **之前** —— 卸载期间标志位仍为 True，
      轮询会跳过，不会出现"刚卸完就被下一轮拉起来"。

    想关掉：`config.yaml` 的 `llm.small.unload_after_extract: false`。
    """
    try:
        import requests

        from services.shared.config import get_config
        small = ((get_config() or {}).get("llm", {}) or {}).get("small", {}) or {}
        if small.get("unload_after_extract", True) is False:
            return "disabled"
        base_url = str(small.get("base_url") or "http://127.0.0.1:8089/v1").rstrip("/")
        root = base_url[:-3] if base_url.endswith("/v1") else base_url
        model = small.get("model") or "Qwen3.5-4B-Q4_K_M"
        resp = requests.post(f"{root}/models/unload", json={"model": model}, timeout=10)
        if resp.status_code >= 300:
            _eprint(f"[提炼] 4B 卸载返回 HTTP {resp.status_code}（不影响结果）")
            return f"http-{resp.status_code}"
        _eprint(f"[提炼] 4B 已卸载，显存立刻归还")
        return "unloaded"
    except Exception as e:
        _eprint(f"[提炼] 4B 卸载失败（不影响结果）: {e}")
        return "error"


def _run_extract_background(app=None, llm=None):
    """后台全量提炼扫描。

    Args:
        app: FastAPI app 实例，用于从 app.state._llm_state 复用 LLM（避免重复加载 Ollama）
        llm: 直接传入 LLM 实例，优先级最高
    """
    global _extract_running, _extract_progress
    g = _get()

    # 复用 LLM 实例，避免重复加载 Ollama 模型导致显存浪费
    if llm is None and app is not None:
        try:
            llm = app.state._llm_state.get("llm") if hasattr(app.state, "_llm_state") else None
        except Exception:
            llm = None
    if llm is None:
        # 最后 fallback：临时新建（仅 MCP 独立调用路径）
        from services.shared.llm_router import create_llm_backend
        llm = create_llm_backend(g["get_config"]())

    _extract_running = True
    start_ts = time_mod.time()
    _extract_progress = {"status": "running", "current_date": "", "processed": 0,
                         "total_dates": 0, "new_count": 0,
                         "start_time": start_ts, "elapsed_seconds": 0}
    try:
        cold = g["get_cold_status"]()
        _extract_progress["total_dates"] = cold["total_dates"]
        for i, d in enumerate(cold["dates"]):
            date_str = d["date"]
            _extract_progress["current_date"] = date_str
            _extract_progress["processed"] = i + 1
            try:
                count = g["extract_warm_from_date"](date_str, llm)
                _extract_progress["new_count"] += count
            except Exception as e:
                _eprint(f"[提炼] [FAIL] {date_str}: {e}")
                g["log_exception"]("extract.per_date", e)
        elapsed = time_mod.time() - start_ts
        _extract_progress["elapsed_seconds"] = round(elapsed, 1)

        # 提炼完成后立刻索引进 B3 —— 这一步以前**完全缺失**，导致温层文件里的
        # 新条目永远进不了 B3，检索也就永远查不到新对话（实测：B2 抽出 26 条后
        # B3 仍是迁移时的 771 条）。warm_indexer 幂等，可重复调用。
        try:
            from services.B3_header_engine.warm_indexer import index_warm
            idx = index_warm()
            _extract_progress["indexed"] = idx.get("new_rows", 0)
            _extract_progress["b3_total"] = idx.get("after")
            _eprint(f"[提炼] B3 索引：新增 {idx.get('new_rows')} 行，总库存 {idx.get('after')}")
        except Exception as e:
            _extract_progress["indexed"] = None
            _eprint(f"[提炼] [WARN] B2→B3 索引失败（温层已写入，可手动重跑 warm_indexer）: {e}")
            g["log_exception"]("extract.index_warm", e)

        # 旧 FAISS 索引（v1 遗留路径，与 B3 无关）；保留兼容
        try:
            from services.B3_header_engine.memory_indexer_legacy import reload_index
            reload_index()
        except Exception:
            pass
        _extract_progress["status"] = "done"
    except Exception as e:
        _extract_progress["status"] = "error"
        _extract_progress["elapsed_seconds"] = round(time_mod.time() - start_ts, 1)
        g["log_exception"]("extract.fatal", e)
    finally:
        # ★ 2026-10-24：批次结束立刻还显存（见 `_unload_small_model_after_extract`）。
        #   放在清 `_extract_running` 之前：卸载期间标志位仍为 True，轮询会跳过。
        _extract_progress["model_unload"] = _unload_small_model_after_extract()
        _extract_running = False


def _trigger_auto_extract(app=None):
    """30s 防抖自动提炼触发"""
    global _auto_extract_timer
    g = _get()
    cfg = g["get_config"]()
    if not cfg.get("extract_schedule", {}).get("enabled", True):
        return
    if _auto_extract_timer:
        _auto_extract_timer.cancel()
    def _do():
        global _extract_running
        if _extract_running:
            return
        import threading
        t = threading.Thread(target=_run_extract_background, args=(app,), daemon=True)
        t.start()
    _auto_extract_timer = threading.Timer(30.0, _do)
    _auto_extract_timer.daemon = True
    _auto_extract_timer.start()


# ---------- 注册函数 ----------
def register(app):
    """把记忆域的所有 endpoint 注册到 FastAPI app"""

    @app.get("/cold/status")
    async def cold_status():
        return _get()["get_cold_status"]()

    @app.get("/cold/read/{date}")
    async def cold_read(date: str):
        entries = _get()["read_cold_memory"](date, max_entries=100)
        return {"date": date, "count": len(entries), "entries": entries}

    @app.get("/cold/export/{date}")
    async def cold_export(date: str):
        if date == "all":
            md = _get()["export_all_cold_as_markdown"]()
            return {"format": "markdown", "date": "all", "content": md}
        return {"format": "markdown", "date": date,
                "content": _get()["export_as_markdown"](date)}

    @app.post("/cold/delete/{date_str}/{entry_id}")
    async def cold_delete(date_str: str, entry_id: str):
        result = _get()["delete_cold_entry"](date_str, entry_id)
        if result["status"] == "ok":
            try:
                from services.B3_header_engine.memory_indexer_legacy import reload_index
                reload_index()
            except Exception:
                pass
        return result

    @app.post("/cold/import")
    async def cold_import(request: Request):
        g = _get()
        form = await request.form()
        file = form.get("file")
        target_date = form.get("date", None)
        if file is None:
            return {"status": "error", "message": "未提供文件"}
        content = await file.read()
        result = g["import_from_file"](content, file.filename, target_date)
        if result.get("status") == "ok":
            try:
                from services.shared.llm_router import create_llm_backend
                cfg = g["get_config"]()
                llm = create_llm_backend(cfg)
                date_str = result.get("date") or datetime.now().strftime("%Y-%m-%d")
                new_count = g["extract_warm_from_date"](date_str, llm)
                result["extracted"] = new_count
                from services.B3_header_engine.memory_indexer_legacy import reload_index
                reload_index()
            except Exception as e:
                result["extract_error"] = str(e)[:100]
        return result

    # ---- 温记忆 ----
    @app.get("/memory/stats")
    async def memory_stats():
        g = _get()
        entries = g["load_all_warm_entries"]()
        by_type = {}
        drafted = 0
        for e in entries:
            t = e.get("type", "其他")
            by_type[t] = by_type.get(t, 0) + 1
            if e.get("drafted"):
                drafted += 1
        return {"total": len(entries), "drafted": drafted,
                "pending": len(entries) - drafted, "by_type": by_type}

    @app.get("/memory/hot")
    async def memory_hot():
        entries = sorted(_get()["load_all_warm_entries"](),
                         key=lambda e: e.get("callCount", 0), reverse=True)
        return {"hot": [{"id": e["id"], "type": e.get("type", ""),
                          "summary": e.get("summary", ""),
                          "call_count": e.get("callCount", 0),
                          "drafted": e.get("drafted", False)}
                         for e in entries[:20]]}

    @app.post("/memory/search")
    async def memory_search(request: Request):
        """记忆检索（V1 前端兼容端点，走 B4 新管道）。

        ⚠️ 2026-09-25 起 `retrieve()` 的升温**只加 heat、不动 count**：
        返回的这几条只算「被翻出来过」。`count`（被采用次数）改由调用方
        显式上报产生（B5 `apply_action_batch` / MCP `memory_feedback`）——
        因为检索之后还有相关性过滤、用户确认过滤、preview 预览等关卡，
        「被捞出来」不等于「被用上」。见 B5 模块开头「heat 与 count 的分工」。
        """
        body = await request.json()
        query = body.get("query", "")
        top_k = body.get("top_k", 10)
        g = _get()
        try:
            # 切到 B4 新检索管道（SQLite + bge-m3 向量精排）
            from services.B4_dual_gate_retrieval import retrieve
            from services.B7_audit import AuditLogger
            audit = AuditLogger()

            result = retrieve(query_text=query, max_items=top_k, final_topk=top_k)
            results = result["items"]

            # 审计落盘（B7）
            audit.log_search(
                {"query": query, "elapsed_ms": result.get("elapsed_ms")},
                [r["memory_id"] for r in results],
            )

            # 附带原文（如果有 content_path）
            for r in results:
                cp = r.get("content_path")
                if cp and os.path.exists(cp):
                    try:
                        with open(cp, "r", encoding="utf-8") as f:
                            r["cold_content"] = f.read()[:500]
                    except Exception:
                        pass

            return {
                "query": query,
                "count": len(results),
                "results": results,
                "elapsed_ms": result.get("elapsed_ms"),
                "truncated": result.get("truncated"),
                "pipeline": result.get("pipeline"),
            }
        except Exception as e:
            g["log_exception"]("memory.search_ep", e)
            return {"query": query, "count": 0, "results": [], "error": str(e)[:200]}

    @app.post("/memory/extract")
    async def memory_extract():
        global _extract_running
        if _extract_running:
            return {"status": "running", "message": "提炼任务正在进行中"}
        t = threading.Thread(target=_run_extract_background, args=(app,), daemon=True)
        t.start()
        return {"status": "started", "message": "提炼任务已启动"}

    @app.get("/memory/extract/status")
    async def memory_extract_status():
        return dict(_extract_progress)

    @app.delete("/memory/entry/{entry_id}")
    async def memory_delete_entry(entry_id: str):
        """从温记忆 JSONL 中删除指定条目并重建索引"""
        from services.B2_warm_extract.warm_extractor import _get_warm_dir
        g = _get()
        warm_dir = _get_warm_dir()
        deleted = False
        if os.path.exists(warm_dir):
            for fname in os.listdir(warm_dir):
                if not fname.endswith(".jsonl"):
                    continue
                fp = os.path.join(warm_dir, fname)
                try:
                    # 删除也是「读全文 → 整文件重写」，和 `update_warm_entry_call_count`
                    # 是同一类风险：得和追加、和 callCount 重写互斥，且替换要原子，
                    # 否则半路被杀就把当天温层截断（2026-09-25）。
                    with file_lock(fp):
                        with open(fp, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        new_lines = []
                        for line in lines:
                            try:
                                entry = json.loads(line.strip())
                                if entry.get("id") == entry_id:
                                    deleted = True
                                    continue
                            except Exception:
                                pass
                            new_lines.append(line)
                        if deleted:
                            atomic_write_text(fp, "".join(new_lines))
                            break
                except Exception:
                    pass
        if deleted:
            from services.B3_header_engine.memory_indexer_legacy import reload_index
            n = reload_index()
            return {"status": "ok", "message": f"已删除条目 {entry_id}，重建索引 {n} 条"}
        return {"status": "not_found", "message": f"未找到条目 {entry_id}"}

    @app.get("/memory/debug")
    async def memory_debug():
        from services.B3_header_engine.memory_indexer_legacy import get_index_info
        idx = get_index_info()
        entries = _get()["load_all_warm_entries"]()
        return {
            "index": idx,
            "warm_count": len(entries),
            "sample_entries": [
                {k: e.get(k) for k in ["id", "type", "summary", "topics", "entities", "relations", "intent", "temporal"]}
                for e in entries[:3]
            ],
            "latest_date": sorted({e.get("source_date", "") for e in entries}, reverse=True)[:5] if entries else [],
        }

    @app.get("/memory/export")
    async def memory_export():
        entries = _get()["load_all_warm_entries"]()
        return {"format": "json", "count": len(entries), "entries": entries}

    @app.get("/memory/export-all")
    async def export_all():
        g = _get()
        cold_md = g["export_all_cold_as_markdown"]()
        warm_entries = g["load_all_warm_entries"]()
        return {
            "cold": {"format": "markdown", "content": cold_md},
            "warm": {"format": "json", "count": len(warm_entries), "entries": warm_entries},
            "exported_at": datetime.now().isoformat(),
        }

    # 注入到 chat 域的辅助函数（方便 routes 拆分后仍可用）
    # 用闭包自动带上 app，避免 chat 域重复创建 LLM
    app.state._trigger_auto_extract = lambda: _trigger_auto_extract(app)
    app.state._search_memories = None  # chat 域会填充
