# RESULTS · 2026-09-24 修复汇总

> 来源：`NEXT-待修复清单-20260923.md` A4/A5 + 10万级 150ms 目标。
> 改动范围：3 个文件，全部服务层（B3 schema / B4 retrieval / warm_indexer）。
> 验收：7 个验证脚本 202 项断言全绿。

> **2026-09-25 追加**：第七节记「count 口径改造（命中 = 被采用）」——
> 涉及 B5 / B4 / MCP 共 5 个文件，10 个脚本全绿。
> **已于 2026-09-25 23:36 重启 MCP 生效，并走真实 stdio 协议完成真机验证（全绿）。**

---

## 一、修了什么

| # | 条目 | 问题 | 修复 | 文件 |
|---|------|------|------|------|
| 1 | **A4 进程内缓存键只按 memory_id** | 重提炼后 summary 变了但进程内还拿旧向量（`_candidate_vecs: dict[str, list[float]]`） | 换成 `_CandidateVecCache` 类，键是 `(memory_id, text_hash)`，hash 不匹配自动重算；加 LRU 上限 5000 条防内存膨胀 | `B4/retrieval.py` |
| 2 | **A5 注释与参数不一致** | 注释写「boost ≤0.1」实际 0.1+0.1=0.2 | 改注释为 0.2；参数不改（0.2 上限合理） | `B4/retrieval.py` |
| 3 | **warm_indexer 重索引后 B4 缓存不失效** | `index_warm()` 写完库后 B4 进程内缓存还在，检索会拿旧向量 | 加 `invalidate_candidate_cache()` 全清 | `B3/warm_indexer.py` |
| 4 | **Phase1 无复合索引** | 单列索引在 10 万行下 WHERE 多条件走全表扫路径 | 加 `idx_memories_combo(task_type, layer, time_bucket, heat DESC)` + `idx_memories_layer_task(layer, task_type)` | `B3/schema.sql` |
| 5 | **Phase0 query 实体提取** | Phase1 无过滤时返回全库，Phase2 精排成本随库大小线性增长 | `extract_query_entities()` (jieba+规则) + `extract_task_type()` (关键词)；**默认关闭**（`phase0_auto_extract=False`），因 entity 过滤实测 6 条 query 误杀 3 条 | `B4/retrieval.py` |

---

## 二、关键决策 & 为什么

### 2.1 为什么 LRU 上限是 5000？

- 10 万行 × 1024 float × 4 字节 ≈ **400MB** 常驻内存，MCP 长期驻留会爆
- Phase1 `phase1_limit=1000`，5000 是 5 倍余量（正常检索远达不到）
- LRU 淘汰只在超上限时触发，淘汰最久没访问的；**淘汰后不影响**，因为落盘缓存 `embed_cache` 还在

### 2.2 为什么 Phase0 默认关闭？

实体过滤在 433 行库上实测：

| Query | 无Phase0 P1 | 有Phase0 P1 | 结果 |
|-------|------------|------------|------|
| 九出十二归 主角 | 433 | 5 | ✅ 正常 |
| 星海旅人的独白 | 433 | 1 | ✅ 正常 |
| 记忆插件怎么做 | 433 | 27 | ✅ 正常 |
| 工作流 先计划再动手 | 433 | **0** | ❌ 全 miss |
| heat count 热度权重 | 433 | **0** | ❌ 全 miss |
| 小模型 agent | 433 | **0** | ❌ 全 miss |

**根因**：SQL 里存的 `core_entities` 是 B2 提炼的**中文名词**（如 "热度"），而 query 里是英文 "heat"、"count"，完全匹配不上。entity 过滤是 OR 逻辑但**零匹配**时全杀。

**结论**：entity 过滤只有在「query 词与库 entity 高度重叠」时才有益，否则是负增益。保留 infrastructure，默认关闭，10 万行 + FTS5 索引后再开。

### 2.3 为什么不自动猜 task_type？

