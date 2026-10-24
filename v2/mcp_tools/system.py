"""mcp_tools.system — 系统域：配置 CRUD、模型健康检查、错误日志、LLM 连接测试。

从 V1 routes.py 拆分而来。
"""
import os, sys, time as time_mod, requests, subprocess, atexit, threading, schedule, uuid
from fastapi import Request


# ============================================================================
# 冷库轮询 —— stdio 模式下 B2 提炼的唯一自动触发器（2026-09-22 新增）
# ============================================================================
#
# 为什么需要它
# ------------
# 3 小时一次的提炼调度器挂在 FastAPI 的 ``@app.on_event("startup")`` 上，而 dsh
# 用的是 ``--mcp-stdio``（``create_mcp_server()`` 根本不建 FastAPI app），那条钩子
# 永远不会跑；冷记忆又是 dsh 的 cold-memory-watcher 插件**直接用 Node 写文件**的，
# 不走 MCP ``memory_write``，所以「写入后 30 秒防抖提炼」也不会响。
# 净结果：冷库一直在长，却没人把它提炼成温记忆，更不会进 B3 索引。
#
# 为什么轮询是安全的
# ----------------
# * 轮询只读本地 jsonl 数行数（``get_cold_status()``），几乎不耗资源；
# * 只有冷库条数**真的变化**时才触发提炼，不是每个 tick 都调模型；
# * 提炼逐条幂等（``extract_warm_from_date`` 用已存在的 warm id 去重，已提炼的直接
#   跳过），B3 索引也是幂等的（``warm_indexer``）—— 所以重复触发不会重复调小模型。
#
# 配置：``extract_schedule.poller_interval_minutes``（默认 10 分钟）。
# 只由 ``mcp_server.main()`` 的 stdio 分支启动：FastAPI 模式已经有 3 小时调度器。

_COLD_POLLER_STATE = {
    "started": False,
    "interval_seconds": None,
    "last_total": -1,
    "runs": 0,
    "last_error": None,
}


def cold_poller_status() -> dict:
    """轮询器当前状态，供排障查看。"""
    return dict(_COLD_POLLER_STATE)


def start_cold_vault_poller(interval_seconds: float = None) -> bool:
    """启动冷库轮询线程。已经启动过就返回 False（幂等）。"""
    if _COLD_POLLER_STATE["started"]:
        return False

    if interval_seconds is None:
        minutes = 10.0
        try:
            from services.shared.config import get_config
            sched = (get_config() or {}).get("extract_schedule", {}) or {}
            minutes = float(sched.get("poller_interval_minutes", 10))
        except Exception:
            pass
        interval_seconds = max(60.0, minutes * 60.0)

    _COLD_POLLER_STATE["started"] = True
    _COLD_POLLER_STATE["interval_seconds"] = interval_seconds

    def _cold_total():
        try:
            from services.B1_cold_memory import get_cold_status
            return int((get_cold_status() or {}).get("total_entries", 0))
        except Exception as e:
            _COLD_POLLER_STATE["last_error"] = f"cold_status: {e}"
            return None

    def _loop():
        from mcp_tools.memory import _extract_running, _run_extract_background
        while True:
            try:
                total = _cold_total()
                if total is not None and total != _COLD_POLLER_STATE["last_total"]:
                    if _extract_running:
                        # 上一轮还没跑完：保留 last_total，下个 tick 再试
                        pass
                    else:
                        print(f"  [轮询] 冷库 {_COLD_POLLER_STATE['last_total']} → {total}，触发提炼", file=sys.stderr)
                        threading.Thread(target=_run_extract_background,
                                         args=(None,), daemon=True).start()
                        _COLD_POLLER_STATE["last_total"] = total
                        _COLD_POLLER_STATE["runs"] += 1
            except Exception as e:
                _COLD_POLLER_STATE["last_error"] = str(e)
            time_mod.sleep(interval_seconds)

    threading.Thread(target=_loop, daemon=True, name="cold-vault-poller").start()
    print(f"  [轮询] 冷库轮询已启动（每 {interval_seconds / 60:.0f} 分钟检查一次）", file=sys.stderr)
    return True


