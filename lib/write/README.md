# lib/write —— B1 冷库写入侧（dsh-cold-memory-watcher 的副本）

## 这是什么

`dsh-libmem` 的**写入侧子模块**：监听 DSH 会话事件 + 轮询磁盘会话日志，把
每一轮真人↔助手对话落进书房 OS 的 B1 冷库（`data/cold_vault/YYYY-MM-DD.jsonl`）。

## 它是副本，不是新代码

6 个文件全部来自 `<UPSTREAM_DIR>\lib\`（2026-09-30 版，
当时的 `verify` 套件四支全绿）。**除下面 4 处字面量外，代码逐字节相同**：

| 文件 | 行 | 改动 | 原因 |
|---|---|---|---|
| `index.js` | 文件头 | 补了一段说明 + `@module dsh-libmem/write` | 说明这是副本 |
| `index.js` | `export const name` | `cold-memory-watcher` → `libmem-write` | 状态文件里的 `plugin` 字段 |
| `index.js` | `DEFAULT_STATUS` / `DEFAULT_POLL_STATE` | `'..'` → `'..', '..'` | 文件现在在 `lib/write/`，状态文件要落在包根 |
| `index.js` | `log()` 前缀 | `[cold-memory-watcher]` → `[libmem]` | 日志前缀统一 |

> ★ **2026-10-12：`vault.js` 也改了**（不再是"完全未改"）—— 补**第二道跨进程去重**。
> 两处副本（本目录与 `dsh-cold-memory-watcher/lib/vault.js`）**同步改、保持逐字节一致**，
> 因为漏洞在两边都存在：进程内 `known` 集合挡不住"另一个实例在它建立之前就读过了"。
> 实测证据：同一对 `user+assistant` **逐字节相同**、`id` 不同、**时间差 8 小时**
> （`123242750001` / `204308779002`）。判据与实测见 `dsh-libmem/README.md` §8 第三批的 P0。

`pairing.js` / `poller.js` / `session-log.js` / `zstd-frames.js`
**完全未改**。

## 为什么不直接 `import` 原包

`dsh-libmem` 的定位是「读写一体的那一个记忆插件」。如果靠绝对路径去 import
另一个插件目录里的内部文件，那么「哪天把旧目录删了/挪了」就变成一次**静默**
故障 —— 正好是这个仓反复踩的那类坑（路径解析错了不报错，只是什么都记不下）。
副本 + 上面这张对照表 + `bin/verify-libmem.mjs` 的字节比对，是更笨但可验证的做法。

## 上游更新了怎么办

```powershell
# 看差异
fc.exe /L "<UPSTREAM_DIR>\lib\poller.js" "<DSH_WORKSPACE>\dsh-libmem\lib\write\poller.js"

# 确认上游改动是有意为之之后：重新复制 5 个未改动文件 + 按上表重做 index.js 的 4 处改动
```

`bin/verify-libmem.mjs --check-copy` 会逐字节比对这 6 个文件与上游，
并列出上面这 4 处预期差异之外的一切偏差 —— 上游漂了会当场失败。
