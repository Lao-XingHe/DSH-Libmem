# Phase 2 验收报告 · B6 Validator + 热度衰减 + 6 Bug 修复

> 交接来源：`D:\Deepseek Harness\NEXT-待办-B6验证器与热度衰减.md`（2026-09-22 完成报告）
> 复现日期：2026-09-23
> 验收标准：三个 verify 脚本 **ALL CHECKS PASSED**，且全程隔离临时库 + 自清理（真库前后不变）

---

## 一、执行摘要

| 脚本 | 断言数 | 结果 | 真库触碰 |
|------|--------|------|----------|
| verify-b6.py | 64 | ✅ ALL CHECKS PASSED | 自动还原 heat/count，零残留 |
| verify-decay.py | 54 | ✅ ALL CHECKS PASSED | SHUFANG_DATA_DIR 重定向到临时目录，真库 sum(heat) 前后不变 |
| verify-reindex-heat.py | 14 | ✅ ALL CHECKS PASSED | 同上，真库 sum(heat) 前后不变 |
| **合计** | **132** | **全绿** | **零触碰** |

---

## 二、verify-b6.py 实跑摘录（64 PASS）

### 正常 query → 合法 MemoryResponse
```
items=3 empty=False degraded=False tokens=50 trace='6cb7216e3b38429f'
PASS 字段齐全  (missing=[])
PASS 响应能被 validator 反验通过
PASS trace_id 查到审计（search + write 同一 trace）
```

### 空结果 → 诚实返回（硬规则 2）
```
items=0 empty=True hint='本次未命中任何记忆（Phase1 候选 0 条…），未做任何补全。'
PASS 没有编造条目
PASS followup_hint 说清了「筛完真没有」
```

### 指代不明 → needs_clarification（硬规则 1）
```
detect_ambiguity('那个东西呢') = True  (指代词 ['那个'] 的指代对象不明)
PASS 完整 query 不被误判（旧实现 "这个 bug 怎么修" 会误报）
PASS 反问响应合法
```

### ⭐ 重试不会导致 heat 重复增长（5 PASS）
```
注入一次非法响应 → handler 恰好被调 2 次
Δsum(count) == 交付条目数 (Δcount=3, applied=3)
Δsum(heat) == 交付条目数 × 1.0 (Δheat=3.0)
对照组（裸调两次 raw）：Δcount=6 → 重复升温
```

### 硬规则 3 越权拒绝 + 零副作用
```
decided_by != 'user' → degraded=True + 原因 + 零升温
```

### confidence ∈ [0,1] 回归
```
构造 _score=1.43/heat=60/count=120 → confidence=0.93（被夹到 [0,1]）
header 里保留排名分 score=1.43 供排障
```

### memory_drilldown 三包之一（真能取到原文）
```
目标 91478f1febc2 → 命中 2026-04-27.jsonl 中对应行（逐行 hash 精确命中）
L2 located=True snippet='## 需求档案：并行办公文件AI处理助手...'
L3 located=True keys=['assistant','id','time','user']
不存在的 memory_id → 诚实报错，不编造内容
```

---

## 三、verify-decay.py 实跑摘录（54 PASS）

### 半衰期数学（λ=ln2/30d）
| 场景 | 输入 | 衰减后 | 理论值 | PASS |
|------|------|--------|--------|------|
| 30 天前 | heat=10 | 5.0000 | ≈5.0 | ✅ |
| 昨天 | heat=10 | 9.7716 | ≈9.77 | ✅ |
| 90 天前 | heat=10 | 1.2500 | ≈1.25 | ✅ |
| 1000 天前 | heat=8 | 0.000000 | ≈0 | ✅ |
| heat=0 | 0 | 0.0 | 0 | ✅ |

### 幂等：连跑两次不重复衰减
```
t30 第一次: 10 → 5.0000
t30 第二次: 5.0000 → 5.0000  （若实现不刷新 updated_at，这里会变成 2.5）
```

### 四象限 + 层迁移
```
高/低 count × 高/低 heat → 四格全对
migrate_layers(heat>0 前置) → 冷层非空（老逻辑全判 hot）
热=0 的行被判为 cold（不是 warm/hot）
```

### 调度接线三件套
```
decay_memory.py → sqlite3 在线备份（走 WAL）→ decay-run.ps1 → wscript.exe run-hidden.vbs
计划任务 MemoryDecay 已注册，StartWhenAvailable=true，每天 18:00
```

---

## 四、verify-reindex-heat.py 实跑摘录（14 PASS）

### ⭐ 核心 bug：重跑温层索引 = 全库累积热度清零（已修）
```
首次 index_warm: rows=36, sum_heat=0, sum_count=0
模拟检索堆高:   sum_heat=324.0, sum_count=1512
再次 index_warm: sum_heat=324.0, sum_count=1512  ← UPSERT 保留运行时状态！

抽查行: heat=9.0 count=42 layer=hot sovereignty=user updated_at=2020-01-01 全保留
同时 summary 被温层新值覆盖（不是「永不更新」）
```

---

## 五、真库隔离证明

三个 verify 脚本全程在临时库跑（`SHUFANG_DATA_DIR=C:\Users\admin\AppData\Local\Temp\*`），真库：
- **verify-b6**：自清理代码还原 heat/count 到运行前（sum(heat) 前后相等）
- **verify-decay / verify-reindex-heat**：SHUFANG_DATA_DIR 重定向，真库全程旁观

真库状态（运行后）：
```
sum(heat) = 85.400466   （与 Phase 2 前一次调用后的值相等）
rows = 771
```

---

## 六、6 个实跑 Bug 修复清单

| # | Bug | 根因 | 修复 |
|---|-----|------|------|
| 3.1 | confidence 会 >1 → 整条检索降级 | _score 理论上限 1.5，旧代码把它当 confidence 塞进响应，validator 判非法 | confidence 用相似度 _sim（夹 [0,1]），排名分另放 header.score |
| 3.2 | memory_drilldown 从未成功返回 L2/L3 | content_path 指向整天 JSONL，旧代码 json.load() 当单个 JSON 读 → Extra data | memory_id = md5(f"{日期}#{原始id}")[:12] → 逐行重算 hash 精确命中 |
| 3.3 | 硬规则 1 「见代词就反问」把可用检索废掉 | `"这个 bug 怎么修"` 含「这个」被误报 | 改成 query<3字 或 去指代词后实义<5字 才算指代不明 |
| 3.4 | migrate_layers() 把 0 热度判成 warm/hot | 老逻辑 h>=0 恒真 → 冷层恒空 | 层判定加 heat>0 前置；update_layer(touch=True) 默认，迁移传 False |
| 3.5 | ⭐ 重跑温层索引 = 全库热度清零 | bulk_insert 用 INSERT OR REPLACE（整行替换），温层文件不带 heat | 改 UPSERT ON CONFLICT DO UPDATE，冲突时只更新内容字段，heat/count/updated_at/layer/sovereignty 保留 |
| 3.6 | 备份带 -wal/-shm → 回滚被旧 WAL 重放 | sqlite3.backup() 把 WAL journal_mode 带进备份 | 备份完把目标库切回 journal_mode=DELETE（单文件自包含） |