def register(app):
    """把系统域 endpoint 注册到 FastAPI app"""

    # ---------- LLM 初始化（沿用 V1 全局模式）----------
    _llm_state = {"llm": None, "lock": threading.Lock()}

    def _init_llm():
        from services.shared.config import get_config
        from services.shared.llm_router import create_llm_backend
        from services.shared.errors import log_exception
        try:
            with _llm_state["lock"]:
                cfg = get_config()
                _llm_state["llm"] = create_llm_backend(cfg)
                # ★ 2026-10-24：**不再预热 4B**（原 `_warmup_small_model()`）。
                #
                # 它每次桥启动都发一次 1-token 请求把 4B 拉进显存 —— 冷加载实测
                # 7~9.4s、加载态约 3.9GB（`dsh-llama-server/warm-window.log`：
                # `推理通过（9428ms）`）。但提炼往往几分钟后才发生，**或者根本不发生**
                # （没有新冷条目就不提炼）。router 本来就会在第一次真正需要时加载，
                # 预热纯粹是白占显存 + 白读一遍 2.4GB 的 gguf。
                #
                # 代价：首次提炼会多等一次冷加载（约 8s）。提炼是后台批处理，
                # 不在任何延迟链路上，这个代价不可感知。
        except Exception as e:
            log_exception("llm.init", e)
            _llm_state["llm"] = None

    def _release_ollama_models():
        """空实现：llama router 自己管理模型驻留（models.ini + router 的 LRU）。

        2026-09-22 统一模型服务后本地模型都由 router 托管，没有可以「外部卸载」
        的东西。保留函数名是为了不惊动 /llm/unload 与 atexit 的调用方。
        """
        return

    # ---------- 调度器 ----------
    _scheduler_running = False

    def _start_scheduler():
        nonlocal _scheduler_running
        if _scheduler_running:
            return
        _scheduler_running = True
        from services.shared.config import get_config
        cfg = get_config()
        schedule_cfg = cfg.get("extract_schedule", {})
        mode = schedule_cfg.get("mode", "interval")
        if mode == "cron":
            for t in schedule_cfg.get("cron", {}).get("times", ["08:00", "18:00", "22:00"]):
                schedule.every().day.at(t).do(_scheduled_extract)
        else:
            hours = schedule_cfg.get("interval", {}).get("hours", 4)
            schedule.every(hours).hours.do(_scheduled_extract)

        def _run_loop():
            while True:
                schedule.run_pending()
                time_mod.sleep(30)
        threading.Thread(target=_run_loop, daemon=True).start()

    def _scheduled_extract():
        cfg = __import__("services.shared.config", fromlist=["get_config"]).get_config()
        schedule_cfg = cfg.get("extract_schedule", {})
        if not schedule_cfg.get("enabled", True):
            return
        try:
            from mcp_tools.memory import _extract_running, _run_extract_background
            if _extract_running:
                return
            threading.Thread(target=_run_extract_background, args=(app,), daemon=True).start()
        except Exception as e:
            print(f"[调度提炼] 失败: {e}")

    # ---------- 启动钩子 ----------
    @app.on_event("startup")
    async def _startup():
        # 索引初始化（数据目录为空时 gracefully 跳过）
        try:
            from services.B3_header_engine.memory_indexer_legacy import ensure_index_ready
            ensure_index_ready()
            print("  记忆索引就绪")
        except Exception as e:
            print(f"  [启动] 索引初始化跳过（数据目录可能为空）: {e}")

        # LLM 初始化
        try:
            _init_llm()
        except Exception as e:
            print(f"  [启动] LLM 初始化失败: {e}")

        # 调度器启动
        try:
            _start_scheduler()
        except Exception as e:
            print(f"  [启动] 调度器启动失败: {e}")

        atexit.register(_release_ollama_models)

    @app.on_event("shutdown")
    async def _shutdown():
        _release_ollama_models()

    # ---------- Endpoints ----------
    @app.get("/system/status")
    async def system_status():
        from services.shared.config import get_config
        from services.B1_cold_memory import get_cold_status
        from services.B2_warm_extract import load_all_warm_entries
        from datetime import datetime
        g_cfg = get_config()
        cold = get_cold_status()
        entries = load_all_warm_entries()

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_cold = next((d["entries"] for d in cold.get("dates", [])
                           if d["date"] == today_str), 0)
        try:
            from services.B2_warm_extract.warm_extractor import _get_warm_dir
            today_warm_file = os.path.join(_get_warm_dir(), f"{today_str}.jsonl")
            today_warm = sum(1 for _ in open(today_warm_file, "r", encoding="utf-8")) \
                if os.path.exists(today_warm_file) else 0
        except Exception:
            today_warm = 0

        def _health(which):
            try:
                backend = _llm_state["llm"].small if which == "small" else _llm_state["llm"].large
                return backend.check_health()
            except Exception:
                return {"healthy": None, "message": "LLM 未初始化"}

        small_h = _health("small")
        large_h = _health("large")

        return {
            "cold": {"total_entries": cold.get("total_entries", 0),
                     "total_dates": cold.get("total_dates", 0)},
            "warm": {"total": len(entries),
                     "drafted": sum(1 for e in entries if e.get("drafted"))},
            "today": {"cold_entries": today_cold, "warm_entries": today_warm},
            "models": {
                "small": {"name": _llm_state["llm"].small.name if _llm_state["llm"] else "未初始化",
                          "healthy": small_h["healthy"], "message": small_h["message"]},
                "large": {"name": _llm_state["llm"].large.name if _llm_state["llm"] else "未初始化",
                          "healthy": large_h["healthy"], "message": large_h["message"]},
            },
        }

    @app.get("/system/config")
    async def system_config():
        from services.shared.config import get_config, get_personas
        cfg = get_config()
        pcfg = get_personas()
        return {
            "providers": cfg.get("providers", {}),
            "llm": {
                "local_engine": cfg.get("llm", {}).get("local_engine", "llama_server"),
                "small": {k: cfg.get("llm", {}).get("small", {}).get(k)
                          for k in ["backend", "model", "base_url", "model_path",
                                    "n_gpu_layers", "max_history_messages", "enable_reasoning"]},
                "large": {k: cfg.get("llm", {}).get("large", {}).get(k)
                          for k in ["provider", "model", "base_url", "api_key",
                                    "enable_reasoning", "reasoning_model", "max_history_messages"]},
            },
            "personas": pcfg,
            "extract_schedule": cfg.get("extract_schedule", {}),
            "paths": cfg.get("paths", {}),
        }

    @app.post("/system/config")
    async def system_config_update(request: Request):
        from services.shared.config import get_config, save_config, get_personas, save_personas
        body = await request.json()
        cfg = get_config()
        if "llm" in body:
            lb = body["llm"]
            if "local_engine" in lb:
                cfg.setdefault("llm", {})["local_engine"] = lb["local_engine"]
            if "small" in lb:
                sc = cfg.setdefault("llm", {}).setdefault("small", {})
                for f in ("model", "backend", "base_url", "model_path",
                          "n_gpu_layers", "max_history_messages", "enable_reasoning"):
                    if f in lb["small"]:
                        sc[f] = lb["small"][f]
            if "large" in lb:
                lc = cfg.setdefault("llm", {}).setdefault("large", {})
                for f in ("provider", "model", "base_url", "enable_reasoning",
                          "reasoning_model", "max_history_messages"):
                    if f in lb["large"]:
                        lc[f] = lb["large"][f]
                if ("api_key" in lb["large"] and lb["large"]["api_key"]
                        and "*" not in lb["large"]["api_key"]):
                    lc["api_key"] = lb["large"]["api_key"]
        if "personas" in body:
            pb = body["personas"]
            pc = get_personas()
            for f in ("memory_enhance", "default_persona"):
                if f in pb:
                    pc[f] = pb[f]
            if "presets" in pb and isinstance(pb["presets"], dict):
                pc.setdefault("presets", {}).update(pb["presets"])
            if "custom_personas" in pb:
                pc["custom_personas"] = pb["custom_personas"]
            save_personas(pc)
        if "extract_schedule" in body:
            cfg["extract_schedule"] = body["extract_schedule"]
        try:
            save_config(cfg)
        except Exception as e:
            return {"status": "error", "message": str(e)}
        try:
            _init_llm()
        except Exception:
            pass
        return {"status": "ok"}

    @app.post("/llm/models")
    async def llm_list_models(request: Request):
        body = await request.json()
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")
        if not base_url or not api_key:
            return {"models": [], "error": "请先配置接口地址和 API 密钥"}
        try:
            resp = requests.get(f"{base_url.rstrip('/')}/models",
                                headers={"Authorization": f"Bearer {api_key}"},
                                timeout=15)
            if resp.status_code == 200:
                raw = resp.json().get("data", [])
                models = sorted(set(m.get("id", "") for m in raw if m.get("id")))
                return {"models": models}
            return {"models": [], "error": f"API 返回 {resp.status_code}"}
        except requests.exceptions.Timeout:
            return {"models": [], "error": "请求超时"}
        except requests.exceptions.ConnectionError:
            return {"models": [], "error": "无法连接"}
        except Exception as e:
            return {"models": [], "error": str(e)[:100]}

    @app.post("/llm/test-small")
    async def llm_test_small(request: Request):
        body = await request.json()
        engine = body.get("engine", "llama_server")
        if engine == "llama_cpp":
            model_path = body.get("model_path", "")
            if not model_path:
                return {"status": "error", "message": "未指定模型文件路径"}
            if not os.path.exists(model_path):
                return {"status": "error", "message": f"模型文件不存在: {model_path}"}
            try:
                import llama_cpp
                return {"status": "ok", "message": "模型文件存在，llama-cpp-python 已安装"}
            except ImportError:
                return {"status": "error", "message": "pip install llama-cpp-python"}
        base_url = body.get("base_url", "http://127.0.0.1:8089/v1")
        model = body.get("model", "")
        try:
            resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
            if resp.status_code == 200:
                models = [m.get("id", "") for m in resp.json().get("data", [])]
                if model and model not in models:
                    return {"status": "warning",
                            "message": f"llama router 已连接，但没有模型 '{model}'", "models": models}
                return {"status": "ok",
                        "message": f"llama router 已连接，共 {len(models)} 个模型可用", "models": models}
            return {"status": "error", "message": f"llama router 返回 {resp.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"status": "error", "message": "无法连接 llama router（8089），请运行 DS工程启动器.bat"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.post("/llm/test-large")
    async def llm_test_large(request: Request):
        body = await request.json()
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")
        model = body.get("model", "deepseek-chat")
        if not api_key or not base_url:
            return {"status": "error", "message": "未填写 API Key 或接口地址"}
        try:
            resp = requests.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 5}, timeout=10)
            if resp.status_code == 200:
                return {"status": "ok", "message": f"连接成功，模型 {model} 可用"}
            elif resp.status_code == 401:
                return {"status": "error", "message": "API Key 无效（401）"}
            elif resp.status_code == 403:
                return {"status": "error", "message": "API Key 无权限（403）"}
            else:
                return {"status": "error",
                        "message": f"请求失败 HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.post("/llm/unload")
    async def llm_unload():
        _release_ollama_models()
        return {"status": "ok", "message": "llama router 自行管理模型驻留，无需释放"}

    # ---------- 错误日志 ----------
    @app.get("/system/error-logs")
    async def system_error_logs():
        from services.shared.errors import get_recent_errors
        entries = get_recent_errors(50)
        return {"count": len(entries), "errors": entries}

    @app.get("/system/error-logs/export")
    async def system_error_logs_export():
        from services.shared.errors import export_errors
        return {"format": "json", "content": export_errors()}

    @app.post("/system/error-logs/clear")
    async def system_error_logs_clear():
        from services.shared.errors import clear_errors
        count = clear_errors()
        return {"status": "ok", "cleared_files": count}

    @app.post("/system/error-logs/report")
    async def system_error_logs_report(request: Request):
        from services.shared.errors import log_error
        body = await request.json()
        log_error(body.get("module", "frontend"), body.get("message", ""), body.get("traceback", ""))
        return {"status": "ok"}

    # ---------- 把 llm_state 暴露给 chat 域 ----------
    app.state._llm_state = _llm_state