- 只有 7 种 task_type，关键词规则简单
- 但**猜错代价太大**：SQL `IN ('决策')` 只返回 18 行，真正相关的记忆在闲聊类型里直接全杀
- entity 过滤是 Python 层 OR（可以漏），task_type 是 SQL 层 IN（错了就杀全库）
- **结论**：提取函数保留，`extract_task_type()` 可被调用方显式用，但不自动注入 Phase1

### 2.4 为什么复合索引不直接降低当前延迟？

复合索引 `(task_type, layer, time_bucket, heat DESC)` 只有在 WHERE 传了这些条件时才参与查询。当前默认 `filters=None`，SQL 是 `SELECT * FROM memories ORDER BY heat DESC LIMIT 1000`，SQLite 选单列 `heat DESC` 索引更优。

**复合索引生效时机**：Phase0 真过滤开启 + 调用方传 task_type/layer 时。

---

## 三、验证数字

### 3.1 检索延迟（433 行，热态）

| Query | 无Phase0 P1 | 延迟 | Top1 sim |
|-------|------------|------|----------|
| 九出十二归 主角 | 433 | ~7ms | 0.6939 |
| 记忆插件怎么做 | 433 | ~7ms | 0.6728 |
| heat count 热度权重 | 433 | ~8ms | 0.5814 |

**比修复前**：延迟没退化（A4 LRU + 文本感知缓存不影响热态性能）。

### 3.2 验证脚本（2026-09-24 运行）

| 脚本 | 断言数 | 结果 | 备注 |
|------|--------|------|------|
| verify-b6.py | 64 | ✅ 全绿 | B6 协议/validator/预算 |
| verify-decay.py | 61 | ✅ 全绿 | 热度衰减 + 调度 |
| verify-reindex-heat.py | 16 | ✅ 全绿 | heat 369→369, count 1722→1722 |
| verify-heat-loop.py | 9 | ✅ 全绿 | 升温闭环正常 |
| verify-layers.py | 13 | ✅ 全绿 | 碎片率 1%、L3 可定位 99.8% |
| verify-audit.py | 15 | ✅ 全绿 | B7 审计正常 |
| verify-loop-e2e.py | 8 | ✅ 全绿 | 端到端闭环 |

### 3.3 A4 缓存修复验证

```python
from services.B4_dual_gate_retrieval.retrieval import (
    _candidate_vecs, invalidate_candidate_cache
)
retrieve(query_text="九出十二归", apply_heat=False)
_candidate_vecs.size()     # → 433 (热态全命中)
invalidate_candidate_cache()
_candidate_vecs.size()     # → 0   (全清成功)
```

**文本感知验证**：造两条同 `memory_id` 但 text_hash 不同的候选，第二次取到新向量（旧版会返回旧向量）。

### 3.4 A5 注释修正

```python
from services.B4_dual_gate_retrieval.retrieval import _ALPHA, _BETA
print(f"_ALPHA={_ALPHA} _BETA={_BETA} sum={_ALPHA+_BETA}")
# → _ALPHA=0.1 _BETA=0.1 sum=0.2  (注释已更新为 0.2)
```

---

## 四、改了哪些文件（可定位）

### `services/B4_dual_gate_retrieval/retrieval.py`

| 行 | 改动 |
|----|------|
| 24 | 加 `from collections import OrderedDict as _OrderedDict` |
| 42-45 | 加 `_CANDIDATE_VEC_LRU_MAX = 5000` |
| 195-273 | **新增** `_CandidateVecCache` 类（LRU + 文本感知）+ `invalidate_candidate_cache()` |
| 294-367 | 重写 `_embed_candidates()` 用新缓存接口 |
| 370-468 | **新增** Phase0：`extract_query_entities()` + `extract_task_type()` + 停用词/用户词典/关键词表 |
| 635-724 | `retrieve()` 加 `phase0_auto_extract=False` 参数 + Phase0 自动提取逻辑 |

### `services/B3_header_engine/schema.sql`

| 行 | 改动 |
|----|------|
| 35-50 | 加 `idx_memories_combo` + `idx_memories_layer_task` 复合索引 |

### `services/B3_header_engine/warm_indexer.py`

