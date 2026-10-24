/**
 * LibMem 服务托管 —— 起、探、守 Python 桥。
 *
 * 三种状态都要能被外部看见（写进 `statusFile`）：`reused`（复用已有服务）、
 * `spawned`（自己起的）、`down`（起不来）。旧 MCP 配置里最贵的一课就是
 * 「服务没起来，但 dsh 照常启动，直到有人调记忆工具才发现」—— 所以这里
 * 把**进程模式、pid、健康报文、失败原因、日志尾部**全部落到状态文件里。
 *
 * 进程用 `stdio: ['ignore', fd, fd]` 起：
 *   * 不用管道 —— 受限沙箱下管道 spawn 直接 EPERM（这是整件事的起因）；
 *   * 不丢日志 —— stdout/stderr 都写进 `logFile`，失败时插件能读回尾部
 *     贴进错误信息，而不是让人去猜。
 *
 * @module dsh-libmem/service
 */
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import { callTool, getHealth, LibmemError, makeBaseUrl } from './client.js'

/** Python 侧模块入口（相对 `projectRoot`，即以 `-m` 运行）。 */
const BRIDGE_MODULE = 'services.C2_mcp_server.libmem_bridge'

/** 探活重试间隔。 */
const HEALTH_POLL_MS = 400

/** 失败时回读日志的行数。 */
const LOG_TAIL_LINES = 25

/**
 * 托管一个 LibMem 服务实例，并作为插件侧的调用门面。
 */
export class LibmemService {
  /**
   * @param {object} options
   * @param {object} options.settings - 已解析的插件配置。
   * @param {(message: string) => void} [options.log] - 诊断输出（必须走 stderr）。
   * @param {(name: string) => Promise<void>} [options.beforeCall] - 每次转发工具调用前的钩子
   *   （★ 2026-10-16：用来实现「需要时自动拉起」—— 模型被卸载后，第一次检索自己把它拉回来）。
   */
  constructor ({ settings, log = () => {}, beforeCall = null }) {
    this.settings = settings
    this.log = log
    this.beforeCall = beforeCall
    this.base = makeBaseUrl(settings.host, settings.port)

    /** @type {import('node:child_process').ChildProcess|null} */
    this.child = null
    /** @type {number|null} 日志文件描述符（stdout/stderr 共用）。 */
    this.logFd = null
    this.disposing = false
    /** @type {NodeJS.Timeout|null} */
    this.restartTimer = null
    /** @type {Promise<object>|null} 进行中的 start()，用于并发去重。 */
    this.starting = null

    this.status = {
      plugin: 'libmem',
      startedAt: new Date().toISOString(),
      base: this.base,
      mode: null,
      pid: null,
      health: null,
      restarts: 0,
      calls: 0,
      failures: 0,
      lastCall: null,
      lastError: null,
      updatedAt: new Date().toISOString(),
    }
  }

  /** 把状态刷到磁盘。诊断绝不能反过来把插件弄挂，所以整段吞异常。 */
  writeStatus () {
    this.status.updatedAt = new Date().toISOString()
    try {
      if (this.settings.statusFile) {
        fs.writeFileSync(this.settings.statusFile, JSON.stringify(this.status, null, 2), 'utf8')
      }
    } catch {
      /* diagnostics must never break the session */
    }
  }

  /**
   * 探活。
   * @param {number} [timeoutMs]
   * @returns {Promise<object|null>}
   */
  async health (timeoutMs = 3000) {
    return getHealth(this.base, timeoutMs)
  }

  /**
   * 确保服务可用（幂等、并发安全）。
   *
   * @returns {Promise<object>} 本方法自己的状态快照。
   */
  async start () {
    if (this.starting) return this.starting
    this.starting = this.#doStart().finally(() => { this.starting = null })
    return this.starting
  }

  async #doStart () {
    if (this.settings.reuseExisting) {
      const health = await getHealth(this.base, 2500)
      if (health) {
        this.#adopt('reused', health, null)
        this.log(`复用已在运行的服务 ${this.base}（pid=${health.pid}，${health.toolCount} 个工具）`)
        return this.status
      }
    }

