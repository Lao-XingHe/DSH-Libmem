# 小模型 Agent 作为「前台接待员」—— A/B 对照实验

> 2026-09-23 · 目的：回答「检索链路里要不要加一个小模型 agent 做 query 理解 + 响应精简」

## 1. 背景

当前检索链路（路径 A）：

```
用户 query → B4 双门控 → top-k 摘要 → 主模型综合回答
              ├─ Phase1 SQLite WHERE（task_type / time_bucket / layer / core_entities）
              └─ Phase2 bge-m3 余弦 + heat/count 精排
```

用户提出的想法（路径 B）：

```
用户 query → 小模型抽 filters → B4 双门控 → top-k 摘要 → 小模型精简 → 主模型拿精简版
                                  ↑ 更好的 Phase1 过滤条件        ↑ 更少 token
```

小模型的角色是「前台接待员」：
- **query 理解**：从口语化 query 中抽出 `task_type / core_entities / time_range`，让 Phase1 过滤更准
- **响应精简**：把 B4 返回的 top-k 摘要压缩成更短的结构化简报，省主模型 token
- 主模型只做「综合回答」这一件事，不碰检索和筛选

## 2. 关键认知纠正

**B4 链路本身已经是完整的语义检索器。** bge-m3 是 embedding 模型（不是推理模型），已经在做语义匹配。如果让小模型再做一遍「语义筛选」，就是在已有语义检索之上再叠一层语义检索，收益很小、延迟翻倍。

所以小模型的分工边界应该是：
- **不做检索本身**（B4 已覆盖）
- **只做查询理解**（Phase1 的 filters 现在靠用户侧硬传，没人帮着从 query 里抽）
- **只做响应精简**（B4 返回的是 L1 摘要，可能还可以压短）

## 3. A/B 两条路径的精确定义

### 路径 A：当前（基线）

```python
# 纯代码，不碰任何 LLM
result = retrieve(
    query_text=query,
    # filters 全部 None —— Phase1 退化成全库扫描 + Phase2 bge-m3 精排
    task_types=None, time_buckets=None, layer=None, core_entity_any=None,
    apply_heat=False,   # 对照实验只读，不升温
)
# 返回 items 直接给主模型
```

**特征**：0 次 LLM 调用，延迟 ≈ bge-m3 query 向量 + 候选向量缓存命中后精排 ≈ 100~500ms

### 路径 B：小模型 agent

```python
# Step 1：小模型抽 filters
filters = small_model(query) → {
    "task_type": ["规划"],           # 从 "我想做一个记忆插件" 抽出
    "core_entities": ["记忆", "插件"], # 从 query 里抽实体
    "time_range": None,              # 没提时间就不猜
    "layer": None,
}

# Step 2：带 filters 调 B4
result = retrieve(
    query_text=query,
    task_types=filters.get("task_type"),
    core_entity_any=filters.get("core_entities"),
    # ...
    apply_heat=False,
)

# Step 3：小模型精简响应
compact = small_model_compact(result["items"]) → [
    {"memory_id": "xxx", "bulleted_summary": "- 做了啥\n- 结果如何"},
    ...
]
# compact 给主模型
```

**特征**：2 次小模型调用（抽 filters + 精简响应），延迟 ≈ 小模型 2×(500ms~2s) + B4 ≈ 1.5~5s

## 4. 需要回答的三个问题

| # | 问题 | 怎么量 |
|---|------|--------|
| 1 | 路径 B 的 filters 能不能让 Phase1 更准？ | 对比 A/B 两条路径的 `total_filtered`（Phase1 候选数）和命中率 |
| 2 | 响应精简能省多少主模型 token？ | 对比 A 的 `sum(len(item.summary))` 和 B 的精简版 token 数 |
| 3 | 小模型两次调用的延迟，换回来的命中率提升值不值？ | 延迟差 vs 命中率差 vs token 省 |

## 5. 测试用 query（ground truth 已知）

这些是之前跑过的、我们知道正确答案是什么的 query：

| # | Query | 期望命中 | 期望 task_type | 期望 entities |
|---|-------|----------|----------------|---------------|
| 1 | `九出十二归 主角` | 《九出十二归》小说相关记忆 | ["其他"] | ["九出十二归", "主角", "黄有财"] |
| 2 | `星海旅人的独白` | 插件安装相关记忆（test 数据） | ["其他"] | ["星海旅人", "独白"] |
| 3 | `记忆插件怎么做` | B 4 双门控 / 记忆检索管道 | ["规划"] | ["记忆", "插件", "检索"] |
| 4 | `工作流 先计划再动手` | 工作方法相关记忆 | ["决策"] | ["计划", "动手", "工作流"] |
| 5 | `heat count 热度权重` | retrieval.py 的 alpha/beta 调整 | ["决策"] | ["heat", "count", "热度", "权重"] |
| 6 | `小模型 结构化 query` | 关于小模型 agent 的讨论 | ["研究"] | ["小模型", "结构化", "query", "agent"] |
| 7 | `冷层 温层 重复` | 坑 B 冷/温重复合并 | ["决策"] | ["冷层", "温层", "重复"] |
| 8 | `content_path L3` | 坑 C 补 content_path | ["决策"] | ["content_path", "L3"] |

## 6. 判定规则

跑完对照后，**路径 B 要接进主链路，必须同时满足**：

1. **命中率提升 ≥ 20%**（或路径 A 完全 miss、路径 B 能命中）
2. **主模型 token 消耗降 ≥ 30%**
3. **总延迟 ≤ 2× 路径 A**（多一次小模型调用不应慢过 2 倍）