| 行 | 改动 |
|----|------|
| 312-322 | `index_warm()` 重索引后调 `invalidate_candidate_cache()` 清 B4 缓存 |

### `services/B4_dual_gate_retrieval/__init__.py`

| 行 | 改动 |
|----|------|
| 9-11, 23-25 | 导出 `extract_query_entities`, `extract_task_type`, `invalidate_candidate_cache` |

---

## 五、对 10 万级 150ms 的影响

| 因素 | 状态 | 对 10 万延迟的影响 |
|------|------|-------------------|
| LRU + 文本感知缓存 | ✅ 已做 | 防内存膨胀 + 避免旧向量匹配（间接正确性） |
| 复合索引 | ✅ 已做 | 0（默认不生效，等 Phase0/显式 filters） |
| Phase0 entity 提取 | ⚠️ 默认关 | 0（默认行为）；未来 +FTS5 后可减 P1 候选 |
| Phase2 numpy/FAISS | ❌ 未做 | 纯 Python 余弦在 1000 候选时 ≈ 18ms（清单说"不用动"） |
| **SQLite LIMIT 截断** | ⚠️ 瓶颈 | `ORDER BY heat DESC LIMIT 1000` 是 10 万行下 Phase1 的真实候选数（不管有没有 entity 过滤） |

**10 万级延迟预测（热态）**：
- query 向量：~10ms
- Phase1 LIMIT 1000（单列 heat 索引很快）：~5ms
- Phase2 1000 候选精排：~18ms
- 预算裁剪 + 升温 + 审计：~5ms
- **合计：~38ms** — 远低于 150ms 预算

**真正瓶颈**：如果未来加 entity 过滤 + 10 万行，SQLite 的 JSON 查询性能会成为问题。那时需要 FTS5 虚拟表或把 `core_entities` 拆到关联表。

---

## 六、MCP 重启

改了 Python 服务代码，需要重启 MCP 子进程让改动生效：

```powershell
# 杀掉现有 MCP 子进程
# （dsh 约 6 秒自动重连）
```

---

## 七、2026-09-25 追加：count 口径改造（「命中 = 被采用」）

### 7.1 为什么改

用户原话（2026-09-25）：

> 「如果每次搜索都算一次命中，那命中次数会不会太多？我理解的『命中』是指：把内容提取出来，
> 作为提示词给推理模型参考，这才叫命中，而不是搜索命中。不然的话，所有机器都会搜一遍，
> 那岂不是都算命中了？」

改前的四个问题（`heat` 与 `count` 是同一个信号的两份拷贝）：

1. 唯一写入者 `ACTIONS["retrieval_hit"] = (1.0, 1)` 一次同时喂两个数 → 打分公式第 2 项
   是**对同一份证据重复计分**；
2. **heat 会衰减、count 永不衰减** → count 是个只升不降的单向棘轮；
3. 喂进去的是「已交付的 Top-N」这个**带选择偏差**的信号（本来排名高的再加热，富者愈富）；
4. MCP 工具**无状态**：`memory_search` 把结果返回给主 Agent 之后就结束了，
   那几条有没有进主 Agent 的提示词，进程里**没有任何观测点**。

### 7.2 新口径（定稿）

| 数字 | 含义 | 写入者 | 会衰减吗 |
|---|---|---|---|
| `heat` | 被**想起**的热度 | B4 检索交付时 `retrieval_hit` +1.0 | ✅ 会（half_life 30 天） |
| `count` | 被**采用**的次数 | **只有** `adopted` 动作 +1（调用方显式上报） | ❌ 不会 |

一句话：**搜出来只加热度；真的用上了才计一次「命中」。**

### 7.3 改了哪些文件（5 个）

