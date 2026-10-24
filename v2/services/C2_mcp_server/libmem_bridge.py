"""LibMem HTTP 桥 —— 给 DSH 插件 `dsh-libmem` 用的进程内接口面。

为什么需要它
------------
`mcp_server.py` 的 `--mcp-stdio` 是**管道**协议：dsh 起一个 Python 子进程，
父子之间用 stdin/stdout 传 JSON-RPC。这在 dsh 的**受限沙箱**下走不通 ——
沙箱禁止以管道方式 spawn 子进程（`EPERM`，只有 `inherit`/`ignore` 允许），
所以 `dsh-cold-memory-watcher/lib/vault.js` 才被迫把 B1 的落盘格式在 Node 里
重写了一遍。

HTTP 不受这条限制：插件用 `stdio: 'ignore'`（再加两个**文件描述符**收日志）
起本服务，然后走 127.0.0.1 的 HTTP 调工具。于是：

  * 工具面不必再抄一份 Python 实现（单一真源见 `libmem_tools.py`）；
  * 一个 Python 进程常驻，bge 相关缓存不会每次调用重建；
  * 插件能看到服务日志（文件描述符），不用再猜子进程为什么没起来。

为什么用标准库 `http.server` 而不是 FastAPI
-------------------------------------------
**实测**（2026-10-10，本机 Python 3.12.9）：这套环境里 fastapi 与 starlette
版本不匹配，`FastAPI(...)` 直接抛

    TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'

也就是说 `mcp_server.py` 的 FastAPI（:8766）模式**本来就起不来**。桥只要有
三个路由、不想要任何依赖，标准库足够，而且顺带躲开了这颗版本地雷。

接口面（**只有三个**，故意不铺开）
---------------------------------
    GET  /health          探活：版本 / pid / 解析出的数据路径 / 工具数 / poller 状态
    GET  /tools           工具名单（自检与文档用）
    POST /tool            {"name": "<工具名>", "args": {...}} → {"ok": true, "result": ...}

工具实现全部来自 `libmem_tools.py`，本文件只做转发与错误翻译。

启动
----
    python -m services.C2_mcp_server.libmem_bridge --host 127.0.0.1 --port 8768
    python -m services.C2_mcp_server.libmem_bridge --no-poller   # 并行验证期用
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 与 mcp_server.py 一致的 path 推导
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES = os.path.dirname(_HERE)
_V2 = os.path.dirname(_SERVICES)
for _p in (_V2, _SERVICES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.C2_mcp_server.libmem_tools import (  # noqa: E402
    SERVICE_VERSION,
    TOOLS,
    call_tool,
)

SERVICE_NAME = "libmem"

#: 进程启动时刻与 poller 状态，`/health` 报出去（诊断用）。
_STARTED_AT = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
_POLLER_STATE = {"state": "unknown", "detail": ""}

#: 工具调用的互斥量。
#:
#: 为什么要有：B3 的 SQLite 写（heat/count、审计）与向量缓存不是为多写并发的
#: 场景写的，而 HTTP 桥天然会并发（模型一轮里并发调多个工具）。这里把工具调用
#: **串行化** —— 检索 ~500ms 的量级，串行的代价远小于一处分叉写入的排查成本。
_TOOL_LOCK = threading.Lock()


def _log(message: str) -> None:
    """所有诊断走 stderr：stdout 留给启动横幅。"""
    sys.stderr.write(f"[libmem-bridge] {message}\n")
    sys.stderr.flush()


# 日志编码钉死 UTF-8。
#
# 为什么必须显式钉：Windows 上 Python 的 stderr 在被重定向到文件时用的是
# **本地编码**（本机 cp936）。插件把 stdout/stderr 都指向同一个日志文件，
# 混着 cp936 的中文会让日志在 UTF-8 阅读器里变成乱码 —— 而这份日志正是
# 「服务为什么没起来」的唯一线索。PYTHONIOENCODING 也设了，双保险。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 不是 TextIOWrapper（被别的宿主接管）时就算了
        pass


def _resolved_paths() -> dict:
    """把生效的数据路径报出来。

    存在的理由和 `knowledge_stats` 一样：路径解析错了**不会报错**，只会
    「什么都搜不到」。冷记忆监听那边踩过一整类这种坑（vault 解析成 null
    却照常运行），所以让 /health 直接把生效值打出来。
    """
    try:
        from services.shared.config import loader
        base = loader.get_base_dir()
        return {
            "baseDir": base,
            "configPath": os.path.join(base, "config.yaml"),
            "dataRoot": loader.get_data_path("data_root"),
            "coldVault": loader.get_data_path("cold_vault"),
            "warmDir": loader.get_data_path("warm_dir"),
            "indexDir": loader.get_data_path("index_dir"),
        }
    except Exception as exc:  # 路径解析失败不该让服务不可用，但必须被看见
        return {"error": f"{type(exc).__name__}: {exc}"}


def _health() -> dict:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "pid": os.getpid(),
        "python": sys.executable,
        "cwd": os.getcwd(),
        "startedAt": _STARTED_AT,
        "tools": sorted(TOOLS),
        "toolCount": len(TOOLS),
        "poller": dict(_POLLER_STATE),
        "paths": _resolved_paths(),
    }


def _tools() -> dict:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "tools": [
            {
                "name": name,
                "doc": (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "",
            }
            for name, fn in sorted(TOOLS.items())
        ],
    }


# ---------------------------------------------------------------------------
# 模型服务真探活（GET /health/models）
# ---------------------------------------------------------------------------
#
# ★ 2026-10-10 移植自 `LibMem-AML` 踩过的 B1 坑：
#   「/health 报 embed_reachable=True 而实际不可用 —— 只探了端口，没真调」。
#   一个 200 的健康检查掩盖了一场 0 分的灾难。
#
# 为什么这条在本插件里更要紧：`phase2_rank` 的 fail-open 护栏（同日移植）生效后，
# 嵌入服务挂掉**不再表现为「查不到」**，而是「查到了但全是噪声」—— 从工具的响应
# 上比空结果更难分辨。所以探活必须真调一次。
#
# 为什么单独开一个端点而不是塞进 /health：`/health` 是插件启动时的就绪探针
# （每 400ms 轮询一次），不能让它每次都去等两个模型服务的往返。诊断时才付这个钱。
_MODEL_PROBE_TTL = 30.0
_MODEL_PROBE = {"at": 0.0, "payload": None}


def _post_json(url: str, payload: dict, timeout: float = 2.0):
    """POST 一个 JSON 并解析响应；返回 (毫秒, body)。stdlib，不引依赖。"""
    import urllib.request
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    return int((time.perf_counter() - started) * 1000), body


def _probe_models() -> dict:
    """真调一次嵌入与重排，回报各自可用性与往返耗时（30 秒内复用上次结果）。"""
    now = time.time()
    if _MODEL_PROBE["payload"] is not None and now - _MODEL_PROBE["at"] < _MODEL_PROBE_TTL:
        cached = dict(_MODEL_PROBE["payload"])
        cached["cached"] = True
        return cached

    try:
        from services.shared.config import loader
        cfg = (loader.get_config() or {}).get("retrieval", {}) or {}
    except Exception:                       # noqa: BLE001 - 读不到配置就用默认地址
        cfg = {}

    embed_base = str(cfg.get("embed_llama_base") or "http://127.0.0.1:18099").rstrip("/")
    rerank_base = str(cfg.get("rerank_base") or "http://127.0.0.1:18098").rstrip("/")
    out = {"cached": False, "ttlSec": _MODEL_PROBE_TTL}

    # 嵌入：真算一条 1 个字符的向量（够证明"能算"，又不给服务添负担）
    try:
        ms, body = _post_json(f"{embed_base}/v1/embeddings",
                              {"model": str(cfg.get("embed_model") or "bge-m3"),
                               "input": ["探活"]})
        vec = ((body.get("data") or [{}])[0] or {}).get("embedding") or []
        out["embed"] = {"ok": bool(vec), "ms": ms, "dim": len(vec), "base": embed_base}
    except Exception as exc:                # noqa: BLE001
        out["embed"] = {"ok": False, "ms": None, "base": embed_base,
                        "error": f"{type(exc).__name__}: {exc}"}

    # 重排：真算一条 query/文档对的分数
    try:
        ms, body = _post_json(f"{rerank_base}/v1/rerank",
                              {"model": str(cfg.get("rerank_model") or "bge-reranker-v2-m3"),
                               "query": "探活", "documents": ["探活"]})
        results = body.get("results") or []
        out["rerank"] = {"ok": bool(results), "ms": ms, "base": rerank_base}
    except Exception as exc:                # noqa: BLE001
        out["rerank"] = {"ok": False, "ms": None, "base": rerank_base,
                         "error": f"{type(exc).__name__}: {exc}"}

    # 两边都挂 = 检索会退化成"全是噪声"（护栏兜住不返回空，但分数不可信）
    out["degraded"] = not (out["embed"]["ok"] and out["rerank"]["ok"])
    _MODEL_PROBE["at"] = now
    _MODEL_PROBE["payload"] = out
    return out


def _run_tool(name, args):
    """返回 `(http_status, payload)`。

    错误码分工是刻意的：未知工具 404、参数不对 400、工具自己炸了 500。
    混成一个码会让插件侧没法区分「我调错了」和「后端坏了」。
    """
    if not isinstance(name, str) or not name:
        return 400, {"ok": False, "error": "缺少 name", "errorType": "BadRequest"}
    if name not in TOOLS:
        return 404, {
            "ok": False,
            "error": f"未知工具 {name!r}；可用：{', '.join(sorted(TOOLS))}",
            "errorType": "UnknownTool",
        }

    started = time.perf_counter()
    try:
        with _TOOL_LOCK:
            result = call_tool(name, args)
    except TypeError as exc:
        _log(f"{name} 参数错误: {exc}")
        return 400, {"ok": False, "error": str(exc), "errorType": "TypeError"}
    except Exception as exc:
        _log(f"{name} 失败: {type(exc).__name__}: {exc}")
        _log(traceback.format_exc())
        return 500, {"ok": False, "error": str(exc), "errorType": type(exc).__name__}

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    _log(f"{name} ok {elapsed_ms}ms")
    return 200, {"ok": True, "result": result, "elapsedMs": elapsed_ms}


class _Handler(BaseHTTPRequestHandler):
    server_version = "libmem-bridge"
    protocol_version = "HTTP/1.1"

    # 默认日志会往 stderr 打每一行请求 —— 我们已经有自己的业务日志，关掉。
    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 调用方在读回响应前就撤了（超时/被取消）。不是故障，别往日志里塞 traceback。
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            self._send(200, _health())
        elif path == "/health/models":
            self._send(200, _probe_models())
        elif path == "/tools":
            self._send(200, _tools())
        else:
            self._send(404, {"ok": False, "error": f"未知路径 {path}",
                             "errorType": "NotFound"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/tool":
            self._send(404, {"ok": False, "error": f"未知路径 {path}",
                             "errorType": "NotFound"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except Exception as exc:
            self._send(400, {"ok": False, "error": f"请求体解析失败: {exc}",
                             "errorType": type(exc).__name__})
            return

        status, result = _run_tool(payload.get("name"), payload.get("args") or {})
        self._send(status, result)


class _Server(ThreadingHTTPServer):
    """线程池 HTTP 服务，只把**真**故障写进日志。

    为什么必须有这个子类：`protocol_version = "HTTP/1.1"` 意味着连接会被复用，而客户端
    （Node 的 fetch/undici 连接池 —— 也就是本插件自己的探活）会关掉空闲连接。服务端线程
    此时正阻塞在读下一行请求（`raw_requestline = self.rfile.readline(...)`），于是收到
    RST 并抛出 `ConnectionResetError`，`socketserver` 默认把它连同整段 traceback 打到
    stderr —— 实测一天攒了 7 处。

    这份日志是排障时的**唯一门面**（"服务为什么没起来""检索为什么失败"都从这儿看），
    堆满无害 traceback 会让真故障沉底。所以：连接类异常一律忽略，其余照常打印。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        _log(f"请求处理失败（{client_address}）: {type(exc).__name__}: {exc}")
        super().handle_error(request, client_address)


