/**
 * 写入侧（B1 冷库）—— dsh-libmem 的一个子模块。
 *
 * ⚠️ 本目录的 6 个文件是 `dsh-cold-memory-watcher/lib/` 的**副本**（2026-09-30 版，
 * 代码逐字节相同），只改了 4 处与包身份有关的字面量（见 `./README.md` 的对照表）：
 * 模块名、日志前缀、两个默认状态文件路径。**行为零改动**。
 *
 * 为什么不直接 import 原来那个包：`dsh-libmem` 要成为"读写一体的那一个插件"，
 * 靠绝对路径 import 另一个插件目录的内部文件，会让"删掉旧目录"变成一次静默故障。
 * 副本 + 对照表 + 校验脚本，是更笨但可验证的做法。
 *
 * Watches DSH sessions and records each human↔assistant exchange into the
 * Shufang-OS B1 cold vault, which is the first stage of the memory pipeline:
 *
 *     watch (this plugin) → B1 cold vault → B2 warm extract → B3 header.db → B4 embed
 *
 * Only `source.kind === 'user'` messages count as human input; DSH injects
 * runtime snapshots and compaction checkpoints as `user/message` events with
 * `source.kind === 'plugin'`, and those are discarded (see `./pairing.js`).
 *
 * Two deliberate design points:
 *
 * - **Logging goes to stderr.** The harness pipes plugin stdout into its own
 *   protocol stream, so a stray `console.log` would corrupt it.
 * - **The vault is resolved from the session cwd, not hardcoded.** The plugin
 *   walks up from the session's working directory for the nearest
 *   `config.yaml`, mirroring `services/shared/config/loader.py`, and reads the
 *   resolved directory back in its status file and stderr line.
 *
 * @module dsh-libmem/write
 */

import fs from 'node:fs'
import path from 'node:path'
import z from '@deepseek-ai/schemastery'
import { SessionPairing } from './pairing.js'
import { ColdVault, resolveProjectRoot, resolveVaultDir } from './vault.js'
import { discoverSessions, sessionsRoot } from './session-log.js'
import { readZstdRecords } from './zstd-frames.js'
import { SessionLogPoller } from './poller.js'

/** Plugin name used in diagnostics. */
export const name = 'libmem-write'

/** No harness service is required: this plugin only observes the event bus. */
export const inject = []

/**
 * Configuration defaults.
 *
 * Exported so tests can exercise `apply()` without going through schema
 * validation, and so the schema and the runtime fallback cannot drift.
 */
export const DEFAULTS = {
  enabled: true,
  /** Explicit vault directory. Empty means "resolve from the session cwd". */
  vault: '',
  /** Explicit Shufang-OS root. Empty means "walk up from the session cwd". */
  projectRoot: '',
  /** Per-turn cap on the stored assistant reply. */
  maxAssistantChars: 8000,
  /** Write a heartbeat file describing what the plugin has done. */
  statusFile: '',
  /**
   * Poll the on-disk session logs as well as the event bus.
   *
   * Required on hosts that never deliver `session/event` to plugins (measured
   * on the official desktop client, dsh 0.2.0-rc.2). Both sources may run at
   * once: the vault is content-addressed, so a turn seen twice is written once.
   */
  poll: true,
  /** Milliseconds between polling passes. */
  pollIntervalMs: 15000,
  /** Sessions root to scan; empty means the default `<DSH_HOME>/sessions`. */
  pollRoot: '',
  /** Byte-offset state for the poller; empty means inside the plugin directory. */
  pollStateFile: '',
  /**
   * Only poll artifacts modified within this many ms. `0` means no limit.
   * Bounds the first run, which would otherwise replay every session ever
   * recorded; use `bin/backfill.mjs` for a deliberate full history import.
   */
  pollLookbackMs: 604800000,
  /** Idle time after which a parked final turn is released. */
  pollQuietMs: 120000,
}

/**
 * Configuration schema.
 *
 * This must be a real schema object, not a plain object: Cordis resolves plugin
 * config through `runtime.Config['~standard'].validate(config)`, so a plain
 * object fails the whole plugin tree with
 * `Cannot read properties of undefined (reading 'validate')`.
 *
 * `@deepseek-ai/schemastery` is resolved through a junction in this package's
 * `node_modules`, mirroring how `dsh-hybrid-gateway` is mounted by absolute path.
 */