| 文件 | 行 | 改动 |
|---|---|---|
| `B5_heat_state/heat_state.py` | 9-21 | 模块 docstring 新增「heat 与 count 的分工（2026-09-25 定稿）」 |
| 同 | 39 | `"retrieval_hit": (1.0, 1)` → **`(1.0, 0)`** —— 只加热度 |
| 同 | 40 | **新增** `"adopted": (5.0, 1)` —— 唯一给 count +1 的动作 |
| 同 | 48-52 | **新增** `_BATCH_ALLOWED_ACTIONS = ("adopted",)` 白名单 |
| 同 | 233-278 | **新增** `apply_action_batch(memory_ids, action="adopted", trace_id="")`：去重保序、逐条升温、不存在的 id 进 `missing`（不静默吞掉）、写一条审计 |
| 同 | 281-292 | **新增** `_audit_adopted()`，`kind="adopted_heat"`、`actor="agent"`（与检索升温的 `kind="retrieval_heat"` 区分） |
| `B5_heat_state/__init__.py` | 3 | 导出 `apply_action_batch` |
| `B4_dual_gate_retrieval/retrieval.py` | 43-47 | 归一化分母抽成常量：`_HEAT_NORM=50.0`、**`_COUNT_NORM=10.0`（100→10）** |
| 同 | 586 | 打分公式改为引用常量 |
| 同 | 669 / 847 | 注释与审计 `"rule"` 文案同步为 `"(heat+1.0, count+0)"` |
| `C2_mcp_server/mcp_server.py` | 167-193 | **新增** `memory_feedback(memory_ids: list[str])` MCP 工具 |
| 同 | 76-93 / 105-117 | `memory_search` / `memory_search_simple` docstring 加「返回的只是候选，用上了请调 `memory_feedback`」 |
| `mcp_tools/memory.py` | 217 | FastAPI `/memory/search` docstring 同步 |

**两处实现说明**：
* `apply_action_batch` 接在 **service 层**而不是 MCP 工具层 —— 与 B4 `_audit_heat` / B6 同理，
  MCP / FastAPI / CLI 所有调用者都汇合在这里，接在这儿才不会「多一条上层路径就多一处漏审计」。
* **`_COUNT_NORM` 从 100 改成 10**：100 是按旧语义「被检索次数」设的分母。count 变成
  「被采用次数」后数量级小得多，分母 100 会让这一项永远接近 0（等于白留）。改成 10 后 10 次采用即饱和。

### 7.4 验证脚本改造（5 个）

| 脚本 | 改了什么 |
|---|---|
| `verify-heat-loop.py` | **补自清理**：新增 `before_traces()` + `restore(rows, traces)`，§0 快照全库 `(memory_id, heat, count)` → §5 还原并删掉本脚本新增的审计行，然后断言「全库已恢复成运行前的值」；另加 `adopted` 行为验证 |
| `verify-loop-e2e.py` | 同样补 `snapshot_heat()` / `restore_heat()`；§4 断言改为 `heat>0 且 count==0`；**新增 §5**：显式上报 `adopted` 后 `count+1`、`heat+5.0`，且只影响上报的那一条 |
| `verify-b6.py` | §5 断言改为 `Δsum(count) == 0`；对照组改比 **heat**（原来比 count 会恒假 —— count 现在恒 0） |
| `verify-heat-ranking.py` | ⭐ **修了一个长期误算**：原来用 **sim 差**当 gap 来算「要攒多少次命中才能反超」，但两行的 heat/count 本来就不同、boost 早把差距拉开了 → 严重低估。实测 sim 差 0.0014 的那一行，真实 **score 差是 0.0563**，按 sim 差算出「1 次命中就能反超」，结果排名纹丝不动。改成用 **score 差**选 pair + `hits_needed()` 只推 heat 轴 |
| 其余 5 个 | 未改（与 count 无关） |

### 7.5 验证数字（2026-09-25）

* 全套 **10 个脚本** `ALL CHECKS PASSED`：verify-b6 / verify-decay / verify-reindex-heat /
  verify-ps1-encoding / verify-c1-knowledge / verify-layers / verify-audit /
  verify-heat-loop / verify-heat-ranking / verify-loop-e2e
* `verify-heat-loop` 自清理生效：`Δsum(heat)=0.0`、`Δsum(count)=0`、已删本脚本新增审计 3 行
* `verify-loop-e2e` 真实跑通闭环：写冷记忆 → 小模型提炼 1 条 → 索引 → 召回（`heat 1.0 / count 0`）
  → `adopted`（`heat 6.0 / count 1`）→ 清理 + 还原 0 残留
