"""C2 记忆服务入口（V2）。

三层架构：
  1. FastAPI REST（V1 兼容）— 承接旧 Electron 前端
  2. FastMCP stdio/HTTP — 承接 dsh 直接调用（`mcp__xilian__*` 工具）
  3. HTTP 桥（`libmem_bridge.py`）— 承接 dsh 原生插件 `dsh-libmem`

★ 2026-10-10：13 个工具的函数体已抽到 **`libmem_tools.py`（单一真源）**。
  本文件里的 `@mcp.tool()` 只剩**薄包装**，docstring（＝模型看到的工具描述）
  逐字保留。这样 MCP 入口与 HTTP 桥共用同一份实现，行为不会分叉。

  抽出来的动因：DSH 插件 `dsh-libmem` 要直接调这些工具，而闭包里的函数体
  外部拿不到 —— 要么抄一份（本仓明确反对，见 `memory_drilldown` 的历史注释），
  要么抽出来。选了后者。

启动方式：
  python -m services.C2_mcp_server.mcp_server              # 默认 FastAPI :8766
  python -m services.C2_mcp_server.mcp_server --mcp-stdio   # MCP stdio 模式（旧路径）
  python -m services.C2_mcp_server.mcp_server --mcp-http    # MCP HTTP :8767
  python -m services.C2_mcp_server.libmem_bridge            # LibMem HTTP 桥 :8768（新路径）

依赖：
  pip install fastapi uvicorn schedule pyyaml requests whoosh jieba
  pip install mcp  # FastMCP（可选，MCP 模式需要）
"""
import os, sys, argparse

# 把 v2/ 加入 path
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICES = os.path.dirname(_HERE)
_V2 = os.path.dirname(_SERVICES)
for p in (_V2, _SERVICES):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.C2_mcp_server import libmem_tools  # noqa: E402


def create_fastapi_app():
    """组装 FastAPI app，把 4 个域的 endpoint 全部挂上"""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="昔涟记忆服务", version="2.0.0-pre")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    # 注册 4 个域
    from mcp_tools.memory import register as reg_memory
    from mcp_tools.knowledge import register as reg_knowledge
    from mcp_tools.chat import register as reg_chat
    from mcp_tools.system import register as reg_system

    reg_system(app)   # system 先注册，提供 llm_state / startup hook
    reg_memory(app)
    reg_knowledge(app)
    reg_chat(app)     # chat 依赖 system 的 llm_state 和 memory 的 _trigger_auto_extract

    # 桥接：chat 域要用 memory 域的自动提炼触发函数
    try:
        from mcp_tools.memory import _trigger_auto_extract
        app.state._trigger_auto_extract = _trigger_auto_extract
    except Exception:
        pass

    return app


