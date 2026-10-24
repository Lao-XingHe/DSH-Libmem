# dsh-libmem

**一句话**：基于图书馆系统 LIS 做的长期记忆系统插件：自动回忆、语义检索、对话落盘。

**为什么是 LIS**：整套东西按图书馆学的分工搭 —— 编目（记忆条目 + 表头字段）、检索（四路召回 + 重排）、
参考咨询（拒答与反问话术）、剔旧与热度（衰减）；对应到代码就是 B1 冷库 → B2 提炼 → B3 表头引擎 →
B4 双门检索 → B5 热度 → B6 协议 → B7 审计。

**细一点**：它是书房 OS v2 记忆插件（B1~B7 冷库/提炼/热度/审计 + C1~C3）的 DSH 原生形态 ——
自己托管 Python 内核、注册 15 个 `libmem_*` 原生工具、按对话自动回忆、承担冷库写入侧，
并在界面右下角挂一个「记忆」面板。

> 这是一个**标准 DSH bundle**（`package.json` 里声明 `dsh.bundle.patch` → 本目录的 `cordis.patch.yml`），
> 可以像别的插件一样用 `dsh plugin add` 安装、在插件管理页里启用/停用/卸载。

---

## 0.5 打包避坑：写了 `exports` 就必须导出 `./package.json`

插件管理页那一行的**简介 / 标题 / 图标**，宿主是用 `readPluginMeta()` 读的 —— 它解析的是
**`<包名>/package.json`** 这个**子路径**（`optionalResourcePath('dsh-libmem/package.json')`，
走 Node 真模块解析器）。而 Node 的规则是：**一旦 `package.json` 里写了 `exports`，
没列进去的子路径一律拒绝解析**。

后果很隐蔽：`ERR_PACKAGE_PATH_NOT_EXPORTED` 被静默吞掉 → 读不到 manifest →
**插件页那一行只剩名字，简介（连标题、图标）全空**，可插件本身跑得好好的、工具也都正常。

所以本包 `exports` 里必须有这一条（DSH 官方包也都写着它）：

```json
"./package.json": "./package.json"
```

（对照：`dsh-whale-widget` **没有** `exports` 字段，所以它天然读得到 —— 不是它做了什么特别的事。）

## 0.6 界面面板（右下角「记忆」按钮）

