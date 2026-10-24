# MAP-D 委派协议 · 落地说明

> 2026-09-25 ｜ 规范：[主Agent委派协议_MAP-D_v0.1.md](file:///C:/Users/admin/Desktop/书房OS-DSH大迁移/主Agent委派协议_MAP-D_v0.1.md)
> 结论：**MAP-D 的运行时机制由 dsh 原生 subagent 完整提供，本项目不重造协议层**，
> 只做两件事 —— ① 挂载并路由到小模型；② 守住「工具过程不沉淀进记忆库」。
>
> 本次范围：D2 委派。C3 人设库、D1 技能库按约定暂不做。

---

## 一、改了什么（两处，都在配置/插件层，没动 B1~B7）

| # | 文件 | 改动 | 对应 MAP-D 条款 |
|---|------|------|----------------|
| 1 | `C:\Users\admin\.dsh\profiles\web\cordis.patch.yml` | 新增 `- insert:` 块，挂载 `dsh-subagent` + `dsh-subagent-spawn-in-process` + `dsh-tool-subagent`，子级路由到本地 `llama-local` 的 `Qwen3.6-35B-A3B-UD-IQ3_S` | §2 三层角色、§5 小模型路由 |
| 2 | `D:\Deepseek Harness\dsh-cold-memory-watcher\lib\index.js` | `session/event` 钩子加子会话守卫：`header.origin === 'subagent'` 或 `header.parentSession` 存在 → 跳过，不写冷库 | §7 记忆沉淀规则、§1.1 记忆污染 |

### 为什么不自造 Python 协议层

MAP-D §3 定义了 TaskDispatch / TaskResult / Dig 三个包。检查后确认**三个包在 dsh 原生都已实现**，
在 Python 侧再写一遍 dataclass + validator 会是死代码（没有消费者：主 Agent 用的是 dsh 原生
`subagent` 工具，不会去调一个自造的 MCP 委派工具）。

这个判断有前车之鉴 —— 之前给小模型做的「前台接待员 agent」协议，A/B benchmark 证明路径 B
全线失败（0% 命中、延迟 527~1476×），那份投入完全浪费。这次先确认「原生是否已覆盖」再决定写不写。

---

## 二、MAP-D 三个包 → dsh 原生机制映射（已核实）

| MAP-D 包 / 条款 | dsh 原生机制 | 证据 |
|----------------|-------------|------|
| **TaskDispatch**（§3.1 委派请求） | `subagent` 工具的 `prompt` 参数 —— 任务包以自然语言写进提示词 | `dsh-tool-subagent` 只暴露 `prompt` / `label`（+后台参数） |
| **TaskResult**（§3.2 结果摘要 L1） | 子 Agent 的**最终文本**。one-shot 前台调用只回这一条，**中间步骤不进主上下文** | `dsh-tool-subagent` README：「成功时只包含子 agent 的最终文本」「子 agent 中间步骤不会进入父级」 |
| **Dig**（§3.3 深挖通道） | 子会话存档 + `send_message` 继续追问 | 需把 `backgroundMode` 改成 `continuable` 并挂 `dsh-tool-subagent-control`（**本次未开**） |
| **小模型路由**（§5 模型档位） | `agentOptions: {provider, model, reasoningEffort, maxTokens}` 覆盖子级路由 | `dsh-tool-subagent` 配置表；两个进程内后端声明支持 `agentOptions` |
| **§7 工具过程不沉淀** | 子会话带 `origin: 'subagent'` 会话头 → watcher 跳过 | `dsh-subagent` 的 `SessionHeader`：`origin?: 'subagent'`、`parentSession?: SessionId` |
| **§4 分流规则** | **无原生机制** —— 靠主 Agent 判断（见第四节） | — |

---

## 三、派发提示词契约（重要）

MAP-D §3.1/§3.2 的 `return_format`（`required_fields: [结论, 数据来源, 置信度]`）**不能靠协议字段强制**，
只能靠**提示词约束**。主 Agent 委派时应把要求写进 `prompt`，例如：

```
任务：查一下 2026 年 9 月佛山房价环比数据。
约束：最多 5 步；优先用 web 检索类工具；只做这一件事。
返回要求（严格）：只输出下面三行，不要寒暄、不要过程：
结论：<一句话>
数据来源：<来源列表>
置信度：<0~1>
```

子 Agent 的最终文本会原样成为工具结果回到主上下文 —— 这就是 MAP-D 的 L1 摘要。

> ⚠️ 结构是否被遵守取决于子模型。本地 35B 对格式指令的服从度需要实测（见第七节风险）。

---

## 四、分流规则（§4 落地）

MAP-D §4 的 `should_delegate(task)` 是**主 Agent 的判断依据**，不是代码。给主 Agent 的指引：

| 场景 | 处理 | 原因 |
|------|------|------|
| 单次、低噪声工具（查天气、取一条记录、读一个文件） | **直接调** | 委派往返更慢、更贵 |
| 多步骤、高噪声流程（查资料→整理→汇总） | **委派** | 过程噪声巨大，必须隔离 |
| 需要判断/迭代的检索 | **委派** | 子 Agent 内部可多次试错 |
| 用户明确「帮我查一下 X」 | **委派** | 用户只关心结论 |
| 需要主 Agent 连续推理的中间步骤 | **直接调** | 委派会切断推理链 |

判据：`steps <= 1 且 低噪声` → 直接调；`steps >= 3 或 高噪声` → 委派；否则看是否需试错。

---

## 五、当前配置（profile 原文）

```yaml
- insert:
    - id: dsh-subagent
      name: '@deepseek-ai/dsh-subagent'
    - id: dsh-subagent-spawn
      name: '@deepseek-ai/dsh-subagent-spawn-in-process'
    - id: dsh-tool-subagent
      name: '@deepseek-ai/dsh-tool-subagent'
      config:
        provider: spawn
        toolName: subagent
        backgroundMode: one-shot
        agentOptions:
          provider: llama-local
          model: Qwen3.6-35B-A3B-UD-IQ3_S
          maxTokens: 4096
```

**常用调整**：

| 想做什么 | 怎么改 |
|---------|--------|
| 子级换回云端大模型（最稳） | 删掉 `agentOptions` 三行 → 子级继承父级 `deepseek-official/deepseek-flash` |
| 子级换轻量档（更快更省） | `model:` 换成 `Qwen3.5-9B-Q4_K_M` 或 `Qwen3.5-4B-Q4_K_M` |
| 开 Dig 深挖通道（§3.3） | `backgroundMode: continuable`，并加挂 `@deepseek-ai/dsh-tool-subagent-control` |
| 限制递归委派深度 | 加 `maxDepth: 1`（子 Agent 不能再委派；**需提供方支持 `depthLimit`，否则挂载会失败**） |

> 刻意没设 `maxDepth` / `persona` / `toolFilter` —— 这三项都要求提供方声明对应能力，
> 若 `spawn-in-process` 未声明会在**挂载时**失败（不是首次委派时），风险不必要。
> 默认 `maxDepth` 是 3，够用。

---

## 六、启用与验证步骤

**本轮只做了静态验证**（见第六节末）。运行时验证需要重启 dsh：

1. **重启 dsh harness**（profile 改动需要重新加载）。
2. **确认挂载成功**：读 `D:\Deepseek Harness\dsh-hybrid-gateway\load-status.json`，
   以及 dsh 启动日志里 plugin 加载无报错。
   - 若 `agentOptions` 或 `provider: spawn` 写错，会**在挂载时报错**（不是静默失败）——
     这是好事，说明配置错了能立刻发现。
3. **试一次委派**：让主 Agent 做一件"多步高噪声"的事（如"帮我查一下 X 并汇总"），
   观察它是否调用 `subagent` 工具、子级是否走了本地 35B。
4. **确认子会话没污染记忆库**：读
   `D:\Deepseek Harness\dsh-cold-memory-watcher\watch-status.json`，
   看 `subagentEventsSkipped`：
   - `> 0` → 守卫生效（子会话事件确实到达了钩子，且被拦下）
   - `== 0` → 两种可能：没委派，或 dsh 没把子会话派发到全局钩子（则本来就没污染）

### 本轮已做的静态验证

| 项 | 结果 |
|----|------|
| profile YAML 合法（4 条目，新增第 4 条含 3 个模块） | ✅ `yaml.safe_load` 通过 |
| `dsh-subagent` / `-spawn-in-process` / `-tool-subagent` 包存在 | ✅ 均在 npm 安装目录 |
| `agentOptions` 是合法工具配置键（非杜撰） | ✅ 见 `dsh-tool-subagent` README 配置表 |
| `SessionHeader.origin: 'subagent'` 标记真实存在 | ✅ 见 `dsh-subagent` 编译产物 |
| watcher `index.js` 语法 | ✅ `node --check` 通过 |

---

## 七、风险与未验证项（诚实声明）

| 项 | 状态 | 说明 |
|----|------|------|
| **profile 是否真能加载** | ⚠️ 未验证 | 需重启 dsh。配置错会在挂载时报错，可快速发现 |
| **本地 35B 的工具调用成功率** | ⚠️ 未验证 | 正是 MAP-D §10 开放问题 1。MoE 35B-A3B 的**激活参数仅 ~3B**，工具调用可能明显弱于 deepseek-flash。若失败率高，删 `agentOptions` 回退云端 |
| **委派端到端延迟** | ⚠️ 未验证 | §10 开放问题 2。本地模型首 token 慢，可能不如直接调 |
| **子会话是否真会进冷库** | ⚠️ 未验证 | 守卫是**防御式**的：若子会话根本不到达钩子，`subagentEventsSkipped` 恒为 0，行为与改动前一致 |
| **记忆沉淀的"另一半"（结论入记忆库）** | ❌ 未做 | §7 要求「结果摘要的**可复用结论**→ 进记忆库」。目前子会话摘要留在主上下文，是否沉淀成记忆由主 Agent 决定，没有自动通道 |
| **Dig 深挖通道（§3.3）** | ❌ 未做 | 需要 `continuable` + `dsh-tool-subagent-control` |
| **C3 人设库 / D1 技能库** | ❌ 按约定不做 | 用户明确「可以暂时不做」 |

---

## 八、与旧架构的关系

- **B1~B7 完全没动** —— 委派是执行侧的事，与记忆检索/审计解耦。
- **B6 MAP 协议**（记忆三包）与 **MAP-D**（委派三包）是**兄弟扩展**，不是同一层：
  B6 管「主 Agent ↔ 记忆库」，MAP-D 管「主 Agent ↔ 子 Agent」。
- **冷库管道** 仍由 `cold-memory-watcher` 写入，本次只加了一道子会话闸门。
- 本次没有新增 `D2` Python 模块 —— 见第一节「为什么不自造 Python 协议层」。