    if (!this.settings.manageProcess) {
      this.status.mode = 'down'
      this.status.lastError = `manageProcess=false 且 ${this.base} 上没有健康的 LibMem 服务`
      this.writeStatus()
      throw new LibmemError(this.status.lastError, { code: 'UNREACHABLE' })
    }

    // 只有**真要自己起进程**时才要求 projectRoot：复用别人的服务不需要它。
    // 发布版的默认值是空的（不能写死作者机器上的路径），所以这里给人话，而不是让
    // `spawn` 用一个空 cwd 抛出看不懂的错。
    if (!String(this.settings.projectRoot || '').trim()) {
      this.status.mode = 'down'
      this.status.lastError = 'projectRoot 未配置：它必须指向你的「书房 OS v2」目录（该目录下要有 config.yaml）'
      this.writeStatus()
      throw new LibmemError(this.status.lastError, { code: 'NOT_CONFIGURED' })
    }

    this.#spawn()
    const health = await this.#waitHealthy(this.settings.startTimeoutMs)
    if (!health) {
      const tail = this.logTail()
      this.status.mode = 'down'
      this.status.lastError = `服务在 ${this.settings.startTimeoutMs}ms 内未就绪（${this.base}）`
      this.writeStatus()
      throw new LibmemError(
        `${this.status.lastError}\n--- ${this.settings.logFile} 尾部 ---\n${tail}`,
        { code: 'START_FAILED' },
      )
    }
    this.#adopt('spawned', health, this.child?.pid ?? null)
    this.log(`已启动服务 ${this.base}（pid=${health.pid}，${health.toolCount} 个工具）`)
    return this.status
  }

  #adopt (mode, health, pid) {
    this.status.mode = mode
    this.status.health = health
    this.status.pid = pid ?? health?.pid ?? null
    this.status.lastError = null
    this.writeStatus()
  }

  #spawn () {
    const { projectRoot, host, port, bridgePoller, logFile } = this.settings
    // 解释器留空 = 走 PATH 里的 `python`（发布版默认）；装了错解释器时报错会在日志里。
    const python = String(this.settings.python || '').trim() || 'python'
    const args = ['-m', BRIDGE_MODULE, '--host', host, '--port', String(port)]
    if (!bridgePoller) args.push('--no-poller')

    try {
      this.logFd = fs.openSync(logFile, 'a')
    } catch (error) {
      this.logFd = null
      this.log(`打不开日志文件 ${logFile}（继续，但子进程输出会丢）：${String(error)}`)
    }

    const stdio = this.logFd === null
      ? ['ignore', 'ignore', 'ignore']
      : ['ignore', this.logFd, this.logFd]

    this.log(`启动：${python} ${args.join(' ')}  (cwd=${projectRoot})`)
    this.child = spawn(python, args, {
      cwd: projectRoot,
      stdio,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
    })
    this.status.pid = this.child.pid ?? null
    this.writeStatus()

    this.child.on('error', (error) => {
      // 解释器路径不对之类：spawn 本身失败，不会有 exit。
      this.status.lastError = `spawn 失败：${String(error)}`
      this.writeStatus()
      this.log(this.status.lastError)
    })

    this.child.on('exit', (code, signal) => this.#onExit(code, signal))
  }

  #onExit (code, signal) {
    this.child = null
    this.status.pid = null
    if (this.disposing) {
      this.log(`服务已停止（code=${code} signal=${signal}）`)
      return
    }

    this.status.lastError = `服务意外退出（code=${code} signal=${signal}）`
    this.log(`${this.status.lastError}\n--- 日志尾部 ---\n${this.logTail()}`)

    if (this.status.restarts >= this.settings.maxRestarts) {
      this.status.mode = 'down'
      this.status.lastError += `；已达重启上限 ${this.settings.maxRestarts}，不再自动重启`
      this.writeStatus()
      return
    }

    this.status.restarts += 1
    this.status.mode = 'restarting'
    this.writeStatus()
    this.restartTimer = setTimeout(() => {
      this.start().catch((error) => {
        this.status.lastError = `重启失败：${error.message}`
        this.writeStatus()
      })
    }, this.settings.restartDelayMs)
    // 长驻定时器会把宿主进程钉住不让退出 —— 宿主自己拥有生命周期。
    this.restartTimer.unref?.()
  }

  /**
   * 等服务就绪；子进程提前死掉就立刻放弃（不用等满超时）。
   * @param {number} timeoutMs
   * @returns {Promise<object|null>}
   */
  async #waitHealthy (timeoutMs) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const health = await getHealth(this.base, 2000)
      if (health) return health
      if (this.child === null && this.status.mode !== 'restarting') return null
      await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_MS))
    }
    return null
  }

  /**
   * 调一个工具。服务掉线时**先自愈一次**再报错 —— 手动重启服务不该是使用者的活。
   *
   * @param {string} name - Python 侧工具名。
   * @param {object} args
   * @param {AbortSignal} [signal]
   * @returns {Promise<any>} 工具结果（原样透传）。
   */
  async call (name, args, signal) {
    // ★ 2026-10-16：「需要时自动拉起」（面板上的第二个开关）。
    //   模型被「卸载模型」清掉之后，第一次真正要用模型的调用在这里把它拉回来。
    //   钩子**绝不允许**把调用本身弄失败：它抛错就吞掉、继续往下走 ——
    //   拉不起来最多是检索质量降级（本来就有 fail-open 护栏），
    //   而"因为拉模型失败所以连记忆都读不到"是不可接受的。
    if (this.beforeCall) {
      try {
        await this.beforeCall(name)
      } catch (error) {
        this.log(`按需拉起模型失败（继续调用 ${name}）：${String(error)}`)
      }
    }
    try {
      return await this.#callOnce(name, args, signal)
    } catch (error) {
      const retryable = error instanceof LibmemError &&
        (error.code === 'UNREACHABLE' || error.code === 'TIMEOUT')
      if (!retryable || signal?.aborted === true) throw error
      this.log(`${name} 失败（${error.code}），尝试拉起服务后重试一次`)
      try {
        await this.start()
      } catch (startError) {
        throw error // 原始错误更有信息量（它是"为什么调用失败"）
      }
      return this.#callOnce(name, args, signal)
    }
  }

  async #callOnce (name, args, signal) {
    const startedAt = Date.now()
    try {
      const { result, elapsedMs } = await callTool(
        this.base, name, args, this.settings.toolCallTimeoutMs, signal,
      )
      this.status.calls += 1
      this.status.lastCall = { name, at: new Date().toISOString(), ms: elapsedMs ?? (Date.now() - startedAt) }
      this.writeStatus()
      return result
    } catch (error) {
      this.status.failures += 1
      this.status.lastError = error.message
      this.writeStatus()
      throw error
    }
  }

  /**
   * 回读日志尾部，用于把「为什么没起来」直接贴进错误信息。
   * @returns {string}
   */
  logTail () {
    try {
      const text = fs.readFileSync(this.settings.logFile, 'utf8')
      return text.split(/\r?\n/).slice(-LOG_TAIL_LINES).join('\n')
    } catch (error) {
      return `（读不到日志 ${this.settings.logFile}：${String(error)}）`
    }
  }

  /** 停掉自己起的服务并释放句柄。复用来的服务**不动**。 */
  async dispose () {
    this.disposing = true
    if (this.restartTimer) {
      clearTimeout(this.restartTimer)
      this.restartTimer = null
    }
    const child = this.child
    this.child = null
    if (child && this.status.mode !== 'reused') {
      try {
        child.kill()
      } catch (error) {
        this.log(`停服务失败：${String(error)}`)
      }
    }
    if (this.logFd !== null) {
      try {
        fs.closeSync(this.logFd)
      } catch {
        /* 关不掉也无所谓，进程要退了 */
      }
      this.logFd = null
    }
    this.status.mode = 'disposed'
    this.writeStatus()
  }
}
