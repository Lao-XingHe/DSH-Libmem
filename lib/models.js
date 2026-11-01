/**
 * 模型服务的「卸载 / 按需拉起」控制。
 *
 * 为什么需要这个模块
 * ------------------
 * 记忆插件背后有三个本地模型在吃显存（RTX 3060 12GB，2026-10-16 逐进程实测）：
 *
 *   Qwen3.5-4B-Q4_K_M    127.0.0.1:8089 路由，B2 温提炼用   加载态 1.9~4.0 GB
 *   bge-m3-F16           127.0.0.1:18099，检索嵌入（1024 维） 加载态 ~730 MB
 *   bge-reranker-v2-m3   127.0.0.1:18098，检索重排           加载态 ~460 MB
 *
 * 三者都**没有**"我不用了立刻还回来"的统一手段：
 *   * 4B 靠路由的 `sleep-idle-seconds`（`models.ini` 里 300s）自己睡；
 *   * 两个 bge 是**独立进程**，llama.cpp 单实例没有卸载 API，只能结束进程。
 * 所以"现在立刻把显存还回来"这件事必须由插件动手 —— 这就是本模块存在的理由。
 *
 * 两条语义（对应面板上的两个控件）
 * --------------------------------
 *   1. **卸载**（按钮）：立刻释放。
 *      · 4B 走路由原生的 `POST /models/unload`（实测返回 `{"success":true}`，
 *        router 侧状态从 `loaded` 变 `unloaded`，子进程消失）；
 *      · bge 走"**按端口**找进程再结束" —— 必须按端口，不能按进程名：
 *        路由的子实例也是 `llama-server.exe`，按名字杀会把 4B 一起带走。
 *   2. **需要时自动拉起**（开关）：开启时，任何一次要碰模型的工具调用之前，
 *      插件先把不在线的服务拉起来再放行；**关掉 = 卸载之后它就一直保持卸载**，
 *      直到面板上手动拉起。开关**持久**（落 `stateFile`），不像自动回忆档位
 *      那样只是内存态 —— 重启 DSH 后仍然生效。
 *
 * 一个刻意的取舍
 * --------------
 * 卸载**不**区分"睡着的"和"加载中的"：睡着的进程壳每个还占 ~115 MB，
 * 既然按钮的语义是"现在就把显存还回来"，那就一起清掉。代价是下次要重新
 * 从磁盘加载（实测 bge 唤回约 1.9s，进程重起约 2~6s）。
 *
 * @module dsh-libmem/models
 */
import { spawn } from 'node:child_process'
import fs from 'node:fs'

/** 找进程、发停止命令用的宿主 shell。 */
const PS = 'powershell.exe'

/**
 * 需要本地模型在场的工具。只有这些调用才会触发「需要时自动拉起」——
 * 写记忆、看统计、审计这些不碰向量，不该因为一次写入就把 1.2GB 拉回显存。
 *
 * ★ 2026-10-24：移出 `memory_timeline` 与 `memory_drilldown`。
 *   两个都**根本不用向量**：
 *     · `memory_timeline` → `B4.recent_retrieve()`：窗口内 + 时间倒序，
 *       代码注释原话是"**完全不看相关性**"；
 *     · `memory_drilldown` → `B6.drilldown_handler()`：按 `memory_id` 取原文。
 *   留在集合里等于"问一句『这几天干了什么』就白拉起两个 bge（约 1.2GB）"，
 *   而它们本来只是躺在床上睡着的（唤醒还要 ~2s × 2）。
 *   判据就一条：**这个工具的实现里有没有 `_embed_text` / `_rerank_scores`**。
 */
export const RETRIEVAL_TOOLS = new Set([
  'memory_search',
  'memory_search_simple',
  'knowledge_search',
  'memory_extract',
])

/** 睡一小会儿。 */
function sleep (ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 一次 HTTP 探测。**任何异常都变成 `{ok:false}`** —— 探测不该把调用方炸掉。
 *
 * @param {string} url
 * @param {object} [options] - fetch 选项。
 * @param {number} [timeoutMs]
 * @returns {Promise<{ok: boolean, status?: number, data?: any, error?: string}>}
 */
async function fetchJson (url, options = {}, timeoutMs = 2000) {
  try {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) return { ok: false, status: response.status }
    const data = await response.json().catch(() => null)
    return { ok: true, data }
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) }
  }
}

