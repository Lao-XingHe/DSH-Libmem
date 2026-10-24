/**
 * LibMem 插件配置 schema。
 *
 * 必须是**真的 schema 对象**（schemastery），不能是普通对象：Cordis 通过
 * `runtime.Config['~standard'].validate(config)` 解析插件配置，普通对象会让
 * 整棵插件树挂掉，报 `Cannot read properties of undefined (reading 'validate')`。
 * （这条踩坑记录来自 `dsh-cold-memory-watcher`，这里照抄它的做法。）
 *
 * @module dsh-libmem/config
 */
import path from 'node:path'
import z from '@deepseek-ai/schemastery'
import { Config as WriteConfig } from './write/index.js'

/** 包根目录（`<pkg>/lib/config.js` → `<pkg>`）。 */
export const PACKAGE_ROOT = path.join(import.meta.dirname, '..')

/**
 * 默认值。导出给校验脚本与 `apply()` 用，避免 schema 与运行期兜底漂移。
 */
export const DEFAULTS = {
  enabled: true,

  // ---- Python 服务托管 ----
  /**
   * 解释器绝对路径。
   *
   * ⚠️ 不要写裸 `python`：PATH 里任何一个别的解释器排到前面（WorkBuddy、
   * 商店版 Python），服务就会以 `No module named 'fastapi'` 起不来 —— 旧 MCP
   * 配置里为这条打过一次补丁，不再犯第二次。
   */
  python: '',
  /** 服务工作目录，必须含 `config.yaml`（路径解析的根）。 */
  projectRoot: '',
  host: '127.0.0.1',
  port: 8768,
  /** false = 只连已有的服务，绝不自己起进程。 */
  manageProcess: true,
  /** 端口上已经有一个健康的 LibMem 服务时复用它，不重复起。 */
  reuseExisting: true,
  /** 等服务就绪的上限（首次要建 schema + 起轮询）。 */
  startTimeoutMs: 90000,
  /** 单次工具调用上限（检索冷启动含 jieba/bge 首次加载）。 */
  toolCallTimeoutMs: 120000,
  /** 桥是否起冷库轮询（B1 → B2 温提炼）。并行验证期可临时关掉。 */
  bridgePoller: true,
  /** 服务日志（stdout+stderr 都进这里）。 */
  logFile: path.join(PACKAGE_ROOT, 'libmem-bridge.log'),
  /** 插件状态心跳文件。 */
  statusFile: path.join(PACKAGE_ROOT, 'status.json'),
  maxRestarts: 5,
  restartDelayMs: 3000,

  // ---- 工具面 ----
  registerTools: true,
  /** 工具名前缀：`libmem` → `libmem_search` 等。 */
  toolPrefix: 'libmem',
  /** 是否在界面右下角挂「记忆」面板（`/libmem/*` 同源接口）。 */
  panel: true,

  // ---- 自动回忆（插件自己判断"这句话要不要翻记忆"）----
  recall: {
    enabled: true,
    /**
     * 判断档位（★ 2026-10-12 四档，照千问绘图的「生图判断」`drawTrigger` 做的）：
     *   off    完全不自动翻（`libmem_search` 工具照常可用）
     *   strict 只认**点名**（「查一下记忆」）与回溯词（「上次…」「还记得」）
     *   smart  在 strict 基础上再加问句与长消息（默认，也是 v1 的行为）
     *   eager  每条真人消息都翻，只排除寒暄与斜杠命令
     * 旧值 `always` 仍接受，等同于 `eager`。
     */
    mode: 'smart',
    maxItems: 5,
    maxChars: 1200,
    minChars: 6,
    freshMs: 180000,
    /**
     * ★ 2026-10-12：是否注入「记忆使用纪律」提示词段。
     *
     * 为什么需要它：判断只管"搜不搜"、贴块只管"看不看得见"，模型仍可以**当没看见**。
     * 这一段写死三条要求（块里有就直接当依据用 / 没有就别硬凑也别装记得 /
     * 真用上了就 `libmem_feedback` 回报 id），并随档位调整措辞。
     * 它注册成**独立的 systemPrompt 段**，只随档位变 —— 不破坏 KV 缓存前缀。
     */
    discipline: true,
    /**
     * 新会话开局额外贴一段"最近在做的事"（按时间取，不按相关性）。
     * ★ 这是"短会话省钱"方案的另一半：会话不再背几十万 token 的历史，
     *   但要靠它把上下文接上。
     */
    coldStart: true,
    coldStartItems: 6,
    coldStartDays: 14,
  },

  // ---- 本地模型（卸载 / 按需拉起）----
  //  ★ 2026-10-16：用户要求「加一个卸载模型按钮 + 一个需要时自动拉起的开关」。
  //  三个模型都在吃显存（4B ~1.9-4.0G / bge-m3 ~730M / bge-reranker ~460M），
  //  控制逻辑在 `./models.js`，面板控件在 `assets/panel.js`。
  models: {
    /** 「需要时自动拉起」的**初始**值；面板上的开关会覆盖它（落 stateFile，重启仍生效）。 */
    autoload: true,
    /** 开关的持久化文件。 */
    stateFile: path.join(PACKAGE_ROOT, 'model-autoload.json'),
    /** llama.cpp 路由：4B 在里面，按需加载、靠 sleep-idle-seconds 自己睡。 */
    routerUrl: 'http://127.0.0.1:8089',
    routerModel: 'Qwen3.5-4B-Q4_K_M',
    /** 嵌入服务：**独立进程**，卸载 = 结束进程（llama.cpp 单实例没有卸载 API）。 */
    embedBase: 'http://127.0.0.1:18099',
    embedPort: 18099,
    embedStartScript: 'D:\\Deepseek Harness\\dsh-cold-memory-watcher\\bin\\start-bge-embed.ps1',
    /** 重排服务：同上。 */
    rerankBase: 'http://127.0.0.1:18098',
    rerankPort: 18098,
    rerankStartScript: 'D:\\Deepseek Harness\\dsh-cold-memory-watcher\\bin\\start-bge-rerank.ps1',
    /** 等一个服务被拉起来的上限（脚本自己还要等就绪）。 */
    startTimeoutMs: 60000,
    /** 单次状态探测的超时 —— 面板每次刷新都会探，不能拖。 */
    probeTimeoutMs: 2000,
  },

  // ---- 写入侧（B1 冷库）----
  write: {
    enabled: true,
    vault: '',
    // 会话 cwd 是 `D:\Deepseek Harness`，那里没有 config.yaml —— 只能显式给根。
    projectRoot: '',
    maxAssistantChars: 8000,
    statusFile: path.join(PACKAGE_ROOT, 'watch-status.json'),
    poll: true,
    pollIntervalMs: 15000,
    pollStateFile: path.join(PACKAGE_ROOT, 'poll-state.json'),
  },
}

