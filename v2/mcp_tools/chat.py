"""mcp_tools.chat — 对话域：/chat 主 endpoint、pipeline 状态机、关键词提取、人设生成。

从 V1 routes.py 拆分而来。chat endpoint 是 V1 的核心业务编排，跨 memory/knowledge/LLM 三域。
"""
import os, re, sys, uuid, time as time_mod, threading
from fastapi import Request


def _eprint(*args, **kwargs):
    """诊断横幅一律走 stderr。

    stdio MCP 模式下 stdout 是 JSON-RPC 通道，往 stdout 打字会插进协议流。
    （本模块原有用 `print` 打关键词日志的地方，新加的日志一律走这里。）
    """
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


# ---------- 常量 ----------
# 反问/拒答的**判定与措辞**都来自 B6 协议层的共用模块 —— 产品与评测 import 同一份，
# 不再各写一套（本仓踩过"评测绕过 chat.py、两边各说一套"的坑）。
# 详见 `services/B6_map_protocol/clarify.py` 的模块文档。
from services.B6_map_protocol.clarify import (          # noqa: E402
    CLARIFY_INSTRUCTION as _CLARIFY_INSTRUCTION, annotate as _clarify_annotate,
)

_DEFAULT_SYSTEM_PROMPT = f"""你是一个个人AI工作台的助手，拥有用户的长期记忆。在回答问题时，请结合提供的"相关记忆"来理解用户的背景和偏好。如果记忆中有相关内容，自然地引用它。

【硬规则 1 · 日期必须换算，不许复述相对词】
每条记忆最前面的方括号里是**该次对话的发生日期**，例如 `- [2023-05-08] [闲聊] …`。
记忆摘要里的时间往往是相对的（"昨天""上周""三年前"）。当用户问"什么时候""多久之前""哪一天"时，
**必须**用那条记忆的日期把它换算成绝对日期 —— 例如 `[2023-05-08]` 里的"昨天"就是 2023-05-07。
**禁止**直接回答相对词（"昨天""上周"），那等于没有回答时间。

【硬规则 2 · 不确定不等于拒答；说不清就反问】
{_CLARIFY_INSTRUCTION}"""

_KW_EXTRACT_PROMPT = """从以下用户消息中提取3-5个用于记忆搜索的关键词或短语。如果用户的问题涉及文档、资料、知识库中的文件内容，在关键词末尾添加 [KB] 标记。只返回结果，不要序号、不要解释、不要其他内容。

用户消息: {message}
关键词:"""

_PERSONA_GENERATE_PROMPT = """根据用户的描述，生成一段完整的角色人设提示词。要求：1. 描述该角色的身份、性格、语气风格 2. 给出具体的表达习惯和常用语 3. 说明回答问题的风格偏好 4. 控制在150-300字

用户描述: {description}

直接输出人设提示词，不要加"人设："等前缀，不要用代码块包裹。"""

_STOP_WORDS = {"我","你","他","她","它","们","的","了","是","在","和","与","或","这","那",
               "什么","怎么","为什么","如何","可以","一个","这个","那个","有些","一些","一下",
               "没有","不是","就是","只是","还是","但是","因为","所以","如果","虽然","而且",
               "已经","关于","对于","根据","通过","方面","时候","知道","觉得","认为",
               "需要","应该","能够","进行","起来","过来","过去","其中","之间","之后","之前",
               "以上","以下","以及","所有","一般","一定","一直","一种","一样","可能","开始",
               "继续","其他","哪","吗","呢","吧","啊","喔","哦"}

# ---------- Pipeline 状态机 ----------
_pipeline_status: dict[str, dict] = {}
_pipeline_lock = threading.Lock()


def _set_pipeline(rid, stage, **kwargs):
    with _pipeline_lock:
        _pipeline_status[rid] = {"stage": stage, "ts": time_mod.time(), **kwargs}