点开能看到：服务状态（在线/模式/PID/版本）、模型服务（嵌入与重排真探活 + 耗时）、
自动回忆（**四档切换** + 判断/翻/点名/注入/**采用**计数 + 最近判断与最近采用）、
写入侧（已写轮数 + 最近落盘）；
还能**试搜一句**、触发**温提炼**、**重建知识库索引**、**切换自动回忆档位**、**开关记忆纪律**、
以及 ★ 2026-10-16 新增的**「卸载模型」+「需要时自动拉起」**那一对（见 §4.1）。

它走的是**与页面同源的相对接口** `/libmem/*`（不碰跨域），带一道轻栅栏：
Host 必须是回环、`Sec-Fetch-Site: cross-site` 一律拒、带 `Origin` 时必须同源或是 `dsh-app://`（桌面壳）。

⚠️ 桌面端的界面注入表是**宿主启动时一次性收集**的，所以**面板要重启 DSH 才会出现**；
而且必须用**内联 script 行**（`script-src` 那条一旦加载失败会把整个界面 boot 弄挂）
—— 这两条都是小鲸鱼踩出来的，本插件照抄了它的做法（见 `lib/panel.js` 文件头）。

---

## 0. 装给别人用：先看这一节（重要）

**这个包只是「插件的壳」，不含记忆内核。** 能跑的前提是对方机器上已经有：

| 需要什么 | 说明 |
|---|---|
| **书房 OS v2 内核** | 一个目录，含 `config.yaml` / `services/` / `mcp_tools/` / `data/` —— 就是 `projectRoot` 指向的东西。可发布形态见本仓 `study/v1.3.4/`（Gitee 快照） |
| Python 3.12 + 几个包 | `fastapi` `mcp` `pyyaml` `jieba` `whoosh` `requests`（内核需要；插件只要求"这个解释器能起内核"） |
| 两个模型服务 | 嵌入 bge-m3 @`127.0.0.1:18099`、重排 bge-reranker-v2-m3 @`127.0.0.1:18098`。**没起也能用**，但检索退化成噪声分（护栏保证不返回空） |
| **许可证** | 内核是 **PolyForm Noncommercial 1.0.0**：自用/学习/研究免费，**公司或商用需授权**（见 `study/LICENSE`、`study/LICENSING.md`） |

### 装法

```powershell
# 1)（推荐）先把两个共享依赖链上 —— 离线、不联网、不改 profile
cd <插件目录>
node bin/setup-links.mjs
#    想改用 npm 的真依赖也行：pnpm install

# 2) 装进你的 profile（desktop 换成你实际用的 profile 名）
dsh plugin --profile desktop add link:<插件目录>
#    例：dsh plugin --profile desktop add link:D:\Plugins\dsh-libmem
#    也可以 file:<路径>（拷贝安装）或发布到 npm 后直接写包名

# 3) 重启 DSH
```

> `link:` 是软链安装：源目录改了立即生效，但**装完不能移动/改名该目录**（移动后要重新 `add`）。
> 装完插件会出现在**插件管理页**，之后可在页面里启用 / 停用 / 卸载。

### 装完必做：给两个路径

不改这两个，服务起不来（`libmem_status` 会明确报 `projectRoot 未配置`，或给启动失败的日志尾部）。
**推荐**在自己的 profile patch（`<DSH_HOME>\profiles\<profile>\cordis.patch.yml`）里**按 id 覆盖**
（插件管理页装完后会自动追加一行 `- id: libmem`，把 config 写进去即可）：

```yaml
- id: libmem
  disabled: false
  config:
    python: 'C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe'
    projectRoot: 'D:\Deepseek Harness\dsh-libmem\v2'      # 必须含 config.yaml
    write:
      enabled: true
      projectRoot: 'D:\Deepseek Harness\dsh-libmem\v2'
      poll: true
```

也可以直接改插件目录里的 `cordis.patch.yml`（简单，但升级插件时会被新版覆盖）。

> ⚠️ **不要再在 profile patch 里写 `insert: id: libmem`** —— 同一个 id 出现在两层，
> loader 会直接抛 `duplicate loader entry id`（`cordis-plugin-loader` index.js:91），
> 插件树加载失败。row 由本包的 `cordis.patch.yml` 提供，profile 只负责覆盖配置。

---

```
DSH 会话
   │  ① 写入侧（本插件，lib/write/）—— 事件监听 + 文件轮询
   ▼
B1 冷库 data/cold_vault/<date>.jsonl
   │  ② 服务内轮询（bridgePoller）→ B2 温提炼 → B3 header.db + 原文索引
   ▼
B3/B4 检索（本插件 → HTTP → Python 内核）
   │  ③ 工具面 libmem_*（15 个）
   ▼
模型（libmem_search / libmem_drilldown / libmem_feedback …）
```

---

## 1. 它替掉了什么

| 之前 | 现在 |
|---|---|
| `mcp-xilian`：`@deepseek-ai/dsh-mcp-client` 用 **MCP stdio** 拉 Python 子进程，工具叫 `mcp__xilian__*` | 本插件自己托管 HTTP 服务，工具叫 **`libmem_*`** |
| `cold-memory-watcher`：**另一个**插件负责写入侧，自己解析一遍 `projectRoot` / `vault` | 写入侧并进**同一个包**（`lib/write/`），读写共用一份根解析 |
| 服务起不来 → 只能翻 dsh 的 stderr | `libmem_status` + `status.json` + `libmem-bridge.log` 三处可查 |

**为什么必须离开 MCP stdio**：dsh 的受限沙箱**禁止以管道方式 spawn 子进程**
（`EPERM`；只有 `inherit`/`ignore` 允许）。这正是写入侧当初被迫在 Node 里重写
一遍 B1 落盘格式的原因。HTTP 不受这条限制 —— 插件用 `stdio: 'ignore'` 加两个
**文件描述符**收日志起服务，然后走 `127.0.0.1` 调工具。

---

## 2. 目录

```
dsh-libmem/
├── lib/
│   ├── index.js      插件入口：name / inject / Config / apply（先读写侧，再后台起服务，再注册工具）
│   ├── config.js     配置 schema（schemastery）+ 默认值
│   ├── service.js    服务托管：起 / 探活 / 退避重启 / 停 / 状态落盘 / 日志尾部回读
│   ├── client.js     HTTP 客户端（fetch + 超时 + 中断 + 错误分类）
│   ├── tools.js      15 个工具的定义（薄壳，全部转发给 Python 侧同名工具）
│   └── write/        B1 写入侧（dsh-cold-memory-watcher 的副本，见该目录 README.md）
├── bin/verify-libmem.mjs   自检（副本比对 + schema 编译 + 端到端调用 + 卸载）
├── node_modules/@deepseek-ai/{schemastery,dsh-tools}   junction 到 profile 的共享 node_modules
├── status.json       服务状态心跳（运行时生成）
├── watch-status.json 写入侧状态心跳（运行时生成）
└── libmem-bridge.log 服务日志（stdout + stderr 都进这里）
```

Python 侧只新增两个文件（都在 `dsh-libmem/v2/services/C2_mcp_server/`）：

| 文件 | 作用 |
|---|---|
| `libmem_tools.py` | **13 个工具实现的单一真源**。`mcp_server.py` 的 `@mcp.tool()` 与 HTTP 桥共用它，行为不会分叉 |
| `libmem_bridge.py` | HTTP 桥：`GET /health`、`GET /health/models`（真调一次嵌入/重排）、`GET /tools`、`POST /tool`。**标准库 `http.server`**，零依赖 |

> 为什么桥不用 FastAPI：实测（2026-10-10，Python 3.12.9）本机 fastapi 与 starlette
> 版本不匹配，`FastAPI(...)` 直接抛 `TypeError: Router.__init__() got an unexpected
> keyword argument 'on_startup'` —— 也就是说 `mcp_server.py` 的 FastAPI（:8766）模式
> **本来就起不来**。桥只有三个路由，标准库足够，还顺带躲开了这颗地雷。

---

## 3. 挂载（profile patch）

```yaml
- insert:
    - id: libmem
      name: 'D:\Deepseek Harness\dsh-libmem\lib\index.js'
      config:
        python: 'C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe'
        projectRoot: 'D:\Deepseek Harness\dsh-libmem\v2'
        port: 8768
        bridgePoller: true          # ★ 必须 true，否则温提炼不再触发（不报错，只是搜不到新记忆）
        toolPrefix: 'libmem'
        write:
          enabled: true
          projectRoot: 'D:\Deepseek Harness\dsh-libmem\v2'
          statusFile:  'D:\Deepseek Harness\dsh-libmem\watch-status.json'
          pollStateFile: 'D:\Deepseek Harness\dsh-libmem\poll-state.json'
```

实际生效的两份：`C:\Users\admin\.dsh\profiles\desktop\cordis.patch.yml`（桌面端在读）
与 `...\web\cordis.patch.yml`（CLI `dsh web` 用）。两份都保留了下线旧挂载的注释块，回滚
就是把注释去掉、把 `libmem` 行删掉 —— **不要同时开着**，两边都提供记忆工具面。

⚠️ 两个 profile 不要同时跑：共用 `DSH_HOME`，写入侧会重复轮询同一批会话。

---

## 4. 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 整插件开关 |
| `python` | Python 3.12 绝对路径 | **不要写裸 `python`**：PATH 里别的解释器排前面会让服务起不来 |
| `projectRoot` | `D:\Deepseek Harness\dsh-libmem\v2` | 服务 cwd，必须含 `config.yaml`（路径解析的根） |
| `host` / `port` | `127.0.0.1` / `8768` | 桥的监听地址 |
| `manageProcess` | `true` | `false` = 只连已有服务，绝不自己起 |
| `reuseExisting` | `true` | 端口上已有健康的 LibMem 服务就复用（按 `/health` 的 `service` 字段认身份） |
| `startTimeoutMs` | `90000` | 等服务就绪的上限 |
| `toolCallTimeoutMs` | `120000` | 单次工具调用上限（首次检索含 jieba / bge 预热） |
| `bridgePoller` | `true` | 桥是否起冷库轮询（B1 → B2 温提炼） |
| `logFile` / `statusFile` | 包根下 | 服务日志 / 状态心跳 |
| `maxRestarts` / `restartDelayMs` | `5` / `3000` | 服务意外退出后的退避重启策略 |
| `registerTools` / `toolPrefix` | `true` / `libmem` | 工具面开关与前缀 |
| `panel` | `true` | 右下角「记忆」面板（`/libmem/*` 同源接口） |
| `recall.mode` | `smart` | **四档判断**：`off` / `strict` / `smart` / `eager`（旧值 `always` ≡ `eager`；未知值回落 `smart`） |
| `recall.discipline` | `true` | 是否注入「记忆纪律」段（要求模型读块、引用出处、用了就回报 `libmem_feedback`） |
| `recall.maxItems` / `maxChars` | `5` / `1200` | 一次最多贴几条 / 正文上限 |
| `recall.minChars` | `6` | 比这更短的话不值得翻（"好的"）—— ⚠️ **点名不受它限制**，见 §8 |
| `recall.freshMs` | `180000` | 缓存多久算新鲜；过期不再注入 |
| `recall.coldStart` / `coldStartItems` / `coldStartDays` | `true` / `6` / `14` | 会话开局贴一段「最近在做什么」 |
| `write.*` | 见 patch | 写入侧（B1）：`enabled` / `vault` / `projectRoot` / `poll` / `pollIntervalMs` / `statusFile` / `pollStateFile` |
| `models.*` | 见 §4.1 | 本地模型控制：`autoload` / `stateFile` / `routerUrl` / `routerModel` / `embedBase·Port·StartScript` / `rerankBase·Port·StartScript` / `startTimeoutMs` / `probeTimeoutMs` |

### 4.1 本地模型控制（★ 2026-10-16）

面板上那两个控件是一对：**「卸载模型」现在就把显存还回来，「需要时自动拉起」决定它回不回来。**

三个本地模型都吃显存（RTX 3060 12GB，逐进程实测）：

| 模型 | 端点 | 用途 | 加载态占用 |
|---|---|---|---|
| `Qwen3.5-4B-Q4_K_M` | llama.cpp 路由 `127.0.0.1:8089` | B2 温提炼（`.small`） | 1.9 ~ 4.0 GB |
| `bge-m3-F16` | `127.0.0.1:18099`（独立进程） | 检索嵌入（1024 维） | ~730 MB |
| `bge-reranker-v2-m3` | `127.0.0.1:18098`（独立进程） | 检索重排 | ~460 MB |

**卸载怎么做的**：4B 走路由原生的 `POST /models/unload {"model":…}`（返回 `{"success":true}`）；
两个 bge 是独立进程、llama.cpp 单实例没有卸载 API，只能**按端口找进程再结束**
（必须按端口 —— 路由的子实例也叫 `llama-server.exe`，按进程名杀会把 4B 一起带走）。
判断一律以**重新探测**为准，"命令发出去了"不算数。睡着的进程壳（各约 115 MB）也一并清掉 ——
按钮的语义是"现在就把显存还回来"，代价是下次重新加载（实测 bge 约 4s）。

**自动拉起钩子在哪**：`lib/service.js` 的 `call()` 在每次转发前调 `ModelControl.ensureReady()`，
但**只对 `RETRIEVAL_TOOLS` 生效**（`memory_search` / `memory_search_simple` /
`knowledge_search` / `memory_extract`）—— 写记忆、看统计、审计不碰向量，不该因为一次写入就把 1.2 GB 拉回显存。
钩子**永不让调用失败**：拉不起来只是检索质量降级（本来就有 fail-open 护栏），
而"因为拉模型失败所以连记忆都读不到"不可接受。

> ★ 2026-10-24：`memory_timeline` 与 `memory_drilldown` 已移出这个集合 ——
> 前者走 `recent_retrieve()`（窗口内 + 时间倒序，完全不看相关性）、后者按 id 取原文，
> **两者都不用向量**。留着等于"问一句『这几天干了什么』就白拉起两个 bge"。

**★ 2026-10-24：4B 改成「提炼批次结束即卸载」**（原来只靠 `sleep-idle-seconds 300` 自己睡）。

- **位置**：`v2/mcp_tools/memory.py` 的 `_run_extract_background()` 的 `finally` 里，
  调 `_unload_small_model_after_extract()` → `POST /models/unload`（与面板按钮同一接口）。
- **为什么只有 4B 值得这么做**：它只服务 B2 温提炼，不在检索/对话链路上 —— 下次冷加载
  多 ~8s **没人感知**；而原先"醒着 = 批处理时长 + 5 分钟尾巴"，活跃期轮询每 10 分钟就可能
  触发一次提炼，等于加载态（~3.9 GB）占着的时间远多于真正干活的时间。
  两个 bge 则相反：它们在每次自动回忆的必经之路上，**用完就杀 = 每轮 +6s**，不划算。
- **开关**：`config.yaml` 的 `llm.small.unload_after_extract`（默认 `true`，改 `false` 回旧行为）。
- **同时删掉的**：`v2/mcp_tools/system.py` 里启动时的 `_warmup_small_model()`
  （原先每次桥启动都发 1-token 请求把 4B 拉进显存；冷加载 7~9.4s、3.9 GB，而提炼可能根本
  不发生）。router 本来就会在第一次真正需要时自动加载。

⚠️ **两个只有实测才会知道的坑**（都写进代码注释了）：

1. **不要用 `detached: true` 去跑那两个 ps1**。Windows 上它会带 `DETACHED_PROCESS`，
   powershell 没有控制台，脚本里的 `Start-Process -WindowStyle Hidden -Redirect*` 直接失败，
   **一个 llama-server 都没起来而脚本 exit 0**（静默失败）。实测对照：`detached:true` 40s 起不来 ❌ /
   `detached:false` 4s 起来 ✅。真正的 llama-server 是脚本里 `Start-Process` 起的独立进程，
   powershell 只是等就绪的包装器，不 detached 也安全。
2. **开关是持久的**（落 `models.stateFile`，默认包根 `model-autoload.json`），
   不像自动回忆档位那样只是内存态 —— 否则重启后"我明明关过自动拉起"会自己变回来。

**嵌套默认值的坑（已修）**：`write` 是嵌套 schema，校验器会把**未提供**的字段填成
`WriteConfig` 自己的默认值 —— 那些是空串，会覆盖掉插件里配好的真实路径。空串在写入侧
的含义是「自己去猜」，而会话 cwd 是 `D:\Deepseek Harness`（那里没有 `config.yaml`），
猜不出来就**一条也不记**。所以 `lib/index.js` 对 `projectRoot` / `statusFile` /
`pollStateFile` 三个字段显式兜底；`vault` 与 `pollRoot` 留空是**有意的**。

---

## 5. 工具面（15 个）

| 工具 | Python 侧 | 类型 |
|---|---|---|
| `libmem_search` | `memory_search` | 读 |
| `libmem_search_simple` | `memory_search_simple` | 读（绕过 MAP validator） |
| `libmem_drilldown` | `memory_drilldown` | 读 |
| `libmem_feedback` | `memory_feedback` | **写**（count+1 / heat+5 / B7 审计） |
| `libmem_write` | `memory_write` | **写**（补记用） |
| `libmem_extract` | `memory_extract` | **写**（全量温提炼） |
| `libmem_recent` | `memory_timeline` | 读（★ **按时间列清单**，不看相关性；见 §8 第二批） |
| `libmem_stats` | `memory_stats` | 读 |
| `libmem_knowledge_search` | `knowledge_search` | 读 |
| `libmem_knowledge_reindex` | `knowledge_reindex` | **写** |
| `libmem_knowledge_stats` | `knowledge_stats` | 读 |
| `libmem_knowledge_read` | `knowledge_read` | 读 |
| `libmem_audit_recent` | `audit_recent` | 读 |
| `libmem_audit_verify` | `audit_verify` | 读 |
| `libmem_status` | —（本地） | 读（插件自检） |

返回值统一是**字符串**：输出会按 `output.schema` 做校验，字符串是唯一不会因为后端
把 dict 换成 list 就炸的类型；旧 MCP 路径给模型的本来也是 JSON 文本，模型看到的东西没变。

---

## 6. 运维

```powershell
# 自检（副本比对 + schema 编译 + 真起一个桥 + 逐个调工具 + 卸载）
node D:\Deepseek Harness\dsh-libmem\bin\verify-libmem.mjs            # 自起桥，端口 8770
node D:\Deepseek Harness\dsh-libmem\bin\verify-libmem.mjs --check-copy   # 只比对写入侧副本
node D:\Deepseek Harness\dsh-libmem\bin\verify-libmem.mjs --port 8768 --reuse   # 只连线上服务

# 模型服务真探活（嵌入 / 重排，**不是**探端口）
Invoke-RestMethod http://127.0.0.1:8768/health/models | ConvertTo-Json -Depth 5

# 检索阈值护栏的自检（造"所有候选都低于阈值"的场景）
python D:\Deepseek Harness\scratch\_verify_threshold_fallback.py
```

排障顺序：

1. **`libmem_status`**（或看 `status.json`）—— 服务模式 / pid / 健康报文 / 生效路径 / 调用计数 / 写入侧
2. 服务没起来 → `libmem-bridge.log` 尾部（插件会把尾部**贴进**启动失败的报错里）
3. 服务在但搜不到 → `libmem_stats` 看 `sqlite_total`；新记忆要等温提炼，检查 `bridgePoller`
4. 写入侧没记 → `watch-status.json` 的 `rootSource` / `projectRoot`；`unresolved` 就是根没解析出来

---

## 7. 已知限制

1. **写入侧是副本不是引用**：`lib/write/` 与 `dsh-cold-memory-watcher/lib/` 有 4 处文档化差异，
   上游改了**不会**自动同步。`--check-copy` 会吵，照 [lib/write/README.md](lib/write/README.md) 重新对齐。
2. **服务串行处理工具调用**（桥里一把锁）：B3 的 SQLite 写与向量缓存不是为多写并发设计的，
   拿到的是"不会分叉写入"，代价是并发调用排队（检索 ~500ms 量级）。
3. **`mcp_server.py` 的 FastAPI 模式（:8766）仍然是坏的**（fastapi/starlette 版本不匹配），
   与本次整合无关，没有动它 —— MCP 入口（`--mcp-stdio`）本身可用，作为回滚路径保留。
4. **`libmem_status` 的写入侧字段依赖状态文件存在**；插件刚挂载、还没有任何会话时显示
   「状态文件读不到」是正常的（文件在挂载时就会写出来）。

---

## 8. 移植记录：从哪个版本合入了什么（2026-10-10）

用户问过「V1.3.2 / V1.3.4 里有改进，能不能合进这个插件」。逐文件对账的结论是：

| 来源 | 与本插件内核（`dsh-libmem/v2`）的关系 | 动作 |
|---|---|---|
| `study/v1.3.4/v2` | **逐文件一致**（唯一差异是 `mcp_server.py`，那是本次整合自己改的） | 无事可做 —— 改进本来就在跑 |
| `study/v1.3.2/v2` | 9 个文件比内核**旧**（`expand_hits` / `neighbours_of` / `lang_gate` / `rerank_doc` 等都还没有） | 无需回退 |
| `backups/shufang-os-V1.3.3-20260930` | 同样比内核旧（`routes.py` 差异方向是**旧版**，不是丢掉的改进） | 无需回退 |
| `LibMem-AML`（在 V1.3.4 上继续改的参赛版） | **有内核没有的东西** | **合入 1 项 + 补 1 项**，见下 |

### 已合入

1. **`phase2_rank` 的 fail-open 阈值护栏**（[retrieval.py](dsh-libmem/v2/services/B4_dual_gate_retrieval/retrieval.py)，源：AML 2026-10-03）
   - 问题：嵌入服务（bge-m3 @18099）不可达时落 hash 兜底 ⇒ 余弦全是噪声 ⇒ `min_sim_threshold` 把每条都切掉 ⇒
     `phase2_rank` 返回空 ⇒ `fuse_routes` / `retrieve` 里都是 `if route_fuse and ranked:` **整段跳过** ⇒
     **连词法 / 实体 / 时间路已捞到的结果也一起没了**，且服务照常 200、不报错。
   - 语义：**阈值只降噪，不许清零**。真被切空时退回未过滤的 top_k，并记 `threshold_fallback`；`above_threshold`
     改为按**原始阈值判定**统计（否则护栏生效时它报非 0，把"一条都没过阈值"盖住）。
   - 验证：`scratch\_verify_threshold_fallback.py` 14 项断言 —— 含正交向量造"全低于阈值"场景、
     正常路径逐字未变、真实查询 `above_threshold=159 / fallback=False`。
2. **阈值做成可配**（`_min_sim_threshold()`，同源）：`dsh-libmem/v2/config.yaml` 的
   `retrieval.min_sim_threshold`（缺省 0.15）。配置读不到 / 类型不对 / 抛异常都退回常量。
3. **桥的真探活 `GET /health/models`**（源：AML 的 B1 坑「只探端口 ⇒ 报 OK 但实际不可用」）：
   真调一次嵌入与重排并报往返耗时；`libmem_status` 会打出这一行。
   ★ 这条在护栏之后更要紧：嵌入挂掉**不再表现为「查不到」**，而是「查到了但全是噪声」，
   从工具响应上比空结果更难分辨。做成独立端点而非塞进 `/health`，是为了不拖慢启动就绪探针（400ms 一次）。

### 明确**没有**合入（附理由）

| AML 的改动 | 为什么不合 |
|---|---|
| `user_id` 隔离全家桶（schema 加列、`_memory_id` 拌 user_id、四路作用域、`_route_objects` 缓存键） | 本插件是**单用户**产品；要动 schema + 迁移 + id 哈希与缓存键，风险/收益不成比例 |
| heat / 审计显式 no-op | 产品的热度闭环与 B7 审计**要保留**（参赛版才砍） |
| `max_source_date()` 日期锚点 | 产品已有 v1.3.4 的 `[TODAY]` 注入（真"今天"，比"最近一条记忆"更准）；AML 自己的总纲也把"注入日期锚点"列为**已证伪** |
| `check_converged` 缩减到两处 | 产品没有删掉 B2 / B6，缩了反而少一层防漂移 |
| `rerank_doc: raw` / `routes_enabled: true` / `phase1_limit: 10000` | 这些是**配置口径**，参赛显式写死；产品侧的取舍不同（`retrieval.*` 可配） |

### 本机当前检索口径（2026-10-10）

| 键 | 当前值 | 依据 |
|---|---|---|
| `retrieval.routes_enabled` | **true**（原本 false，本次打开） | 真实中文 query A/B：Top-10 重合 **6.6/10**、耗时中位 **225→220ms**、融合带进新候选 14.8/题（12/12 有增益） |
| `retrieval.routes` | `semantic,entity,time,lexical` | 路3 时间在此批查询 **0/12** 命中（中文日常问句抽不出时间窗），留着零成本 |
| `retrieval.min_sim_threshold` | 0.15（**可配**） | 本次移植，护栏见 §8 第 1 条 |
| `retrieval.rerank_doc` | `auto`（摘要） | 产品默认；`raw` 是给"摘要语言 ≠ 源语言"的语料用的（本仓 LoCoMo 评测库） |

⚠️ **开四路后，响应里会出现 `score: 0` / `confidence: 0` 的条目。** 它们只被路2（实体）/路4（词法）
捞到，没经过 `phase2_rank` 的语义打分，所以**没有 `_sim` 字段**。
**排序本身仍然有依据**：这些条目带着 `_rerank_score`（cross-encoder 分）与 `_final_score`，
交付顺序按 `_final_score` 单调递减（实测一条查询：0.752 → 0.727 → 0.701 → 0.673 → 0.647 → 0.622）。
但**只看 `score`/`confidence` 的下游会误读**（0 分却排第 3）—— 要按相关性筛，请用 `_final_score`。

回滚：`retrieval.routes_enabled` 改回 `false` + 重启桥（插件自愈），或换回
`dsh-libmem/v2/config.yaml.bak-libmem-routes-20261010`。

### 同日第二批：A2（维度回报 + 去重）与**自动回忆**

用户要的是「插件自己判断这句对话要不要翻记忆」。分两半做：

**A2（Python 侧，重启桥即生效）** —— `libmem_tools.memory_search`：
* 结果里多一个 `dimension` 字段（这是哪类问题：查找 A / 汇合 B / 同一性 D / 操作 E…），
  字段里自带一句 `note`：**只作提示**，条数与是否取原文仍由调用方决定；
* `dedupe=true` 的维度（B1 汇总 / D 同一性）**丢掉重复摘要** —— 汇总题的"条数"必须是不同条目。
* **故意不做**：按维度改条数（本仓已证伪"多给"）、强制给原文（调用方本来就能 `depth=L3` 按需取）。

**自动回忆（JS 侧，见 `lib/recall.js`）** —— 三步：
收到真人消息 → `decide()` 判断要不要翻（寒暄/确认/斜杠命令不翻；点名/回溯词/问句/长句才翻）
→ 后台搜一次 → harness 组装模型输入时，把结果作为运行时上下文贴进去（块首是 **【自动回忆】**）。

### ★ 2026-10-12 第二批：**四档判断** + **记忆纪律**（用户要的"有录入有检索有输出"）

**起因**：用户原话「它什么时候才会激活就是调用记忆来回答我的问题？我不想它只是单纯的录入，
我想要有录入有检索有输出的」「我想有自动判断啊，或者说有明确要求或者点名要求」。
对照他喜欢的 `dsh-qwen-paint`「生图判断」（`drawTrigger` 四档）后确认，本插件缺两样：

| 缺什么 | 后果 | 现在怎么补 |
|---|---|---|
| 只有 `off`/`smart` 两档，**没有"只在点名时翻"** | 想省 token 的人无档可调；点名和普通闲聊同权 | 四档：`off`/`strict`/`smart`/`eager`（`RECALL_MODES`） |
| 只写"什么时候**搜**"，没写"搜到了**怎么用**" | 记忆块贴上来了，模型可以当没看见 = 用户说的"录入之后没有回应" | **记忆纪律**提示词段 + 「采用」计数 |

**① 四档判断**（`lib/recall.js`，与生图判断同构；`recall.mode` 配置或面板切换）

| 档位 | 行为 | 适合谁 |
|---|---|---|
| `off` | 完全不自动翻（`libmem_search` 工具照常可用） | 只想要个安静的记忆库 |
| `strict` | **只认点名与回溯词**：「查一下记忆」「上次那个…」 | 怕误触发、嫌噪音 |
| `smart`（默认） | 在 strict 之上**再加问句与长消息**（v1 的行为） | 默认 |
| `eager` | **每条真人消息都翻**，只排除寒暄与斜杠命令 | 记忆即上下文，不怕 token |

⚠️ 判断顺序里有两处**反直觉但必须**的位置（都是踩出来的）：
* **点名排在长度门槛之前** ——「查一下记忆」**正好 5 个字**，`minChars=6` 会先把
  它当"太短"扔掉；而它恰恰是最强的"我要你用记忆"信号。所以 `EXPLICIT_RE` 是第一道正面判断。
* **回溯词连 `strict` 也认** —— 该档的定义就是"点名**与回溯词**"，只认点名会让
  「上次那个方案怎么样了」在 strict 档反而搜不到（自检里有这条用例守着）。
* 旧值 `always` 仍接受（≡ `eager`）；**未知值回落 `smart` 而不是抛错** ——
  schema 收窄成 enum 的话，写错一个字母会让整棵插件树挂掉，代价远大于收益。

**② 记忆纪律**（`buildRecallDiscipline()`，`recall.discipline` 或面板开关）

与记忆块**分开注册**成第二条 context（`order 131`，块是 `130`）——这点是有意的：
纪律文本**只随档位变**，每轮都一样 ⇒ systemPrompt 前缀稳定、KV 缓存不破；
记忆块每轮都变。混成一段会让每轮都重写前缀（成本直接上去）。

它写死三条要求（照生图判断的教训：**禁止 + 给理由**，软话没用）：
1. 块里有相关内容 → **当事实依据直接用**，带上出处（日期 / `memory_id`）；
2. 块里没有或不相关 → **不要硬凑**：不假装"我记得"，不拿摘要脑补原文，该说没有就说，
   然后换说法再 `libmem_search`，要细节用 `libmem_drilldown(depth=L2/L3)`；
3. **真用上了就 `libmem_feedback` 回报 `memory_id`** —— 那是 count 唯一的来源。

**③ 「采用」计数（可证伪的输出凭据）**

`libmem_feedback` 成功后回调闸门记一笔（`lib/index.js` 的 `onFeedback` → `gate.noteFeedback`），
所以 `libmem_status` / 面板上的「采用 N」是**有来源**的数字：
`注入` 说明"贴进去了"，`采用` 说明"模型回报真用上了"。两个都涨才叫"有输出"。

**④ 面板**（`/libmem/action`）

`recall` 动作从"只能 off/smart"扩成**四档白名单**（非法档位返回 400 + `allowed` 列出四档，
不静默回落 —— 静默回落会让人以为切到 strict 了、实际还在 smart 上翻）；
新增 `discipline` 动作。两者都是**内存态**：重启回到 profile 里的配置值。

⚠️ **两条硬约束**（读 harness 源码得来，写在这里防止下一个人踩）：
1. **上下文提供者是同步的**（`dsh-system-prompt` 里直接 `entry.text(context)`，不 await）——
   所以不能在提供者里现场搜索，必须收到消息时先搜好放缓存。**代价：第一句话可能赶不上**
   （组装输入比搜索快），从同一轮的第 2 步或下一轮起稳定生效。`libmem_status` 的
   「自动回忆 … 注入 N 次」就是核对这件事的凭据。
2. **注入文本里不能出现 `{{`** —— harness 会把上下文当模板解析变量，解析失败**直接抛错、
   把那一轮的模型调用弄挂**。记忆内容来自用户原话（代码片段里全是 `{{`），所以 `sanitize()`
   是必须的，不是洁癖。

开关：配置 `recall.mode` = `smart`（默认，按规则）/ `strict`（只认点名与回溯词）/
`eager`（每条都翻，旧值 `always` ≡ 它）/ `off`（不自动翻，工具照常）。
自检覆盖：四档判断 24 条用例、`{{` 消毒、注入格式、跨会话不串、无 scope 不注入。

### ★ 2026-10-12 第三批：**问"最近"却给"几个月前"** —— 检索层的时间优先

**起因（用户原话）**：
> 「为什么去读冷记忆，而不是等记忆插件把答案给你？这里差了什么？」
> 「我是问最近或者这几天的工作做了什么，而你排上来是几个月前的工作，那就有问题啊」
> 「问最近的时间或最近一周的时间，却用一个几个月前的东西，这就很打脸，完全不能用」

**先查清楚"差在哪"（三条，都是实测不是推的）**：

| 事实 | 证据 |
|---|---|
| 数据**都在**（冷库/温层/索引三层都有最近三天） | 冷库 322KB/66KB/8.9KB，温层 39.2KB/28.9KB/0.7KB；B3 索引也有 10-12 的条目 |
| `最近` **本来就被解析**成时间窗口 | `routes.parse_time('最近这几天做了什么')` → `2026-10-05..2026-10-11` |
| 坏在**排序**：窗口内的条目在每一步都被挤下去 | 换 4 种说法搜，返回的全是 4-30 / 5-03 / 5-14 那种几个月前、heat 45~70 的老条目 |

**为什么排序一定会输**：① `TimeRoute.rank()` 只出 20 条，与另外三路 RRF **等权**融合；
② 之后 cross-encoder 按"**这条和问题像不像**"打分 —— "最近这几天做了什么"与几个月前那些
"做了什么"的记录**照样很像**；③ 再叠 `α·heat`，老条目 heat 45~70、新条目只有 1~6。
⇒ **没有任何一个环节把"窗口"当回事**。

**修法一：给排序加时间优先**（`retrieval.py` 的 `query_time_window` / `recency_scores`）

* `query_time_window()` 判"这是不是时间范围查询 + 窗口几天"：认
  `最近/这几天/这两天/前天/大前天/上个月/去年`、`最近N天/周/月`、`last N days`…
  ⚠️ **不认 `上次`** —— 那是"回溯指代"不是时间窗口，认了会把指代问题变成"只要新的"。
* `recency_scores()` 给窗口内的条目 +0.35（**最新那一天再 +0.20**，让清单按天成簇而不是散点），
  窗口外按天最多 −0.30（**衰减不清零**：最近几天真没内容时，给几条稍早的比给空结果好）。
* 生效点**两处**，都用**同一份**分数：`phase2_rank`（`+ recency`）与 `rerank_top`
  （`_final_score` 里加同一项，并且**先把窗口内条目填进重排池**）。
  ⚠️ 第二处尤其要紧：重排只能重排它**看到**的东西 —— 窗口内条目若压根不在 `ranked[:pool]` 里，
  加多少分都没用。实测池内窗口条数 0 → 25。

**修法二：给一条不看相关性的清单管道**（`recent_retrieve()` / `libmem_recent`）

问"这几天我干了什么"是**枚举**题，不是"最像这句话的那一条"。相关性排序对这类问题
**结构上就是错的**：只要还按相似度排，老条目就会混进来，而清单混进几条老的，用户就觉得不能用。
所以 `libmem_recent(days, limit)` 完全不看相关性：窗口内 + 时间倒序 + 尽量列全。
它**不升温**（列清单不是"检索命中"，升温会污染 B5 的"常用浮现"）。

**修法三：自动回忆也换管道**（`recall.js` 的 `recentWindowDays()`）

前两条修的是"我去查的时候"。但**插件自己贴的那一块**同样会贴错 —— 用户问"最近这几天
做了什么"时，`note()` 原本无条件走 `memory_search`（相关性），贴进来的自然还是老记忆，
然后模型照着那块回答。所以 JS 侧也判一次时间窗口，命中就改调 `memory_timeline`，
**只贴清单块**（不再叠一份相关性结果，否则老的又混回来了）。内核没有 `memory_timeline`
（老版本）时退回相关性检索，绝不让回忆失败。
⚠️ 两份时间正则在 JS 与 Python 各有一份，**必须同步改**；判错的代价不对称 ——
多判只是"多贴一块清单"，少判就是"又贴回老记忆"，所以 JS 侧**宁可多判**。

**实测 A/B（真实库、真实中文问句、只读零副作用，`scratch\_verify_recency_ab.py`）**：

```
query: 最近这几天做了什么
  关 recency: 2026-04-30 | 04-28 | 04-27 | 04-30 | 04-29 | 04-28 | 04-30 | 05-04 | 04-27 | 04-30
  开 recency: 2026-10-12 | 10-12 | 10-12 | 10-12 | 10-12 | 10-10 | 10-10 | 10-10 | 10-10 | 10-10
query: 最近一周的工作进展
  关 recency: 2026-04-30 | 05-01 | 05-10 | 04-30 | 10-12 | 05-10 | 10-03 | 04-29 | 10-03 | 10-03
  开 recency: 2026-10-12 | 10-12 | 10-10 | 10-10 | 10-10 | 10-12 | 10-10 | 10-10 | 10-10 | 10-10
```
窗口内条数：`inside_window=127`、重排池内 25 条；三个检测用例（含"不该命中的必须为 None"）全过。

**参数在哪调**：`dsh-libmem/v2/config.yaml` 的 `retrieval.recency.*`
（`enabled` / `default_days` / `window_base` / `today_bonus` / `decay` / `decay_floor` /
`cluster` / `window_only` / `window_cap`）。`enabled: false` 就**逐字回到改动前**，
且只影响"查询里真有最近时间词"的那种问法 —— 别的查询一个字节都没变。

**同日一并修的**：`lib/recall.js` 的纪律② 原文是"块里没有就说没有"，今天恰好证明它**有误导性**
（块里没有 ≠ 库里没有，今天就是"走错了管道"）。已改成"**先换时间范围/问法，两轮都空再说没有**"。

⚠️ **两条硬约束**（读 harness 源码得来，写在这里防止下一个人踩）：

**当天账本**：15 轮对话 = **1.697 亿 token = ¥6.72**，拆开看：

| 项目 | 数量 | 说明 |
|---|---|---|
| 命中缓存的输入 | 约 3.6 亿 | 占 token 量 96% |
| 未命中输入（真·新内容） | 328 万 | 只占 0.9% |
| 输出（含思考） | 85 万 | 单价最高（¥4–8/百万） |
| **模型调用次数** | **851 次** | 每一次都要重发全部上下文 |
| **平均每次输入** | **42.9 万 token** | ← 根因 |
| 单次峰值输入 | 67.6 万 token | |

一句话：**钱花在「每一步都把 40–60 万 token 的上下文重发一遍」上**，不在"写了多少内容"上。
所以真杠杆是**开新会话**（上下文从 43 万回到几万），而不是压缩 —— 压缩会把早期细节总结掉，
换会话不丢细节，只是需要有人记得去换。

### 三处对策（最终采用这一组）

**① 压缩改成"压得晚"**（profile patch 的 `compaction-basic`：`0.8 / 0.16`）

原来那组 `0.65 / 0.25` 是按**本地 qwen 的 153,600 窗口**算的；主模型换成窗口 100 万量级的
deepseek-flash 之后比例没动，阈值就跟着放大到 65 万。（中途试过"压得早"的 0.2/0.06，
**放弃** —— 会丢对话早期的细节。）

现在 `0.8 / 0.16`：约 80 万才压 —— **正常情况下你会先收到提醒去开新会话，压缩根本不会发生**；
真压了也保留约 16 万 token 原文，够接上话。

**② 到点提醒换会话 —— 已拆成独立插件 `dsh-context-guard`**

2026-10-10 按用户要求从本插件里**摘出去了**：记忆插件只管记忆，
"盯上下文占用"是另一件事，独立成插件才能单独启用/卸载/配置，也不该让记忆插件
顺带读用量。

它读 `assistant/message` 事件的 `usage`（`inputTokens + cacheReadTokens + cacheWriteTokens`
= 最近一次调用的输入 = 当前上下文占用），到 80% 就注入【⚠️ 上下文提醒】，
不重复喊（再涨 5 个百分点才再提醒）。

- 代码与文档：`..\dsh-context-guard\`（`lib/index.js` + `README.md`，29 项自检）
- 本插件现在**不含**任何上下文占用逻辑（可用 `grep warnAtRatio dsh-libmem\lib` 自查为零）

**③ 新会话开局自动带"最近在做的事"**（`recall.coldStart`）

短会话不背历史，但开局是失忆的 —— 这一段补上：按**时间**取最近 6 条记忆贴进去
（内核新增的 `memory_recent()`），与按**相关性**的【自动回忆】分工明确：

- 【最近在做什么】= 你开口之前我最近在忙什么（只在每个会话第一条消息时贴一次）
- 【自动回忆】= 关于你刚说的话我记得什么（按规则判断）

### 怎么用

- **一个任务一个会话**。看到上下文提醒（或面板变红）就换会话 —— 这一条能省最多。
- 需要历史细节时直接问 / 让我查（`libmem_search`、`libmem_drilldown`），而不是指望"上下文里都带着"。
- 审计脚本：`node scratch\_token_audit.mjs`（按会话、按轮次拆 token 与估算费用）。

### 参数在哪调

| 想改什么 | 改哪里 | 现值 |
|---|---|---|
| 什么时候压缩 | profile patch `compaction-basic.thresholdRatio` | `0.8`（≈80 万） |
| 压缩后留多少 | 同上 `retainRatio` | `0.16`（≈16 万） |
| 什么时候提醒换会话 | **独立插件** dsh-context-guard 配置 `warnAtRatio` | `0.8` |
| 当前模型窗口（算百分比用） | 同上 `contextWindow` | `1000000` |
| 开局贴几条 | libmem 配置 `recall.coldStartItems` | `6` |