* `verify-heat-ranking`：选中 score 差 0.0019 的一对，1 次命中把排名从第 5 位抬到第 4 位
* MCP 工具面手测（临时脚本，已删）：13 个工具注册齐全；`memory_search` / `memory_search_simple`
  只加 heat 不改 count；`memory_feedback` 去重后 `applied=1`、幽灵 id 进 `missing`、
  `count+1 / heat+5`；审计出现 `kind="adopted_heat"`；还原 0 残留
* 真库核对：`sum(count)=282` 已回到测试前值（无假 adopted）；`sum(heat)=265.255` 对基线 +7（见 7.6）
* 备份：`v2/data/index/header.db.bak-countsem-20260925-230939`（495616 字节）

**真机验证（2026-09-25 23:36 重启 MCP 后，走真实 MCP stdio 协议）**

* 重启：杀掉 PID 14380（旧代码，21:32 起）→ dsh 自动重连，新进程 **PID 32452**（23:36:33），
  命令与父进程（dsh node）不变
* 用与 dsh 完全相同的启动命令建会话（`python -m services.C2_mcp_server.mcp_server --mcp-stdio`）：
  13 个工具注册齐全，**`memory_feedback` 在列**（= 新代码生效）
* `memory_search` / `memory_search_simple`：`count` 54 → 54 **未变**，`heat` 54.99 → 55.99 **升了**
* `memory_feedback([target, target, 'ghost-id'])`：去重后 `applied=1`、`missing=["ghost-id-000000"]`、
  `count` 54 → 55、`heat` +5.0 ✅
* B7 审计：新增 `kind="adopted_heat"` 1 行，`actor="agent"`，`payload_ref=["6b97f4fecd0f"]`
* 跑完还原：`sum(count)=282`、`sum(heat)=265.255104` 双双回到运行前，已删本次新增审计 5 行（零残留）
* **顺带发现（非 bug）**：MCP 工具返回类型为 `list` 时，SDK 会把它拆成**多个 content 块**
  （每块一条），而不是单个 JSON 数组 —— 消费方要逐块读，别只看 `content[0]`

### 7.6 没做的 / 已知残留（如实记录）

1. **历史 count 没清空**。`sum(count)=282` 全是旧语义（检索交付）留下的脏数据，
   但清空属于动历史数据 → 保守起见**保持原值**
   （对应 `NEXT-下一步推进与架构-20260924.md §5.0` 改动点 #4，标 ❌ 未做）。
2. **heat 有 +7 残留没回滚**。修复前那次 `verify-heat-loop` 运行已经给真库灌进了 heat。
   heat 是设计上会衰减的软计数、且任何真实检索本身就会 +1 → 激进回滚会误改真实值，故不回滚。
3. **`_BETA` 保留 0.1**（没有采纳「置 0」那个方案），所以打分第 2 项仍在、反超上限仍是 0.2；
   但 count 此后只会由「被采用」累积，不会再被「搜到了」灌水。

### 7.7 这一环的「跑通」边界（重要）

| 层 | 状态 | 证据 / 缺什么 |
|---|---|---|
| 代码层（B5 动作 / B4 公式 / MCP 工具） | ✅ **已跑通** | 10 个脚本全绿 + MCP 工具面手测全绿 |
| **线上层（dsh 实际调用）** | ✅ **已生效** | 2026-09-25 23:36 重启 MCP（PID 32452），走真实 stdio 协议验证全绿 |
| **行为层（主 Agent 会不会主动上报）** | ❓ **仍无证据** | 机制是「自觉上报」，没有强制。**只能在接下来的真实对话里观察** |

**结论**：代码层 + 线上层都已跑通并验证。**只剩最后一关**：主 Agent 在真实对话中是否
会主动调 `memory_feedback` —— 这没有自动化验证手段，只能靠观察。
若发现它从不调（count 长期纹丝不动），需要另想办法
（例如把提示推到更显眼的位置，或改成从对话内容自动推断，而不是靠自觉）。