def create_mcp_server():
    """创建 FastMCP 实例，把核心能力暴露为 mcp__xilian__* 工具。

    对齐 V2 架构：memory_search 走 B4 双阶段门控（Phase1 SQLite WHERE → Phase2 bge-m3 精排），
    MAP v0.1 协议（MemoryRequest → request_handler → MemoryResponse）。

    实现体在 `libmem_tools.py`；这里的包装函数只提供**模型可见的描述**与签名。
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("[MCP] 未安装 mcp 包，请运行: pip install mcp", file=sys.stderr)
        print("[MCP] 退回 FastAPI 模式", file=sys.stderr)
        return None

    mcp = FastMCP("xilian-memory")

    # ---------- 记忆工具（B4 新管道）----------
    @mcp.tool()
    def memory_search(query: str, top_k: int = 5, depth: str = "L1") -> dict:
        """昔涟记忆检索（V2 · B4 双阶段门控）。

        Phase1 SQLite WHERE 过滤 → Phase2 bge-m3 余弦 + heat/count 精排 → 预算裁剪。
        默认返回 L1 摘要层；如需细节，用 depth=L2/L3 深挖。

        ⚠️ 返回的都只是**候选**：这次调用本身只说明「这些被翻出来了」，
        不代表它们被用上了。如果你真的把某条的 summary 用进了回答
        （作为参考、引用、或影响了判断），请调用 `memory_feedback`
        回报它的 memory_id —— 那是 count（被采用次数）唯一的来源。

        Args:
            query: 搜索 query（自然语言）
            top_k: 返回条数（默认 5）
            depth: L1=摘要 / L2=片段 / L3=原文（默认 L1）

        Returns: MAP v0.1 MemoryResponse dict
            （每条 header 里 `heat` = 被想起的热度，`count` = 被采用的次数）
        """
        return libmem_tools.memory_search(query, top_k=top_k, depth=depth)

    @mcp.tool()
    def memory_search_simple(query: str, top_k: int = 5) -> list:
        """简易检索（不带 MAP 协议信封，直接返回摘要列表）。

        适合 dsh 主 Agent 直接消费的极简场景。

        ⚠️ 它**故意绕过 B6 MAP validator**（直接调 B4 `retrieve()`）：
        返回类型是裸 list，没有地方承载 `needs_clarification` / `empty` /
        `degraded` 这三个字段，所以三条硬规则在这条路上**不生效**。
        需要协议保证（不猜指代 / 不编造 / 不越权）时请用 `memory_search`。

        ⚠️ 返回的是**候选**，不代表被采用。真的用进了回答的那几条，
        请调用 `memory_feedback` 回报 memory_id（count 的唯一来源）。
        """
        return libmem_tools.memory_search_simple(query, top_k=top_k)

    @mcp.tool()
    def memory_drilldown(memory_id: str, depth: str = "L2", max_tokens: int = 1500) -> dict:
        """深挖单条记忆（L1 → L2 片段 或 L3 原文）。

        对齐 MAP v0.1 的 memory_drilldown 包。
        校验、取原文、B7 审计都在 service 层（`B6.drilldown_handler`），
        这里只做参数转发 —— 免得将来多一条上层路径就多一处漏校验/漏审计。

        2026-09-22 修：原先这里自己 `json.load(content_path)`，而 content_path
        指向**整天**冷库 JSONL，必然抛 `Extra data: line 2 column 1` ——
        这个工具以前从来没成功返回过 L2/L3（响应里的 has_deeper=true 是假的）。
        现在按 `memory_id = md5(date#原始id)[:12]` 反查定位到那一行。
        """
        return libmem_tools.memory_drilldown(memory_id, depth=depth, max_tokens=max_tokens)

    @mcp.tool()
    def memory_feedback(memory_ids: list[str]) -> dict:
        """回报「这些记忆我真的用上了」—— count（被采用次数）唯一的来源。

        **什么时候该调**：你把 `memory_search` / `memory_search_simple` 返回的
        某条记忆的内容**实际用进了这一轮回答** —— 作为背景参考、作为依据、
        或它影响了你的判断。**没用到就不要调**：宁可少报，不要凑数。

        只回报真正被采用的那几条，不要图省事把整批搜索结果原样报回来 ——
        count 就是用来区分「翻出来过」（heat）和「真的用过」（count）的，
        原样全报等于把它退回成检索次数。

        Args:
            memory_ids: 被采用的记忆 id 列表（重复项自动去重）

        Returns: {
            "action": "adopted",
            "applied": 成功升温的条数,          # 每条 count +1、heat +5.0
            "missing": [不存在的 id ...],       # 会明确报出来，不静默吞掉
            "failed": 出错的条数,
            "items": [{memory_id, heat, count} ...],
        }

        这是有副作用的写操作（会改 heat/count 并留 B7 审计），
        调一次算一次采用，别在重试里重复调。
        """
        return libmem_tools.memory_feedback(memory_ids)

    @mcp.tool()
    def memory_write(user_message: str, assistant_message: str) -> str:
        """写入冷记忆 + 触发温提炼（B1 → B3 写入链路）。"""
        return libmem_tools.memory_write(user_message, assistant_message)

    @mcp.tool()
    def memory_extract() -> dict:
        """手动触发全量温提炼扫描。"""
        return libmem_tools.memory_extract()

    @mcp.tool()
    def memory_stats() -> dict:
        """记忆统计（B3 SQLite 主索引 + B4 向量缓存）。"""
        return libmem_tools.memory_stats()

    # ---------- 知识库工具 ----------
    @mcp.tool()
    def knowledge_search(query: str, top_k: int = 5, with_related: bool = False) -> list:
        """知识库全文检索（架构书 §5.10）。

        Args:
            query: 关键词 / 自然语言
            top_k: 返回条数
            with_related: 给前几条附上 `related_memories`
                —— §5.10 步骤 3 的「知识条目 ↔ 记忆条目双向关联」。
                走 B4 检索但 **apply_heat=False**（旁路关联不该给记忆升温）。

        ⚠️ 返回裸 list，「索引是空的」和「没有匹配」长得一样 ——
        分不清时先调 `knowledge_stats` 看 `docs_in_index`。
        """
        return libmem_tools.knowledge_search(query, top_k=top_k, with_related=with_related)

    @mcp.tool()
    def knowledge_reindex() -> dict:
        """重建知识库索引（新增/改动了知识库文件之后跑一次）。

        Returns: 完整报告 —— files / indexed / skipped（**每项带跳过原因**）/
            truncated / docs_in_index / readers（docx2txt、pymupdf 装没装）。

        为什么给完整报告而不是一个数字：原先只有 `count`，
        「PDF 因为缺 PyPDF2 被静默跳过」这类问题完全看不见（2026-09-23 实测踩到）。
        """
        return libmem_tools.knowledge_reindex()

    @mcp.tool()
    def knowledge_stats() -> dict:
        """知识库现状：目录里的文件数 / 索引里的文档数 / 各类解析器可用性。

        存在的理由：`knowledge_search` 返回裸 list，没法表达「索引是空的」，
        于是「文档上传了却搜不到」这种状态可以被瞒很久（实际瞒了几个月）。
        """
        return libmem_tools.knowledge_stats()

    @mcp.tool()
    def knowledge_read(path: str, max_chars: int = 3000) -> str:
        """读取知识库文件内容（支持 .txt/.md/.docx/.pdf 等）。"""
        return libmem_tools.knowledge_read(path, max_chars=max_chars)

    # ---------- 审计工具 ----------
    @mcp.tool()
    def audit_recent(limit: int = 20) -> list:
        """最近审计日志（B7）。"""
        return libmem_tools.audit_recent(limit)

    @mcp.tool()
    def audit_verify(memory_id: str) -> dict:
        """验证单条记忆完整性 hash（B7）。"""
        return libmem_tools.audit_verify(memory_id)

    return mcp


# ---------- CLI 入口 ----------
def _ensure_schema():
    """启动时把库结构对齐到 schema.sql（幂等）。

    为什么必须放在启动路径上：`schema.sql` 是表结构的唯一真源，但它此前只被
    两个一次性迁移脚本调用过 `init_db()` —— 于是「改了 schema.sql，库没变」
    会静默发生：09-23 加的两个复合索引，就是靠有人手工跑了一次迁移脚本才进库的。
    漂移不会被任何验证脚本抓到（那些脚本测行为，不测 schema 是否落地）。

    失败**不阻塞启动**：schema 对齐的意义是"让改动生效"，不是"能不能服务"；
    真失败时打 stderr 让人看见，而不是把整个 MCP 拉不起来。
    """
    try:
        from services.B3_header_engine import ensure_schema
        report = ensure_schema()
        if report["skipped_columns"]:
            print(f"[昔涟] ⚠️ schema 有列无法自动补：{report['skipped_columns']}", file=sys.stderr)
    except Exception as e:
        print(f"[昔涟] schema 对齐失败（不影响启动）: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="昔涟记忆服务")
    parser.add_argument("--mcp-stdio", action="store_true",
                        help="以 MCP stdio 模式启动（dsh 子进程方式）")
    parser.add_argument("--mcp-http", action="store_true",
                        help="以 MCP HTTP 模式启动")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766,
                        help="FastAPI 端口（默认 8766）")
    parser.add_argument("--mcp-port", type=int, default=8767,
                        help="MCP HTTP 端口（默认 8767）")
    args = parser.parse_args()

    # 三种启动模式都先对齐 schema（幂等，几十 ms）
    _ensure_schema()

    if args.mcp_stdio:
        print("[昔涟] MCP stdio 模式...", file=sys.stderr)
        # stdio 模式没有 FastAPI startup 钩子 —— B2 提炼只能靠这个轮询自动触发。
        # 启动失败不影响 MCP 本体，所以整段吞异常。
        try:
            from mcp_tools.system import start_cold_vault_poller
            start_cold_vault_poller()
        except Exception as e:
            print(f"[昔涟] 冷库轮询启动失败（不影响 MCP）: {e}", file=sys.stderr)
        mcp = create_mcp_server()
        if mcp:
            mcp.run(transport="stdio")
        else:
            print("[昔涟] FastMCP 不可用，退出", file=sys.stderr)

    elif args.mcp_http:
        print(f"[昔涟] MCP HTTP 模式 -> {args.host}:{args.mcp_port}", file=sys.stderr)
        mcp = create_mcp_server()
        if mcp:
            mcp.run(transport="sse", host=args.host, port=args.mcp_port)
        else:
            print("[昔涟] FastMCP 不可用，回退 FastAPI", file=sys.stderr)
            _run_fastapi(args.host, args.port)

    else:
        _run_fastapi(args.host, args.port)


def _run_fastapi(host, port):
    import uvicorn
    app = create_fastapi_app()
    print(f"[昔涟] FastAPI 服务 -> {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
