# Phase 2 之后 · 下一批待办

> 来源：`NEXT-待办-B6验证器与热度衰减.md §4`（2026-09-22）
> 编号按影响 / 难度排序，建议从 #1 开始串行做

---

## #1 · 层迁移接调度（最低难度 / 最高收益）

**现状**：`decay_memory.py --migrate-layers` 开关已加，但**默认关**；`migrate_layers()` 逻辑已修好（heat>0 前置，层判定不再把冷层恒空）。

**问题**：heat 会衰减、layer 不会跟着变 → 二者逐渐脱钩。本库当前 `hot:734 / cold:34` 是一次性迁移留下的，热层里 700+ 行其实 heat=0，是假热。

**做法**：给计划任务加一层。MemoryDecay 跑完衰减后紧接着跑 `decay_memory.py --apply --migrate-layers`。

**手动预演**：
```powershell
python -X utf8 "<UPSTREAM_DIR>\bin\decay_memory.py" --apply --migrate-layers
# 预期：hot 从 734 降到 ≈29（真有热度的那些），cold 从 34 升到 ≈739
```

**验收**：
- 计划任务 `LastTaskResult=0`
- 热层数量显著减少（从 734 到 ≈29）
- 冷层非空且有内容（不是全被 hot 抢占）
- 真库 sum(heat) 不变（层迁移不碰 heat）

---

## #2 · 两条漏网路径要不要过 validator

**现状**：
- `mcp__xilian__memory_search_simple` 直接调 B4，返回裸 list → **三条硬规则在它上面不生效**
- `mcp_tools/memory.py /memory/search`（V1 兼容）直接调 B4，自己又写了一遍审计 → **同一次检索产生 2 条 search，trace 还不同**

**问题**：硬规则只在 `mcp__xilian__memory_search`（MAP 信封）上成立。两条漏网路径绕过了它。

**建议方案**：
- `memory_search_simple` —— 保持现状，在 README 里注明「这个入口绕开 validator，不保证硬规则 1/2/3」。理由：它是 dsh 侧最省 token 的入口，改契约会影响主 Agent 的用法（见 #3）
- `/memory/search` —— 删掉（或内部转发到 `request_handler`）。同库同一次检索两条 search 审计是 bug

**前置**：#3 拍板后再做

---

## #3 · memory_search_simple 的契约（**需要你拍板**）

**现状**：dsh 可以选 `memory_search`（MAP 信封，全校验，返回完整 MemoryResponse）或 `memory_search_simple`（裸 list，省 token，绕过校验）。

**问题**：要不要让 `memory_search_simple` 也过 validator？

**选项**：
| 选项 | 效果 |
|------|------|
| A. 保持现状（推荐） | simple 就是裸性能入口，调用方知道自己在绕开校验 |
| B. 给 simple 加校验 | 但 MAP 信封里 needs_clarification/empty/degraded 这些字段裸 list 没地方放，改返回值会改工具名或加新字段，影响 dsh 侧 |
| C. 删掉 simple，只留一个 memory_search | 最干净但省 token 的路径没了 |

---

## #4 · cold_content 读整天 JSONL 取到文件开头（低难度）

**位置**：`mcp_tools/memory.py` 的 `cold_content` 字段生成逻辑

**问题**：`open().read()[:500]` 读的是整天 JSONL 文件的前 500 字符，不是这条记忆的原文（同 §3.2 是同一族 wart）。

**做法**：直接复用 `B6.read_drilldown()`（已经在 Phase 2 里逐行 hash 精确命中了）。

---

## #5 · 温提炼行没有原文（需要你决定方向）

**现状**：36 条温记忆全部 `content_path=""` → `has_deeper=false` → 钻不下去。Phase 2 的 verify-b6 里 drilldown 只能命中冷层 JSONL 里的行。

**选项**：
| 选项 | 代价 |
|------|------|
| A. 让 B2 写 content_path 指向温层文件 | 温层文件也是 JSONL，需要在 B2 提炼时记住原始位置 |
| B. 接受「只有 V1 迁移行有原文」 | 简单但温记忆永远钻不下去 |

---

## #6 · C1 知识库 / D1 技能库 / D2 委派（大任务，另立）

C1 知识库可接 B4 做向量检索（文件也写入 memories 表，type="知识"）；D1 技能库盘点；D2 委派协议。

---

## 项目惯例（Phase 2 新增 4 条，全部踩出来的）

沿用原来的（审计/升温/衰减失败不能让检索失败；接线接 service 层；破坏性操作先备份 + 默认 dry-run；改完 Python 要重启 MCP 子进程；验证脚本要自清理；`.ps1` 必须 CRLF；`Start-Process -ArgumentList` 要自己加引号；跑 Python 要设 `[Console]::OutputEncoding`），Phase 2 新增：

| 惯例 | 原因 |
|------|------|
| **验证脚本要先 `SHUFANG_DATA_DIR=<临时目录>` 再 import services**（loader 在 import 期就解析完 paths.*，顺序反了隔离就失效） | 这是 Phase 2 两个验证脚本的隔离手法：**同一份代码**跑在临时库上，一根手指不碰真库；真库只做「前后不变」的旁观断言 |
| **PowerShell 里 `2>&1 \| Out-String` 会把 stderr 变成一大坨 NativeCommandError 报错块**；`$ErrorActionPreference='SilentlyContinue'` 会把 stderr 内容整段丢掉 | 正解：合并流后逐条抽消息（见 decay-run.ps1） |
| **索引器/搬运工不许碰运行时状态列** | heat/count/updated_at/layer/data_sovereignty 分别是 B4·B5·用户主权的状态，索引器只能更新内容字段。用 INSERT OR REPLACE 就会把它们抹掉（§3.5） |
| **降级必须显式**（degraded=true + 原因 + 审计） | 只返回空 empty=true，调用方分不清「真没有」和「坏了」——那是静默撒谎 |