/** 插件配置 schema。 */
export const Config = z.object({
  enabled: z.boolean().default(DEFAULTS.enabled),

  python: z.string().default(DEFAULTS.python),
  projectRoot: z.string().default(DEFAULTS.projectRoot),
  host: z.string().default(DEFAULTS.host),
  port: z.number().step(1).min(1).max(65535).default(DEFAULTS.port),
  manageProcess: z.boolean().default(DEFAULTS.manageProcess),
  reuseExisting: z.boolean().default(DEFAULTS.reuseExisting),
  startTimeoutMs: z.number().step(1).min(1000).default(DEFAULTS.startTimeoutMs),
  toolCallTimeoutMs: z.number().step(1).min(1000).default(DEFAULTS.toolCallTimeoutMs),
  bridgePoller: z.boolean().default(DEFAULTS.bridgePoller),
  logFile: z.string().default(DEFAULTS.logFile),
  statusFile: z.string().default(DEFAULTS.statusFile),
  maxRestarts: z.number().step(1).min(0).default(DEFAULTS.maxRestarts),
  restartDelayMs: z.number().step(1).min(0).default(DEFAULTS.restartDelayMs),

  registerTools: z.boolean().default(DEFAULTS.registerTools),
  toolPrefix: z.string().default(DEFAULTS.toolPrefix),
  panel: z.boolean().default(DEFAULTS.panel),

  // 自动回忆：嵌套 schema 的默认值同样会被**填成自己的默认值**，所以
  // `lib/index.js` 里对 mode 做了显式兜底（照 §4 那条踩坑注释处理）。
  recall: z.object({
    enabled: z.boolean().default(DEFAULTS.recall.enabled),
    // 四档：off / strict / smart / eager（旧值 always → eager）。
    // 这里**故意不收窄成 enum**：`lib/recall.js` 的 `normalizeMode()` 是唯一真源，
    // 未知值一律回落 `smart` 并留痕 —— 收窄成 enum 会让写错一个字母的人**整个插件起不来**
    // （schema 校验失败 = 插件树挂掉），代价远大于收益。
    mode: z.string().default(DEFAULTS.recall.mode),
    maxItems: z.number().step(1).min(1).max(30).default(DEFAULTS.recall.maxItems),
    maxChars: z.number().step(1).min(200).default(DEFAULTS.recall.maxChars),
    minChars: z.number().step(1).min(1).default(DEFAULTS.recall.minChars),
    freshMs: z.number().step(1).min(1000).default(DEFAULTS.recall.freshMs),
    // ★ 2026-10-12：纪律提示词段的开关（`lib/recall.js` 的 buildRecallDiscipline）。
    discipline: z.boolean().default(DEFAULTS.recall.discipline),
    coldStart: z.boolean().default(DEFAULTS.recall.coldStart),
    coldStartItems: z.number().step(1).min(0).max(30).default(DEFAULTS.recall.coldStartItems),
    coldStartDays: z.number().step(1).min(0).default(DEFAULTS.recall.coldStartDays),
  }),

  // 本地模型（卸载 / 按需拉起）：嵌套 schema 的默认值会被填成自己的默认值，
  // 所以 `lib/index.js` 里同样做了一次显式兜底（照 `write` 那条踩坑注释）。
  models: z.object({
    autoload: z.boolean().default(DEFAULTS.models.autoload),
    stateFile: z.string().default(DEFAULTS.models.stateFile),
    routerUrl: z.string().default(DEFAULTS.models.routerUrl),
    routerModel: z.string().default(DEFAULTS.models.routerModel),
    embedBase: z.string().default(DEFAULTS.models.embedBase),
    embedPort: z.number().step(1).min(1).max(65535).default(DEFAULTS.models.embedPort),
    embedStartScript: z.string().default(DEFAULTS.models.embedStartScript),
    rerankBase: z.string().default(DEFAULTS.models.rerankBase),
    rerankPort: z.number().step(1).min(1).max(65535).default(DEFAULTS.models.rerankPort),
    rerankStartScript: z.string().default(DEFAULTS.models.rerankStartScript),
    startTimeoutMs: z.number().step(1).min(1000).default(DEFAULTS.models.startTimeoutMs),
    probeTimeoutMs: z.number().step(1).min(200).default(DEFAULTS.models.probeTimeoutMs),
  }),

  write: WriteConfig,
})