/**
 * 结束监听在指定端口上的 llama-server。
 *
 * ⚠️ 判据是命令行里的 `--port <port>`，**不是进程名** —— 路由的子实例也是
 * `llama-server.exe`。用 `\s+` 并带边界，避免 `1809` 误伤 `18099`。
 *
 * 退出码不足以证明"杀掉了"（没匹配到也是 0），所以调用方一律以**端口探测**
 * 为准复验 —— 这里的返回值只表示"命令发出去了"。
 *
 * @param {number} port
 * @param {(message: string) => void} log
 * @returns {Promise<boolean>} 命令是否成功执行。
 */
function killLlamaByPort (port, log) {
  return new Promise((resolve) => {
    const script =
      'Get-CimInstance Win32_Process -Filter "Name=\'llama-server.exe\'" | ' +
      `Where-Object { $_.CommandLine -match '--port\\s+${port}(\\s|$)' } | ` +
      'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }'

    let settled = false
    const finish = (ok) => {
      if (settled) return
      settled = true
      resolve(ok)
    }

    let child
    try {
      child = spawn(PS, ['-NoProfile', '-NonInteractive', '-Command', script], {
        stdio: 'ignore',
        windowsHide: true,
      })
    } catch (error) {
      log(`结束端口 ${port} 上的进程失败（spawn）：${String(error)}`)
      return finish(false)
    }
    child.on('error', (error) => {
      log(`结束端口 ${port} 上的进程失败：${String(error)}`)
      finish(false)
    })
    child.on('exit', (code) => finish(code === 0))
    const guard = setTimeout(() => finish(false), 20000)
    guard.unref?.()
  })
}

/**
 * 跑一个 bge 启动脚本。
 *
 * 走**已有的 ps1**（`dsh-cold-memory-watcher\bin\start-bge-*.ps1`）而不是自己拼
 * llama-server 命令行，理由：那两个脚本已经处理了"已在跑就不重复启动 / 缺模型
 * 文件就报错 / 日志重定向 / 等就绪"四件事，重写一遍只会多一份会漂移的真相源。
 *
 * `-File` 的路径用 argv 数组传给 Node，由 Node 负责加引号 —— 路径含空格
 * （`<DSH_WORKSPACE>\...`）不会像 `Start-Process -ArgumentList` 那样被拆开。
 *
 * @param {string} script
 * @param {(message: string) => void} log
 * @returns {Promise<{ok: boolean, error?: string}>}
 */
function runStartScript (script, log) {
  if (!script) return Promise.resolve({ ok: false, error: '未配置启动脚本路径' })
  try {
    if (!fs.existsSync(script)) return Promise.resolve({ ok: false, error: `启动脚本不存在：${script}` })
  } catch (error) {
    return Promise.resolve({ ok: false, error: String(error) })
  }

  return new Promise((resolve) => {
    try {
      // ⚠️⚠️ **不要用 `detached: true`**（2026-10-16 实测，对照实验）：
      //   `detached:true` 在 Windows 上会带 `DETACHED_PROCESS` —— 那个 powershell
      //   **没有控制台**，于是脚本里的 `Start-Process -WindowStyle Hidden
      //   -RedirectStandardOutput/-RedirectStandardError` 直接失败，llama-server
      //   一个都没起来，而脚本自己还以 exit 0 退出（**静默失败**）。
      //   实测对照（同一台机器、同一条命令）：
      //     detached:true  + stdio:'ignore'  → 40s 内端口一直不监听 ❌
      //     detached:false + stdio:'ignore'  → 4s 起来 ✅
      //   不 detached 也安全：真正的 llama-server 是脚本里用 `Start-Process` 起的
      //   **独立进程**，powershell 只是等就绪的包装器，它先退也不影响模型。
      const child = spawn(PS, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script], {
        detached: false,
        stdio: 'ignore',
        windowsHide: true,
      })
      // unref 只为不把 DSH 宿主钉住不让退出；进程本身照常跑完就绪等待。
      child.unref()
      resolve({ ok: true })
    } catch (error) {
      log(`拉起 ${script} 失败：${String(error)}`)
      resolve({ ok: false, error: String(error) })
    }
  })
}