def _cleanup_old_pipelines():
    now = time_mod.time()
    with _pipeline_lock:
        stale = [k for k, v in _pipeline_status.items()
                 if v.get("done") and now - v.get("ts", 0) > 120]
        for k in stale:
            del _pipeline_status[k]


# ---------- 辅助函数 ----------
# 问句形态分类（2026-09-26 加）：决定**交付什么**，不是决定*答不答*。
#
# 背景：实测（云端模型、cat2/cat4 各 20 题，只换上下文）——
#   只交付 L1 中文摘要 → cat2 F1 0.605 / cat4 0.200
#   摘要 + 英文原文   → cat2 F1 0.817 / cat4 0.351   （**+0.211 / +0.151**）
# 原因是摘要是**有损**的，而且不只是"漏"，还会**改错**：
# 实测 `How long has Caroline had her current group of friends?` 标准答案 `4 years`，
# 摘要写成 `Five years` —— 再强的模型也没法从错的摘要里救回来。
#
# 那为什么不是一律给原文？因为原文的代价是上下文体积约 6 倍，
# 而推理题（cat3 那类"would/might/likely"）靠的是人物设定与关系，不需要逐字原文，
# 给摘要 + 实体就够，还能省下预算多给几条记忆。
#
# ---- v1.3.1「维度版」：分流逻辑已抽到**唯一一份**共享实现 ----------------
#
# 为什么删掉这里的 `_Q_INFER_PAT` + `classify_question`（2026-09-29）：
#   原来 `chat.py` 与本仓 `locomo-test/full_9b_eval.py` **各有一份逐字相同的**
#   `_INFER_PAT`。这是"产品与评测走两条路"的典型 —— 评测量到的不是产品跑的东西。
#   现在两边都 import `services.shared.dimensions`，判定只有一处。
#
# 维度定义在 `<BASE_DIR>/dimensions.yaml`（**数据**，改文件即可加维度，不用动代码）。
# 5 大项 / 9 子类：查找 A / 汇合 B / 无满足 C / 同一性 D / 操作 E。
# 依据：docs/roadmap/PROBE-V1.3-维度框架-通用版-20260929.md
from services.shared import dimensions as DIM


def classify_question(text) -> str:
    """旧接口，保留向后兼容：`'fact'`（事实题）或 `'inference'`（推理题）。

    ⚠️ **新代码请直接用 `DIM.classify(text)` 拿子类 id**（如 `'B1'`），
    它能表达 5 大项 / 9 子类，而这里只剩 2 类。
    """
    return DIM.classify_question(text)



def _build_system_prompt(personas, persona_key=None, persona_custom=None):
    from services.C3_persona import resolve_persona
    p = resolve_persona(personas, persona_key, persona_custom)
    return f"{p}\n\n{_DEFAULT_SYSTEM_PROMPT}" if p else _DEFAULT_SYSTEM_PROMPT


def _extract_keywords(message, llm):
    if not llm:
        return (message, False)
    try:
        result = llm.infer_small(_KW_EXTRACT_PROMPT.format(message=message),
                                 None, task_type="keyword_extract")
        if result:
            need_kb = "[KB]" in result.upper() or "[kb]" in result.lower()
            cleaned = re.sub(r'\[KB\]|\[kb\]', '', result, flags=re.IGNORECASE)
            cleaned = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9,,\s]', '', cleaned).strip().rstrip(',,')
            cleaned = re.sub(r'[,，]+', ' ', cleaned).strip()
            words = [w for w in cleaned.split() if len(w) >= 2 and w not in _STOP_WORDS]
            cleaned = ' '.join(words)
            return (cleaned if cleaned else message, need_kb)
    except Exception as e:
        print(f"  [关键词] 失败: {e}")
    return (message, False)