export const Config = z.object({
  enabled: z.boolean().default(DEFAULTS.enabled),
  vault: z.string().default(DEFAULTS.vault),
  projectRoot: z.string().default(DEFAULTS.projectRoot),
  maxAssistantChars: z.number().step(1).min(200).default(DEFAULTS.maxAssistantChars),
  statusFile: z.string().default(DEFAULTS.statusFile),
  poll: z.boolean().default(DEFAULTS.poll),
  pollIntervalMs: z.number().step(1).min(1000).default(DEFAULTS.pollIntervalMs),
  pollRoot: z.string().default(DEFAULTS.pollRoot),
  pollStateFile: z.string().default(DEFAULTS.pollStateFile),
  pollLookbackMs: z.number().step(1).min(0).default(DEFAULTS.pollLookbackMs),
  pollQuietMs: z.number().step(1).min(1000).default(DEFAULTS.pollQuietMs),
})

/** Default status file, inside the plugin directory (always writable). */
const DEFAULT_STATUS = path.join(import.meta.dirname, '..', '..', 'watch-status.json')

/** Default poller state file, inside the plugin directory (always writable). */
const DEFAULT_POLL_STATE = path.join(import.meta.dirname, '..', '..', 'poll-state.json')

function log (message) {
  process.stderr.write(`[libmem] ${message}\n`)
}

/**
 * Install the watcher.
 *
 * @param {object} ctx - Cordis plugin context.
 * @param {object} [config] - resolved configuration.
 */