如果三个条件不能同时满足 → 路径 B 降级为「低置信度 fallback」：只有当路径 A 的 top1 sim < 0.35 或 Phase1 候选 > 500 时才触发小模型改写。

## 7. 不在本次实验范围

- 小模型做检索本身（B4 已覆盖，重复劳动）
- 主模型填写数据库表格并检查错误（主模型做校验容易幻觉，用确定性规则更稳）
- Ollama vs llama.cpp vs 其他后端的对比（已有结论：llama.cpp 快 30 倍）

---

## 8. Benchmark 结果（2026-09-23）

**决定性结论：路径 B 全线失败，不接进主链路。**

### 8.1 原始数据

| # | Query | 路径 A 延迟 | 路径 A 命中 | 路径 B 延迟 | 路径 B 命中 | 延迟比 |
|---|-------|------------|------------|------------|------------|--------|
| 1 | 九出十二归 主角 | 42ms | ✅ 5/5 | 32,344ms | ❌ 0/5 | **774×** |
| 2 | 星海旅人的独白 | 27ms | ✅ 1/5 | 8,521ms | ❌ 0/5 | **319×** |
| 3 | 记忆插件怎么做 | 20ms | ✅ 5/5 | 3,268ms | ❌ 0/5 | **168×** |
| 4 | 工作流 先计划再动手 | 21ms | ✅ 2/5 | 10,999ms | ❌ 0/5 | **521×** |
| 5 | heat count 热度权重 | 20ms | ✅ 4/5 | 16,969ms | ❌ 0/5 | **853×** |
| 6 | 小模型 结构化 query | 23ms | ✅ 5/5 | 33,804ms | ❌ 0/5 | **1476×** |
| 7 | 冷层 温层 重复 | 21ms | ✅ 5/5 | 1,288ms | ❌ 0/5 | **63×** |
| 8 | content_path L3 | 19ms | ✅ 1/5 | 808ms | ❌ 0/5 | **44×** |
| **平均** | | **23ms** | **8/8 (100%)** | **13,500ms** | **0/8 (0%)** | **527×** |

路径 A 的 Phase1 候选全是 432（全库），路径 B 的 Phase1 候选全是 **0**。

### 8.2 根因分析

**路径 B 完全 miss 有两层原因**：

#### 原因 1：小模型抽出的 task_type 与入库标注口径不一致

B2 提炼时给小说类内容标了 `task_type="其他"`，但小模型抽 filters 时把 "九出十二归 主角" 判成 `task_type="研究"`。`phase1_filter` 的 SQL 是 **硬 WHERE `task_type IN ('研究')`**——库里根本没有 task_type='研究' 且含 '九出十二归' 实体的行，所以 Phase1 直接归零。

类似地，"冷层 温层 重复" 小模型判成 `task_type="验证"`，"记忆插件怎么做" 判成 `task_type="求助"`——**每一个都错**。

#### 原因 2：小模型还会瞎猜 layer

"heat count 热度权重" 小模型不仅抽了 `task_type="研究"`，还加了 `layer="hot"`。SQL 里 `layer='hot'` 又砍掉一批。双重硬过滤叠加 → Phase1 归零。

#### 原因 3：延迟硬伤

小模型（Qwen3.5-4B-Q4_K_M）走 llama_server 每次调用 **800ms ~ 33s**，且极不稳定（同一类型 query 有时 1.3s 有时 33s）。路径 A 只要 20ms，路径 B 最慢的一条是 A 的 **1476 倍**。

### 8.3 结构性结论

**Phase1 的 filters 是硬过滤**——猜错一个字段 = Phase1 归零 = 整条链路白跑。小模型做 query 结构化的输出无法保证与 B2 入库时的标注口径一致（B2 自己还在用 `_clean_entities` 清理实体碎片），这个口径差是**结构性问题**，调 prompt 解决不了。

**路径 A 为什么全命中**：因为路径 A 不传任何 filters，Phase1 退化成全库 432 条 → Phase2 bge-m3 余弦精排。bge-m3 是 embedding 模型，做的是**字面+语义相似度匹配**，根本不依赖 task_type/entities 这些结构化字段——它看的是 query 和 candidate summary 的语义向量距离。所以入库标注口径不一致对它毫无影响。

### 8.4 建议

1. **不接进主链路**。路径 B 在当前架构下不仅没有收益，反而有害（延迟爆炸 + 命中率归零）。

2. **唯一合理的小模型角色：低置信度 fallback**。只有当路径 A 的 **top1 sim < 0.35**（说明 bge-m3 也没捞到）或 **Phase1 候选 > 500 且精排后 sim 全低** 时，才触发小模型做 **query 改写**（不是抽 filters），改写后重新走路径 A。这是 fallback，不是必经路径。

3. **"前台接待员"这个角色名存实亡**：你设想的"小模型接待 → 结构化反馈 → 主模型填写表格"里，主模型填写表格这件事可以保留（用 B6 MAPValidator 的确定性规则校验，不是让主模型自由判断），但小模型接待这一步目前**没有可落地的接口**——它唯一能调的就是 `retrieve()`，而调用它之前它又不知道要传什么 filters，所以它能做的只剩"query 改写"和"响应精简"。

4. **响应精简也不划算**：路径 A 返回的 items 总字符数只有 50~100 字（L1 摘要），主模型 token 消耗已经很低。小模型精简一次要 1~2s（还可能抽 0 结果），省不下多少。