def _ensure_schema() -> None:
    """启动时把库结构对齐到 schema.sql（幂等）。失败不阻塞启动。"""
    try:
        from services.B3_header_engine import ensure_schema
        report = ensure_schema()
        if report.get("skipped_columns"):
            _log(f"⚠️ schema 有列无法自动补：{report['skipped_columns']}")
    except Exception as exc:
        _log(f"schema 对齐失败（不影响启动）: {exc}")


def _start_poller() -> None:
    """起冷库轮询（B1 → B2 温提炼）。

    ★ 这一步以前是 `--mcp-stdio` 模式独有的副作用。摘掉 MCP 之后如果忘了搬，
    冷库会照常增长而**温提炼永远不再触发** —— 而且不报错，只是搜不到新记忆。
    """
    try:
        from mcp_tools.system import start_cold_vault_poller
        start_cold_vault_poller()
        _POLLER_STATE.update(state="started", detail="cold vault poller 已启动")
        _log("冷库轮询已启动")
    except Exception as exc:
        _POLLER_STATE.update(state="failed", detail=f"{type(exc).__name__}: {exc}")
        _log(f"⚠️ 冷库轮询启动失败（不影响服务）: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LibMem HTTP 桥（DSH 插件 dsh-libmem 用）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--no-poller", action="store_true",
                        help="不起冷库轮询（并行验证期用，避免与另一个进程重复提炼）")
    args = parser.parse_args()

    _log(f"LibMem 桥启动中 -> {args.host}:{args.port} ({SERVICE_VERSION})")
    _ensure_schema()
    if args.no_poller:
        _POLLER_STATE.update(state="skipped", detail="--no-poller")
        _log("冷库轮询按参数跳过")
    else:
        _start_poller()

    server = _Server((args.host, args.port), _Handler)

    # 收到终止信号时干净退出：插件的 ctx.effect 会 kill 子进程，走的是
    # taskkill/terminate；没有这段的话端口要等 TIME_WAIT 之外还可能留孤儿。
    def _stop(signum, _frame):
        _log(f"收到信号 {signum}，退出")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    _log(f"就绪：{len(TOOLS)} 个工具；cwd={os.getcwd()}；pid={os.getpid()}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _log("已停止")


if __name__ == "__main__":
    main()
