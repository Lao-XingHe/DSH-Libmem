# Phase 2 改动清单

## 核心原则：**接线接 service 层**

MCP / FastAPI / CLI 都汇合到 service 层，校验/升温/审计只在那里跑一份。上层工具只做参数转发，不重复实现。

---

## V2 主代码改动（`shufang-os-拆解/v2/`）

### `services/B6_map_protocol/protocol.py`

| 改动 | 位置 |
|------|------|
| `request_handler` 变成带校验的 service 层入口（委托给 `guarded_request_handler`） | L576+ |
| 新增 `raw_request_handler(req, apply_heat=False)` —— 裸管道，无校验、零副作用 | L632+ |
| 新增 `guarded_request_handler(req)` —— 校验 request → 零副作用生成 response → 校验 response → 通过后 `commit_retrieval()` 提交升温 + 审计；两次都不通 → `degraded=True` + 审计 | L576+ |
| 新增 `MAPValidator.check_and_retry()` —— 通用「校验 + 重试 1 次 + 降级」 | L700+ |
| 新增 `drilldown_handler(packet)` + `read_drilldown(memory_id, depth)` —— memory_drilldown 三包落地（逐行 hash 命中整天 JSONL） | L758+ |
| 硬规则 1 判定修复：改 `detect_ambiguity(query)`，去指代词后实义 <5 字才算 | L520+ |
| confidence 用 `_sim`（夹 [0,1]），排名分另放 `header.score` | items 构造处 |
| `followup_hint` 多段之间用 `；` 分隔 | |
| `request_id='那个东西呢'` → needs_clarification=True + `clarification_question` + 原因 | |
| 连续失败 2 次 → `degraded=True` + `degraded_reason` + B7 审计 `kind=b6_degraded` | |

### `services/B4_dual_gate_retrieval/retrieval.py`

| 改动 | 位置 |
|------|------|
| 新增 `commit_retrieval(result, query_text, filters)` —— 把 `apply_heat=False` 纯读结果**提交一次**副作用（升温 + 审计，**不重复**） | L636+ |
| `retrieve()` 支持 `apply_heat=False` 模式 | |
| 返回结构新增 `trace_id` 贯通到 B7 | |

### `services/B3_header_engine/sqlite_store.py`

| 改动 | 位置 |
|------|------|
| `bulk_insert()` / `insert_memory()` 从 `INSERT OR REPLACE` 改 **UPSERT ON CONFLICT(memory_id) DO UPDATE** | L94+ |
| 冲突时**只更新内容字段**：summary / content_path / core_entities / task_type / data 等 | UPSERT SQL 列表 |
| 运行时状态列一律保留：heat / count / updated_at / layer / data_sovereignty | 明确在 DO UPDATE SET 里不包含 |
| `update_layer(memory_id, layer, touch=True)` 新增 `touch` 参数 —— 迁移时传 False 不刷新 updated_at | |

### `services/B5_heat_state/heat_state.py`

| 改动 | 位置 |
|------|------|
| 层判定加 `heat > 0` 前置条件 —— 老逻辑在「多数记忆还没被检索过」的库上会把**全库**判成 hot | migrate_layers |
| `decay_all()` 衰减后刷新 `updated_at`（不变式：heat 永远表示「截止 updated_at 时刻的热度」） | |

### `services/C2_mcp_server/mcp_server.py`

| 改动 | 位置 |
|------|------|
| `memory_drilldown` 从 MCP 工具层挪到 service 层（`B6.drilldown_handler`），工具层只做参数转发 | L129+ |
| `memory_search_simple` 注明绕开 validator（它返回裸 list，没地方放 needs_clarification/empty/degraded） | |

### `config.yaml`

| 改动 |
|------|
| 新增 `b6_map_protocol.flash_routing`（默认 `local_degrade`） |

---

## 插件目录（`<UPSTREAM_DIR>\`）

### 新建

| 文件 | 职责 |
|------|------|
| `bin/decay_memory.py` | `--apply` 才写库；先 sqlite3 在线备份（走 WAL）；备份切 journal_mode=DELETE；校验备份一致；复核 sum(heat) 变化；写 B7 审计 |
| `bin/decay-run.ps1` | UTF-8 BOM + CRLF；`[Console]::OutputEncoding` 设 UTF-8；>2000 行截断到 500；调 `--apply`（无则 dry-run） |
| `bin/run-hidden.vbs` | `wscript.exe //nologo` 调 ps1，避免每次弹控制台窗口 |
| `bin/verify-b6.py` | 64 项；注入非法响应、手动调用 guarded、裸 raw 两次升温对照、真库 heat/count 还原 |
| `bin/verify-decay.py` | 54 项；临时库造 19 行种子；半衰期数学、幂等、updated_at 语义、四象限、层迁移不污染时钟、调度三件套 |
| `bin/verify-reindex-heat.py` | 14 项；临时库 36 行；堆 heat 到 324 → 再跑 index_warm → heat 不回 0 |

### 现有

| 文件 | 改动 |
|------|------|
| `bin/qc_memory.py` | + `heat_wiped` 检查（审计里有 `retrieval_heat` 却 `sum(heat)=0` → error） |

---

## Windows 计划任务

| 任务 | 时间 | 命令 | 备注 |
|------|------|------|------|
| `MemoryQC` | 每 15 分钟 | wscript `run-hidden.vbs` `qc-run.ps1` | 只读；绝不修改数据 |
| `MemoryDecay` | 每天 18:00（StartWhenAvailable=true，错过开机补跑） | wscript `run-hidden.vbs` `decay-run.ps1` | 衰减 + 备份；独立任务独立日志 |

---

## 回滚方法

### 代码回滚
恢复 `v2/` 下所有被修改的文件（git checkout 或复制备份）。

### 调度回滚
```powershell
Unregister-ScheduledTask -TaskName MemoryDecay -Confirm:$false
# MemoryQC 保留（它只做质检不修改数据）
```

### 库回滚（衰减后发现有问题）
```powershell
# ⚠️ 先删掉 -wal/-shm，否则旧 WAL 会被重放
del header.db-wal header.db-shm
# 备份是单文件自包含（journal_mode=DELETE），直接拷回去
copy header.db.bak-decay-<时间戳> header.db
# 校验：sum(heat) 应该精确复原，目标行 10/3 复原
```