/**
 * 模型控制器。
 */
export class ModelControl {
  /**
   * @param {object} options
   * @param {object} options.settings - 已解析的 `models` 配置段。
   * @param {(message: string) => void} [options.log]
   */
  constructor ({ settings, log = () => {} }) {
    this.settings = settings
    this.log = log
    /** 进行中的 ensureReady()，用于并发去重（两个工具同时调时别起两遍）。 */
    this.ensuring = null
  }

  // ------------------------------------------------------------------ 开关

  /**
   * 当前是否"需要时自动拉起"。
   *
   * 优先读落盘文件，读不到才回落到配置值 —— 面板按一下就能改，且重启后还在。
   *
   * @returns {boolean}
   */
  autoload () {
    try {
      const raw = JSON.parse(fs.readFileSync(this.settings.stateFile, 'utf8'))
      if (typeof raw?.autoload === 'boolean') return raw.autoload
    } catch {
      /* 文件不存在 / 坏掉都回落到配置值 */
    }
    return this.settings.autoload !== false
  }

  /**
   * 写开关（持久）。
   *
   * @param {boolean} on
   * @returns {boolean} 写入后的值。
   */
  setAutoload (on) {
    const value = Boolean(on)
    try {
      fs.writeFileSync(
        this.settings.stateFile,
        JSON.stringify({ autoload: value, updatedAt: new Date().toISOString() }, null, 2),
        'utf8',
      )
    } catch (error) {
      this.log(`自动拉起开关写盘失败（本次仍生效）：${String(error)}`)
    }
    this.log(`「需要时自动拉起」= ${value ? '开' : '关'}`)
    return value
  }

  // ------------------------------------------------------------------ 状态