def _mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# ---------- 注册函数 ----------
def register(app):

    @app.post("/chat")
    async def chat(request: Request):
        body = await request.json()
        user_message = body.get("message", "")
        history = body.get("history", [])
        persona = body.get("persona", "")
        persona_custom = body.get("persona_custom", "")
        use_small_model = body.get("use_small_model", False)
        mode = body.get("mode", "generate")
        confirmed_memory_ids = body.get("confirmed_memory_ids", [])
        confirmed_kb_paths = body.get("confirmed_kb_paths", [])
        if not user_message:
            return {"reply": "", "error": "消息为空"}

        # 获取 system 域初始化好的 llm
        llm = app.state._llm_state["llm"]

        request_id = uuid.uuid4().hex[:8]
        _set_pipeline(request_id, "init")
        _cleanup_old_pipelines()

        debug = {
            "small_model": llm.small.name if llm else "未初始化",
            "large_model": llm.large.name if llm else "未初始化",
            "use_small_model": use_small_model,
            "memory_enhance": False,
            "kw_extract": None, "kw_extract_time_ms": 0,
            "memory_hits": 0, "cold_reads": 0,
            "kb_hits": 0,
            "api_status": None, "api_time_ms": 0, "api_error": None,
        }

        from services.shared.config import get_config, get_personas
        from services.B2_warm_extract import load_all_warm_entries, filter_warm_by_relevance, update_warm_entry_call_count
        from services.B1_cold_memory import record_conversation, read_cold_memory
        from services.C1_knowledge import search_knowledge_index, read_kb_file_content
        from services.shared.errors import log_exception

        cfg = get_config()
        personas = get_personas()
        memory_enhance = personas.get("memory_enhance", True)
        debug["memory_enhance"] = memory_enhance

        # 关键词提取
        search_query = user_message
        need_kb = False
        if memory_enhance and llm:
            t0 = time_mod.time()
            search_query, need_kb = _extract_keywords(user_message, llm)
            debug["kw_extract"] = search_query
            debug["kw_extract_time_ms"] = round((time_mod.time() - t0) * 1000)

        # 记忆搜索 — 切 MAP 协议（B6 request_handler → B4 检索管道）
        memory_context = []
        kb_results = []
        # ★ v1.3.1：维度判定必须在**检索之前**做 —— 因为交付条数（max_items）由维度决定。
        #   判据用 `user_message`（原始问句），不是 `search_query`（关键词）：
        #   维度看的是**问句形态**，抽完关键词形态就没了。
        dim_info = DIM.classify_detail(user_message)
        debug["dim"] = dim_info
        _eprint(f"  [维度] {dim_info['child']} {DIM.describe(dim_info['child'])}"
                f" max_items={dim_info['topk']}")
        if memory_enhance:
            _set_pipeline(request_id, "searching")
            try:
                from services.B6_map_protocol import MemoryRequest, request_handler
                map_req = MemoryRequest(
                    query=search_query,
                    depth="L1",
                    max_tokens=500,
                    max_items=dim_info["topk"],     # ★ 维度决定给几条
                    trace_id=request_id,
                )
                map_resp = request_handler(map_req)
                memory_context = []
                for item in map_resp.items:
                    memory_context.append({
                        "id": item.memory_id,
                        "summary": item.summary,
                        "type": item.header.get("task_type", ""),
                        "confidence": item.confidence,
                        "confidence_b6": item.confidence,
                        "task_type": item.header.get("task_type"),
                        "layer": item.header.get("layer"),
                        "core_entities": item.header.get("core_entities", []),
                        "heat": 0.0, "count": 0,  # B4 没填这俩，chat pipeline 不依赖
                    })
                debug["map_items"] = len(memory_context)
                debug["map_empty"] = map_resp.empty
                debug["map_needs_clarification"] = map_resp.needs_clarification
                debug["map_tokens_used"] = map_resp.tokens_used
            except Exception as e:
                log_exception("memory.search", e)
            _set_pipeline(request_id, "filtering", keywords=search_query,
                          hits=len(memory_context))
            memory_context = filter_warm_by_relevance(user_message, memory_context, llm)
            debug["memory_hits"] = len(memory_context)

            kb_results = search_knowledge_index(search_query, top_k=3)
            debug["kb_hits"] = len(kb_results)

        # Preview 模式
        if mode == "preview":
            _set_pipeline(request_id, "preview_ready")
            preview_memories = [{
                "id": m.get("id", ""), "type": m.get("type", ""),
                "summary": m.get("summary", ""),
                "topics": m.get("topics", []), "entities": m.get("entities", []),
                "relations": m.get("relations", []), "intent": m.get("intent", []),
                "temporal": m.get("temporal", []), "source_date": m.get("source_date", ""),
                "ref": m.get("ref", ""), "callCount": m.get("callCount", 0),
            } for m in memory_context[:10]]
            kb_preview = [{"name": k["name"], "path": k["path"],
                           "score": round(k.get("_score", 0), 1)}
                          for k in kb_results[:3]]
            return {
                "preview_id": request_id, "reply": "", "reasoning_content": "",
                "memory_context": preview_memories, "kb_preview": kb_preview,
                "keywords_used": search_query if memory_enhance else "",
                "debug": debug, "request_id": request_id,
            }

        # 确认过滤
        confirmed_set = set(confirmed_memory_ids)
        memory_context = [m for m in memory_context if m.get("id") in confirmed_set]

        # 组装上下文
        cold_reads = 0
        context_block = ""
        kb_context_block = ""
        if memory_context:
            # ★ v1.3.1：交付形态由**维度**决定，不再由 2 类的 fact/inference 决定。
            #   `delivery` 取值（见 dimensions.yaml）：
            #     raw     → 摘要 + 原文      （A 查找 / B1 汇总 / C / D：查准，事实在原文里）
            #     summary → 摘要 + 实体      （B2 探索式：V1.2 实测 +0.165 的那一档）
            #   `dedupe=True` 的维度（B1/D）会丢掉重复摘要 —— 汇总题的"条数"必须是**不同条目**，
            #   否则多给的 15 条被同一天的近似内容占满，等于没给。
            d_child = dim_info["child"]
            d_delivery = dim_info["delivery"]
            d_dedupe = dim_info["dedupe"]
            debug["q_kind"] = classify_question(user_message)   # 兼容字段，保留
            if d_dedupe:
                seen_sig, kept = set(), []
                for m in memory_context:
                    sig = (m.get("summary") or "").strip()
                    if sig and sig in seen_sig:
                        continue
                    seen_sig.add(sig)
                    kept.append(m)
                debug["dedupe_dropped"] = len(memory_context) - len(kept)
                memory_context = kept
            context_parts = ["【相关记忆】"]
            for m in memory_context:
                ref = m.get("ref", "")
                if ref:
                    parts = ref.split("#")
                    if len(parts) == 2:
                        cold = read_cold_memory(date_str=parts[0],
                                                entry_id=parts[1], max_entries=1)
                        if cold:
                            m["cold_content"] = f"用户: {cold[0].get('user','')}\n助手: {cold[0].get('assistant','')}"
                            cold_reads += 1
                update_warm_entry_call_count(m["id"], 1, m.get("ref", ""))
                # 日期必须出现在**每条记忆的最前面**（2026-09-26）。
                #
                # 为什么改：这段上下文原先只有 `[type] summary (关键词)`，**日期整条丢掉了** ——
                # 与 2026-09-26 在 B6/MAP 那条路上修掉的是同一个缺陷
                # （`protocol.py` 的注释里记着实测代价：不带日期 cat2 F1=0.220，
                #  带上 0.353，**+13.3 点**）。对话这条路当时漏改了。
                # 后果：记忆摘要里写"昨天""上周"，模型没有任何锚点可换算成绝对日期，
                # 只能原样复述相对词 —— 实测 9B 就是这么栽的（答 `Yesterday` 而不是具体日期）。
                # 位置放最前、用方括号，因为它就是时间推断的锚，必须一眼可见。
                date = (m.get("source_date") or "").strip()
                date_head = f"[{date}] " if date else ""
                context_parts.append(
                    f"- {date_head}[{m.get('type','')}] {m.get('summary','')}"
                    + (f" (关键词: {', '.join(m.get('topics',[]))})" if m.get("topics") else ""))
                # ---- 按**维度**分流交付（v1.3.1；原为 2 类，2026-09-26 起）----
                # raw（A/B1/C/D）：补原文。事实、数字、日期在摘要里会被**漏掉甚至写错**
                #   （实测摘要把 `4 years` 写成 `Five years`），只有原文靠得住。
                #   长度上限 600 的来历：实测云端在"摘要+原文"下 cat2 +0.211 / cat4 +0.151。
                # summary（B2）：给摘要 + 实体。推理靠人物设定与关系，不需要逐字对话；
                #   省下的体积可以多给几条记忆。
                if d_delivery == 'raw':
                    if m.get("cold_content"):
                        context_parts.append(f"  原文: {m['cold_content'][:600]}")
                else:
                    ents = m.get("entities") or m.get("core_entities") or []
                    if ents:
                        context_parts.append(
                            f"  实体: {', '.join(str(e) for e in ents)}")
            context_block = "\n".join(context_parts)
            debug["cold_reads"] = cold_reads

        if kb_results:
            if confirmed_kb_paths:
                kb_results = [k for k in kb_results if k["path"] in confirmed_kb_paths]
            kb_parts = ["【知识库文件】"]
            for kr in kb_results:
                content = read_kb_file_content(kr["path"], max_chars=3000)
                kb_parts.append(f"- 📄 {kr['name']}（匹配度 {kr['_score']:.1f} 分）")
                if content:
                    kb_parts.append(f"  {content}")
            kb_context_block = "\n".join(kb_parts)

        # 组装 prompt
        max_msgs = cfg.get("llm", {}).get(
            "small" if use_small_model else "large", {}
        ).get("max_history_messages", 100)
        history_slice = history if (max_msgs < 0 or max_msgs > 99999) else history[-max_msgs:]
        full_prompt = "".join(
            f"{'用户' if msg.get('role')=='user' else '助手'}: {msg.get('content','')}\n"
            for msg in history_slice)
        full_prompt += f"用户: {user_message}\n助手:"
        if context_block or kb_context_block:
            full_prompt = f"{context_block}\n\n{kb_context_block}".strip() + "\n\n" + full_prompt

        system_prompt = _build_system_prompt(personas, persona, persona_custom)

        # 调用 LLM
        reasoning_content = ""
        if not llm:
            assistant_text = "LLM未初始化，请检查后端配置。"
            debug["api_error"] = "LLM未初始化"
            _set_pipeline(request_id, "done", done=True)
        else:
            _set_pipeline(request_id, "generating")
            t0 = time_mod.time()
            try:
                if use_small_model:
                    small_cfg = cfg.get("llm", {}).get("small", {})
                    if not small_cfg.get("enable_reasoning", True):
                        system_prompt = (
                            (system_prompt + "\n\n请直接输出最终答案，不要输出任何思考过程或思维链内容。只输出回答本身。")
                            if system_prompt else "请直接输出最终答案，不要输出任何思考过程或思维链内容。只输出回答本身。"
                        )
                    reply = llm.infer_small(full_prompt, system_prompt, task_type="chat")
                else:
                    large_cfg = cfg.get("llm", {}).get("large", {})
                    if large_cfg.get("enable_reasoning") and large_cfg.get("reasoning_model"):
                        reply = llm.infer_large(full_prompt, system_prompt,
                                                 override_model=large_cfg["reasoning_model"])
                    else:
                        reply = llm.infer_large(full_prompt, system_prompt)
                debug["api_time_ms"] = round((time_mod.time() - t0) * 1000)
                if isinstance(reply, dict):
                    assistant_text = reply.get("content", "")
                    reasoning_content = reply.get("reasoning_content", "")
                else:
                    assistant_text = reply
                if assistant_text:
                    if assistant_text.startswith("[错误]"):
                        debug["api_error"] = assistant_text
                        debug["api_status"] = "error"
                    else:
                        debug["api_status"] = "ok"
                else:
                    assistant_text = (
                        "抱歉，本地模型返回空回复。请检查 Ollama 是否启动、模型是否拉取。"
                        if use_small_model else
                        "抱歉，云端模型返回空回复。请检查 API Key。"
                    )
                    debug["api_error"] = "空回复"
                    debug["api_status"] = "empty"
            except Exception as e:
                debug["api_time_ms"] = round((time_mod.time() - t0) * 1000)
                debug["api_error"] = str(e)[:200]
                debug["api_status"] = "exception"
                model_tag = "小模型" if use_small_model else "大模型"
                assistant_text = f"抱歉，调用{model_tag}异常: {str(e)[:100]}"
                log_exception(f"chat.{'small' if use_small_model else 'large'}_model", e)

        # ★ V1.3.4 步骤 3：反问/拒答的**登记与回传**。
        #
        # 三条硬约束（LIS 补充文档 §2.4 ③）在这一段的落地：
        #   ② 反问**必须在单次请求内返回完整回答** —— 这里只是给已有的 `assistant_text`
        #      打标，**没有任何等待点**，所以不会像 `mode="preview"` 那样在无人确认时
        #      "停在半路拿不到回答"。
        #   ③ 触发时登记一条事件，使**反问率与真阳性率可统计**（对应 LIS 的
        #      `unfilled request`）。
        # 判定用的是产品与评测**共用的**那一份（`B6/clarify.py`），不在这里另写。
        _cl = _clarify_annotate(assistant_text)
        debug["clarify_triggered"] = _cl['clarify']
        debug["refused"] = _cl['refuse']
        if _cl['clarify']:
            debug["clarify_question"] = _cl['question']

        # 记录冷记忆（对话后落盘）
        entry_id = ""
        if not hasattr(app.state, "_last_pair"):
            app.state._last_pair = None
        try:
            if app.state._last_pair:
                prev_user, prev_assistant = app.state._last_pair
                entry_id = record_conversation(prev_user, prev_assistant)
                # 触发自动提炼（通过 memory 域暴露的函数）
                if hasattr(app.state, "_trigger_auto_extract"):
                    app.state._trigger_auto_extract()
            app.state._last_pair = (user_message, assistant_text)
        except Exception as e:
            log_exception("chat.record", e)

        _set_pipeline(request_id, "done", done=True)
        return {
            "reply": assistant_text, "reasoning_content": reasoning_content,
            "memory_context": memory_context[:5] if memory_context else [],
            "entry_id": entry_id,
            "keywords_used": search_query if memory_enhance else "",
            # 前端可据此把"反问"与"拒答"单独呈现（LIS：反问是追问，不是放弃）
            "clarify_triggered": _cl['clarify'],
            "clarify_question": _cl['question'],
            "debug": debug, "request_id": request_id,
        }

    @app.get("/chat/status/{request_id}")
    async def chat_pipeline_status(request_id: str):
        _cleanup_old_pipelines()
        s = _pipeline_status.get(request_id, {"stage": "not_found"})
        return dict(s)

    @app.post("/persona/generate")
    async def persona_generate(request: Request):
        from services.shared.errors import log_exception
        body = await request.json()
        description = body.get("description", "")
        if not description:
            return {"error": "请提供人设描述"}
        llm = app.state._llm_state["llm"]
        if not llm:
            return {"error": "LLM未初始化"}
        try:
            result = llm.infer_small(
                _PERSONA_GENERATE_PROMPT.format(description=description),
                task_type="persona_generate")
            return {"persona_prompt": result.strip() if result else ""}
        except Exception as e:
            log_exception("persona.generate", e)
            return {"error": str(e)[:200]}