export function apply (ctx, config = {}) {
  const settings = { ...DEFAULTS, ...config }
  if (settings.enabled === false) {
    log('disabled by configuration')
    return
  }

  const statusFile = settings.statusFile || DEFAULT_STATUS
  /** @type {Map<string, ColdVault>} */
  const vaults = new Map()
  /** @type {Map<string, SessionPairing>} */
  const pairings = new Map()
  let projectRootCache = settings.projectRoot || null
  /**
   * True when the cached root came from a **fallback** rather than from an
   * explicit config value or a real session's cwd.
   *
   * Why this exists: the poller ticks once at mount, before any session event,
   * and its synthetic session carries no `header.cwd` — so it is the *first*
   * caller of `ensureRoot` by construction. Without this flag a fallback
   * resolution would be frozen for the whole process lifetime and a later real
   * session could never correct it. The fallback is a reachable outcome, not a
   * theoretical one: the session cwd is `D:\Deepseek Harness` and there is no
   * `config.yaml` at or above it, so `resolveProjectRoot(cwd)` returns null.
   */
  let rootIsProvisional = false

  const status = {
    plugin: name,
    startedAt: new Date().toISOString(),
    vaultDir: null,
    projectRoot: null,
    sessionsSeen: 0,
    pairsWritten: 0,
    pairsSkipped: 0,
    pairsFailed: 0,
    lastWrite: null,
    lastError: null,
    /** How many completed turns have been handed to `record()`. */
    turnsSeen: 0,
    /** How many times a vault has been requested (1 per session, normally). */
    vaultRequests: 0,
    /** Where the Shufang-OS root came from, so a null result is diagnosable. */
    rootSource: null,
    /**
     * What the session object exposed. The harness may hand plugins a session
     * view whose `header` is not readable, in which case the cwd must be
     * recovered from the session log instead — this records which happened.
     */
    sessionCwd: null,
    sessionKeys: null,
    /**
     * MAP-D §7：被跳过的子 Agent 会话事件数。
     *
     * 子 Agent 的工具过程（schema／参数／原始返回）不该沉淀进记忆库，只该由
     * 主 Agent 的结果摘要（L1）进入。恒为 0 有两种解释：未启用委派，或 dsh
     * 没把子会话事件派发到本插件的全局钩子 —— 两者都说明没必要再拦。
     */
    subagentEventsSkipped: 0,
    /** Poller: artifacts skipped in the last pass because they are sub-agent sessions. */
    pollSubagentSkipped: 0,
    /** Poller: artifacts ever identified as sub-agent sessions (cumulative). */
    pollSubagentArtifacts: 0,
    /**
     * Poller: artifacts whose batch was replayed in the last pass because the
     * sink refused a write. Non-zero means the run is degraded — the turns are
     * not lost (they replay), but they are not stored yet either.
     */
    pollFailed: 0,
  }

  const writeStatus = () => {
    try {
      fs.writeFileSync(statusFile, JSON.stringify(status, null, 2), 'utf8')
    } catch {
      /* diagnostics must never break the session */
    }
  }

  /**
   * Read the `cwd` recorded in a session's own log header.
   *
   * Fallback for when the live session view does not expose `header.cwd`: DSH
   * stamps `cwd` into the first `session` record of the artifact, and the
   * artifact path is derived from the session id.
   *
   * @param {string} sessionId
   * @returns {string|null}
   */
  const cwdFromSessionLog = (sessionId) => {
    if (typeof sessionId !== 'string' || sessionId === '') return null
    try {
      const start = Date.now()
      for (const entry of discoverSessions()) {
        if (entry.sessionId !== sessionId) continue
        const { records } = readZstdRecords(fs.readFileSync(entry.file))
        const header = records.find((r) => r?.type === 'session')
        status.logCwdLookupMs = Date.now() - start
        return typeof header?.cwd === 'string' && header.cwd !== '' ? header.cwd : null
      }
    } catch (error) {
      status.lastError = `cwd lookup failed: ${String(error)}`
    }
    return null
  }

  /**
   * Record a newly resolved root and drop vault handles built for a different one.
   *
   * Rows already written to the old (wrong) directory stay where they are — they
   * cannot be unwritten — but every later write must go to the new root.
   *
   * @param {string} root
   * @param {string} source - human-readable provenance, kept in the status file.
   * @param {boolean} provisional - true when resolved without a real session cwd.
   */
  const adoptRoot = (root, source, provisional) => {
    const changed = projectRootCache !== null && projectRootCache !== root
    projectRootCache = root
    rootIsProvisional = provisional
    status.rootSource = source
    if (changed) {
      vaults.clear()
      log(`project root changed to ${root} (${source}); vault handles rebuilt`)
    }
  }

  /**
   * Resolve the Shufang-OS root once.
   *
   * Order: explicit config, then the live session's cwd, then the cwd recorded
   * in that session's log, then the harness process cwd.
   *
   * A root resolved by the last step is **provisional** — kept in use, but
   * re-resolved the moment a real session with a readable cwd shows up.
   */
  const ensureRoot = (session) => {
    const directCwd = typeof session?.header?.cwd === 'string' ? session.header.cwd : null
    status.sessionCwd = directCwd
    status.sessionKeys = session && typeof session === 'object'
      ? Object.keys(session).slice(0, 12)
      : null

    // 1. Explicit config wins, always, and is never second-guessed.
    if (settings.projectRoot) {
      projectRootCache = settings.projectRoot
      rootIsProvisional = false
      status.rootSource = 'config.projectRoot'
      return projectRootCache
    }

    // Fast path: a trusted root is already known. Re-resolving on every turn
    // would run `shufangRootHint()` (env probes, a YAML read, a directory scan)
    // once per event.
    if (projectRootCache && !rootIsProvisional) return projectRootCache

    // 2. The live session exposed a cwd — trusted, and it may replace a
    //    provisional root.
    if (directCwd) {
      const resolved = resolveProjectRoot(directCwd)
      if (resolved) {
        adoptRoot(resolved, `session.header.cwd(${directCwd})`, false)
        return projectRootCache
      }
    }

    // 3. This session's own log records the cwd it was started with.
    const loggedCwd = cwdFromSessionLog(session?.id)
    if (loggedCwd) {
      const resolved = resolveProjectRoot(loggedCwd)
      if (resolved) {
        adoptRoot(resolved, `session-log.cwd(${loggedCwd})`, false)
        return projectRootCache
      }
    }

    // 4. Keep whatever provisional root we already had.
    if (projectRootCache) return projectRootCache

    // 5. Last resort, and therefore provisional.
    const fallback = resolveProjectRoot()
    if (fallback) {
      adoptRoot(fallback, `process.cwd(${process.cwd()})`, true)
      return projectRootCache
    }
    projectRootCache = null
    status.rootSource = 'unresolved'
    return null
  }

  /** Get (creating on first use) the vault backing one session. */
  const vaultFor = (session) => {
    const sessionId = session?.id ?? 'unknown'
    const cached = vaults.get(sessionId)
    if (cached) return cached
    status.vaultRequests += 1

    const root = ensureRoot(session)
    const { dir, source } = resolveVaultDir({
      vault: settings.vault || null,
      projectRoot: root,
    })
    if (!dir) {
      status.lastError = 'could not resolve the cold-vault directory'
      writeStatus()
      return null
    }

    const vault = new ColdVault({ dir, onLog: log })
    const indexed = vault.index()
    vaults.set(sessionId, vault)
    status.vaultDir = dir
    status.projectRoot = root
    log(`vault ready: ${dir} (${indexed} existing pairs indexed, source=${source})`)
    if (status.sessionCwd) {
      const rel = path.relative(status.sessionCwd, dir)
      if (rel.startsWith('..') || path.isAbsolute(rel)) {
        log(`note: vault is outside the session workspace (cwd=${status.sessionCwd})`)
      }
    }
    writeStatus()
    return vault
  }

  /**
   * Store one pair.
   *
   * @returns {boolean} `true` when the pair is stored **or already present**
   *   (both are success), `false` when it could not be stored. The poller reads
   *   this: on `false` it rewinds its byte offset and replays the batch, so a
   *   temporarily unavailable vault no longer drops turns silently.
   */
  const record = (session, pair) => {
    // Counts completed turns handed over by the pairing state, i.e. the turns the
    // vault has been asked to store. Always `pairsWritten + pairsSkipped + pairsFailed`.
    status.turnsSeen += 1
    const vault = vaultFor(session)
    if (!vault) {
      status.pairsFailed += 1
      status.lastError = `vault unavailable (rootSource=${status.rootSource}, projectRoot=${status.projectRoot})`
      log(`cannot record: ${status.lastError}`)
      writeStatus()
      return false
    }
    const result = vault.write(pair)
    if (result.ok && result.skipped) {
      status.pairsSkipped += 1
    } else if (result.ok) {
      status.pairsWritten += 1
      status.lastWrite = {
        ref: result.ref,
        user: pair.user.slice(0, 80),
        assistantChars: pair.assistant.length,
        endReason: pair.meta?.endReason ?? null,
      }
    } else {
      status.pairsFailed += 1
      status.lastError = result.error ?? 'unknown write failure'
      log(`write failed: ${status.lastError}`)
      writeStatus()
      return false
    }
    writeStatus()
    return true
  }

  const pairingFor = (session) => {
    const sessionId = session?.id ?? 'unknown'
    let pairing = pairings.get(sessionId)
    if (!pairing) {
      pairing = new SessionPairing({
        maxAssistantChars: settings.maxAssistantChars,
        sessionId,
      })
      pairings.set(sessionId, pairing)
    }
    return pairing
  }

  /**
   * Persist everything a session is still holding.
   *
   * The pairing state runs one turn behind (see `./pairing.js`), so a pair is
   * normally released by the next `turn/start`. This is what releases the last
   * turn, which would otherwise never be written because no further turn arrives.
   */
  const finalize = (session, reason) => {
    const sessionId = session?.id ?? 'unknown'
    const pairing = pairings.get(sessionId)
    if (!pairing) return
    for (const pair of pairing.finish(reason)) record(session, pair)
  }

  /**
   * MAP-D §7：这个会话是不是子 Agent 的会话？
   *
   * dsh 给每个进程内子 agent 的会话头打了两个持久标记（见 dsh-subagent 的
   * `SessionHeader`）：`origin === 'subagent'` 标明委派来源，`parentSession`
   * 记录血缘。子会话里装的是工具 schema、调用参数与原始返回 —— 正是 MAP-D
   * §1.1 说的「管道污染」。这些不该进记忆库，只该由主 Agent 的结果摘要（L1）
   * 进入，所以这里直接判定并跳过。
   *
   * 防御式：header 读不到（dsh 可能只给受限的会话视图）时返回 false，行为与
   * 改动前完全一致 —— 不会因为读不到标记就把真实对话漏记。
   */
  const isSubagentSession = (session) => {
    const header = session?.header
    if (!header || typeof header !== 'object') return false
    return header.origin === 'subagent' || header.parentSession != null
  }

  // `session/created` and `session/disposed` are lifecycle hooks on the context,
  // not members of the persisted session-event vocabulary.
  ctx.on('session/created', () => {
    status.sessionsSeen += 1
    writeStatus()
  })

  // `{ global: true }` is REQUIRED here, not cosmetic.
  //
  // A session's `emitCtx` carries a scope filter, and Cordis dispatches through
  //   hooks.filter((hook) => hook.global || !filter || filter.call(thisArg, hook.ctx))
  // so a listener registered without `global` is dropped whenever the emitting
  // session lies outside this plugin's scope. Measured: without the flag the
  // plugin mounted and observed `session/created`, but `session/event` never
  // fired — `turnsSeen`/`vaultRequests` stayed 0 and the vault gained no rows.
  // Core packages that record sessions (e.g. dsh-tool-todo) pass the same flag.
  ctx.on('session/event', (session, event) => {
    try {
      // MAP-D §7：子 Agent 会话不沉淀进记忆库（工具过程留在子会话里，
      // 主 Agent 只收结果摘要 L1）。
      if (isSubagentSession(session)) {
        status.subagentEventsSkipped += 1
        // 只在首次跳过时刷一次状态：既让 subagentEventsSkipped 可被外部读到，
        // 又不会因为子会话事件多而频繁写文件。
        if (status.subagentEventsSkipped === 1) writeStatus()
        return
      }
      // `push` releases the previous turn when a new one starts, so the vault
      // trails by exactly one turn and never holds the turn in progress.
      const pair = pairingFor(session).push(event)
      if (pair) record(session, pair)
    } catch (error) {
      status.lastError = String(error)
      writeStatus()
      log(`event handling failed: ${String(error)}`)
    }
  }, { global: true })

  ctx.on('session/disposed', (session) => {
    const sessionId = session?.id
    if (!sessionId) return
    // Release the parked turn and any turn still open.
    finalize(session, 'disposed')
    pairings.delete(sessionId)
  }, { global: true })

  ctx.effect(() => () => {
    for (const [sessionId] of pairings) {
      finalize({ id: sessionId }, 'shutdown')
    }
    writeStatus()
    log(`stopped; wrote ${status.pairsWritten}, skipped ${status.pairsSkipped}, failed ${status.pairsFailed}`)
  })

  writeStatus()
  log(
    `mounted (vault=${settings.vault || 'auto'}, projectRoot=${settings.projectRoot || 'auto'}, ` +
    `maxAssistantChars=${settings.maxAssistantChars}, status=${statusFile})`,
  )

  // ---------------------------------------------------------------------
  // File-polling fallback.
  //
  // The event-bus path above is silently dead on the official desktop client:
  // `session/created` arrives but `session/event` never does, so nothing is
  // ever recorded. The artifact on disk is written regardless, so poll it.
  // Both paths write through `record()`, and because `ColdVault` dedups on
  // `pairHash(user, assistant)` a turn observed twice is stored once.
  // ---------------------------------------------------------------------
  if (settings.poll === false) {
    log('file polling disabled by configuration')
  } else {
    const pollStateFile = settings.pollStateFile || DEFAULT_POLL_STATE
    const pollRoot = settings.pollRoot || sessionsRoot()
    let poller
    try {
      poller = new SessionLogPoller({
        root: pollRoot,
        stateFile: pollStateFile,
        maxAssistantChars: settings.maxAssistantChars,
        quietMs: settings.pollQuietMs,
        lookbackMs: settings.pollLookbackMs,
        onLog: log,
        onPair: (pair, sessionKey) => {
          // A synthetic session stands in for the one the event bus never gave
          // us: `ensureRoot` reads only `.id`, and the vault keys on it.
          // ★ The return value matters: `false` tells the poller the write was
          // refused, so it replays the batch instead of advancing past it.
          return record({ id: sessionKey, name: `polled:${sessionKey}` }, pair)
        },
      })
    } catch (error) {
      status.lastError = `poller init failed: ${String(error)}`
      writeStatus()
      log(status.lastError)
    }
    if (poller) {
      const tick = () => {
        try {
          const result = poller.poll()
          status.pollFiles = result.files
          status.pollRecords = result.records
          status.pollEmitted = (status.pollEmitted ?? 0) + result.emitted
          status.pollSubagentSkipped = result.subagent
          status.pollSubagentArtifacts = result.subagentArtifacts
          status.pollFailed = result.failed
          status.lastPollAt = new Date().toISOString()
          // Surface the two states that used to be invisible: sub-agent
          // sessions kept out of the vault, and batches the sink refused (they
          // replay next pass, so a non-zero value means the run is degraded).
          if (result.emitted > 0 || result.failed > 0 || result.subagent > 0) writeStatus()
        } catch (error) {
          status.lastError = `poll failed: ${String(error)}`
          writeStatus()
          log(status.lastError)
        }
      }
      // Poll once at mount so a restart immediately picks up whatever the live
      // path missed while the plugin was down.
      tick()
      const timer = setInterval(tick, settings.pollIntervalMs)
      // Without `unref` a long-lived interval would keep the host process alive
      // and block exit; the host owns its own lifetime, not this plugin.
      timer.unref?.()
      ctx.effect(() => () => {
        clearInterval(timer)
        poller.saveState()
      })
      log(`file polling every ${settings.pollIntervalMs}ms (root=${pollRoot}, state=${pollStateFile})`)
    }
  }
}