  /** @returns {Promise<{ok: boolean, data?: any, value?: string|null, error?: string}>} */
  async #routerState () {
    const { routerUrl, routerModel, probeTimeoutMs } = this.settings
    const probe = await fetchJson(`${routerUrl}/v1/models`, {}, probeTimeoutMs)
    if (!probe.ok) {
      return { ok: false, value: null, error: probe.error ?? `HTTP ${probe.status}` }
    }
    const entry = (probe.data?.data ?? []).find((m) => m?.id === routerModel)
    if (!entry) return { ok: true, value: 'missing' }
    return { ok: true, value: entry?.status?.value ?? 'unknown' }
  }

  /**
   * 探一个独立 llama-server。
   * @param {string} base
   * @returns {Promise<{up: boolean, sleeping: boolean}>}
   */
  async #serverState (base) {
    const probe = await fetchJson(`${base}/props`, {}, this.settings.probeTimeoutMs)
    if (!probe.ok) return { up: false, sleeping: false }
    return { up: true, sleeping: probe.data?.is_sleeping === true }
  }

  /**
   * 三处模型服务的此刻状态（面板与 `libmem_status` 共用这一份）。
   *
   * `holds` 的语义是"**还占着显存**"：睡着的 4B、睡着的 bge 都仍然占着
   * 一个进程壳（各约 115 MB），所以都算。面板的按钮靠它决定是"卸载"还是"拉起"。
   *
   * @returns {Promise<object>}
   */
  async state () {
    const [router, embed, rerank] = await Promise.all([
      this.#routerState(),
      this.#serverState(this.settings.embedBase),
      this.#serverState(this.settings.rerankBase),
    ])

    const routerHolds = router.ok && (router.value === 'loaded' || router.value === 'sleeping')
    const holds = routerHolds || embed.up || rerank.up

    return {
      autoload: this.autoload(),
      router: { url: this.settings.routerUrl, model: this.settings.routerModel, ...router, holds: routerHolds },
      embed: { base: this.settings.embedBase, ...embed, holds: embed.up },
      rerank: { base: this.settings.rerankBase, ...rerank, holds: rerank.up },
      holds,
    }
  }

  // ------------------------------------------------------------------ 卸载

  /**
   * 立刻把三个模型的显存还回去。
   *
   * 顺序有讲究：先卸 4B（一条 HTTP，快），再结束两个 bge 进程。
   * 每一步都**尽力而为** —— 一个失败不影响其余，最后统一以重新探测的结果为准，
   * 而不是以"命令发出去没有"为准（这条是从"退出码 0 不等于杀掉了"来的）。
   *
   * @returns {Promise<object>} 卸载报告。
   */
  async unload () {
    const before = await this.state()
    const steps = {}

    // 1) 4B —— 路由原生卸载
    if (!before.router.ok) {
      steps.router = { done: false, detail: `路由不可达（${before.router.error ?? '未知原因'}）` }
    } else if (!before.router.holds) {
      steps.router = { done: true, detail: '本来就是卸载状态' }
    } else {
      const { routerUrl, routerModel } = this.settings
      const res = await fetchJson(`${routerUrl}/models/unload`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: routerModel }),
      }, 20000)
      steps.router = res.ok
        ? { done: true, detail: '已卸载' }
        : { done: false, detail: res.error ?? `HTTP ${res.status}` }
      if (!res.ok) this.log(`卸载 ${routerModel} 失败：${steps.router.detail}`)
    }

    // 2) bge —— 结束独立进程
    for (const [key, label] of [['embed', '嵌入'], ['rerank', '重排']]) {
      const target = before[key]
      if (!target.up) {
        steps[key] = { done: true, detail: '本来就是停着的' }
        continue
      }
      const port = this.settings[`${key}Port`]
      const issued = await killLlamaByPort(port, this.log)
      steps[key] = { done: issued, detail: issued ? '已结束进程' : '结束命令没能执行' }
    }

    // 3) 复验：端口/路由真的放开了才算数
    await sleep(700)
    const after = await this.state()
    const residual = []
    if (after.router.holds) residual.push('4B')
    if (after.embed.up) residual.push('嵌入')
    if (after.rerank.up) residual.push('重排')
    if (residual.length) this.log(`卸载后仍有残留：${residual.join('、')}`)

    return { before, after, steps, residual, ok: residual.length === 0 }
  }

  // ------------------------------------------------------------------ 拉起

  /**
   * 拉起本地模型服务（只处理两个 bge）。
   *
   * 4B 不在这里预热：它在路由上，**第一次请求会自动加载**（这正是
   * `models.ini` 里 `sleep-idle-seconds` 那套的设计意图），提前把它拉起来
   * 只会在"这次其实不需要提炼"的时候白占 2.6 GB。
   *
   * @param {object} [options]
   * @param {boolean} [options.force] - 忽略自动拉起开关（面板上手动点的时候）。
   * @returns {Promise<object>}
   */
  async ensureReady ({ force = false } = {}) {
    if (!force && !this.autoload()) {
      return { skipped: true, reason: '自动拉起已关' }
    }
    if (this.ensuring) return this.ensuring
    this.ensuring = this.#doEnsure().finally(() => { this.ensuring = null })
    return this.ensuring
  }

  async #doEnsure () {
    const started = []
    const now = await this.state()

    for (const [key, label] of [['embed', '嵌入'], ['rerank', '重排']]) {
      if (now[key].up) continue
      const script = this.settings[`${key}StartScript`]
      this.log(`${label}服务不在线，按需拉起：${script || '(未配置脚本)'}`)
      const result = await runStartScript(script, this.log)
      if (!result.ok) {
        started.push({ key, label, ok: false, detail: result.error })
        continue
      }
      const up = await this.#waitUp(key)
      started.push({ key, label, ok: up, detail: up ? '已就绪' : `等待超时（${this.settings.startTimeoutMs}ms）` })
      if (!up) this.log(`${label}服务在 ${this.settings.startTimeoutMs}ms 内没就绪`)
    }

    return { started, startedAny: started.some((s) => s.ok === true) }
  }

  /**
   * 轮询等某个服务监听（`/props` 能应答即算就绪）。
   * @param {'embed'|'rerank'} key
   * @returns {Promise<boolean>}
   */
  async #waitUp (key) {
    const base = this.settings[`${key}Base`]
    const deadline = Date.now() + this.settings.startTimeoutMs
    while (Date.now() < deadline) {
      const probe = await fetchJson(`${base}/props`, {}, 1500)
      if (probe.ok) return true
      await sleep(500)
    }
    return false
  }
}
