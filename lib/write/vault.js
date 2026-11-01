/**
 * Cold-vault writer: appends conversation pairs in the B1 on-disk format.
 *
 * Why this is implemented in Node rather than by shelling out to
 * `services/B1_cold_memory/cold_watcher.py`:
 *
 * The DSH file sandbox forbids `child_process.spawn` with piped stdio
 * (`EPERM`; only `'inherit'`/`'ignore'` are permitted), so a long-lived
 * Node↔Python bridge that exchanges JSON over pipes is not deployable here.
 * Spawning one Python process per conversation would work but costs ~200ms each
 * and adds a failure mode for every write.
 *
 * The format is therefore duplicated deliberately, and kept honest two ways:
 *
 * 1. Path resolution mirrors `services/shared/config/loader.py` exactly — the
 *    vault is found by walking up for the nearest `config.yaml`, not by
 *    hardcoding, so an explicit `vault` config and a `SHUFANG_DATA_DIR`
 *    environment override are still respected.
 * 2. `bin/verify-b1.mjs` reads what this module wrote back through B1's own
 *    `read_cold_memory()` / `get_cold_status()`, so a format drift fails loudly
 *    instead of silently producing a vault B2 cannot consume.
 *
 * Schema written (identical to `cold_watcher.record_conversation`):
 *
 *     {"id": "HHMMSSuuuuuu", "time": "<ISO8601>", "user": "...", "assistant": "..."}
 *
 * one JSON object per line in `<vault>/<local YYYY-MM-DD>.jsonl`.
 *
 * @module dsh-cold-memory-watcher/vault
 */

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

/**
 * Stable identity of a conversation pair, used to make replay idempotent.
 *
 * Mirrors the design of `B4.embed_cache.make_key`: content-addressed, so
 * re-ingesting the same session (or re-running a backfill) never duplicates an
 * entry, and no cursor bookkeeping is required.
 *
 * @param {string} userText
 * @param {string} assistantText
 * @returns {string} 32 hex chars.
 */
export function pairHash (userText, assistantText) {
  return createHash('sha256')
    .update(userText.trim(), 'utf8')
    .update('\u0000', 'utf8')
    .update(assistantText.trim(), 'utf8')
    .digest('hex')
    .slice(0, 32)
}

/**
 * Explicit hints for the Shufang-OS root, tried before any directory walk.
 *
 * The session cwd is the DSH workspace (`<DSH_WORKSPACE>`), which sits on a
 * different drive from the Shufang-OS project, so walking up from it can never
 * find the project — measured: `resolveProjectRoot('<DSH_WORKSPACE>')` is
 * `null`. These hints are what make auto-detection work at all; the walk is only
 * the last resort.
 *
 * @returns {{root: string|null, source: string|null}}
 */
export function shufangRootHint () {
  for (const name of ['SHUFANG_V2_ROOT', 'SHUFANG_ROOT']) {
    const value = process.env[name]
    if (value && fs.existsSync(path.join(value, 'config.yaml'))) {
      return { root: path.resolve(value), source: `env.${name}` }
    }
  }

  // The profile patch already states the Shufang-OS path in the `mcp-xilian`
  // entry's `cwd`, so read it from there instead of duplicating the location.
  // A regex suffices for this one key and keeps the plugin dependency-free.
  const dshHome = process.env.DSH_HOME ?? path.join(os.homedir(), '.dsh')
  const patch = path.join(dshHome, 'profiles', 'web', 'cordis.patch.yml')
  try {
    const text = fs.readFileSync(patch, 'utf8')
    for (const match of text.matchAll(/^\s*cwd:\s*['"]?([^'"\r\n]+?)['"]?\s*$/gm)) {
      const candidate = match[1].trim()
      if (candidate && fs.existsSync(path.join(candidate, 'config.yaml'))) {
        return { root: path.resolve(candidate), source: 'cordis.patch.yml cwd' }
      }
    }
  } catch {
    /* not readable: fall through */
  }

  // Last resort: look for a Shufang-OS checkout in the usual places. Only a
  // `config.yaml` presence counts, so this cannot adopt a stray directory.
  const bases = [
    path.join(os.homedir(), 'Documents', 'trae_projects', 'admin'),
    path.join(os.homedir(), 'Documents', 'trae_projects'),
    path.join(os.homedir(), 'Documents'),
  ]
  for (const base of bases) {
    let entries
    try {
      entries = fs.readdirSync(base, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      if (!entry.isDirectory() || !entry.name.includes('shufang')) continue
      const direct = path.join(base, entry.name)
      for (const candidate of [path.join(direct, 'v2'), direct]) {
        if (fs.existsSync(path.join(candidate, 'config.yaml'))) {
          return { root: candidate, source: `scan(${base})` }
        }
      }
    }
  }

  return { root: null, source: null }
}

/**
 * Locate the Shufang-OS project root.
 *
 * Order: `SHUFANG_DATA_DIR`, then the explicit hints in {@link shufangRootHint},
 * then the nearest ancestor of `startDir` containing `config.yaml`.
 *
 * @param {string} [startDir] - directory to start walking up from.
 * @returns {string|null}
 */
export function resolveProjectRoot (startDir = process.cwd()) {
  const override = process.env.SHUFANG_DATA_DIR
  if (override) return path.resolve(override)

  const hint = shufangRootHint()
  if (hint.root) return hint.root

  let here = path.resolve(startDir)
  for (;;) {
    if (fs.existsSync(path.join(here, 'config.yaml'))) return here
    const parent = path.dirname(here)
    if (parent === here) return null
    here = parent
  }
}

/**
 * Resolve the cold-vault directory: explicit config wins, then the same
 * `paths.cold_vault` (relative to the project root) that B1 would use.
 *
 * @param {object} options
 * @param {string} [options.vault] - explicit vault directory.
 * @param {string} [options.projectRoot] - Shufang-OS root.
 * @returns {{dir: string|null, source: string}}
 */
export function resolveVaultDir ({ vault, projectRoot } = {}) {
  if (vault) return { dir: path.resolve(vault), source: 'config.vault' }
  const root = projectRoot ?? resolveProjectRoot()
  if (!root) return { dir: null, source: 'unresolved' }
  return { dir: path.join(root, 'data', 'cold_vault'), source: `projectRoot(${root})` }
}

/** Local-time `YYYY-MM-DD`, matching B1's `datetime.now().strftime`. */
function localDate (date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/**
 * Process-wide counter for the low three digits of the id.
 *
 * ⚠️ It **must be module-global, not per-`ColdVault`.** B1's id is `HHMMSS` plus
 * six digits, and the clock only supplies the millisecond (`ms * 1000`) — the low
 * three digits disambiguate the rest. A per-instance salt cannot do that:
 * measured 2026-09-30, a poller backfill ran **six** vault instances at once and
 * two of them minted the same `135504712001` in the same millisecond for
 * **different** conversations (`"git clone …"` and `"按你的意见修吧"`).
 *
 * Downstream identity is `md5("{date}#{id}")[:12]` (B3 `memory_id_for`), so a
 * collision silently merges two conversations into one row in `header.db`.
 */
let idSeq = 0

/**
 * B1's 12-char id: `HHMMSS` + 6 digits of sub-second precision.
 *
 * The last three digits come from {@link idSeq} rather than the clock, which is
 * what keeps two writes in the same millisecond distinct even across instances.
 *
 * Exported for `bin/verify-poller.mjs`: the collision it prevents can only be
 * asserted deterministically by pinning the clock, which `write()` does not allow.
 */
export function entryId (date = new Date()) {
  const pad = (n, w = 2) => String(n).padStart(w, '0')
  const micros = date.getMilliseconds() * 1000 + (idSeq++ % 1000)
  return `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}${pad(micros, 6)}`
}

/**
 * Hashes of the pairs already in one day-file, keyed by that file's size.
 *
 * ⚠️ **为什么需要它（2026-10-12 实测补的洞）**：`known` 是**进程内**集合，
 * 在 `index()` 时一次性建立。两个实例（计划任务 + DSH 插件，或插件的两次热重载）
 * 若都在对方写盘**之前**建好了 `known`，两边就都会认为"这条没写过"，各写一份。
 * 实测抓到：同一条对话的 `user+assistant` **逐字节相同**、`id` 不同、时间差 8 小时
 * （`123242750001` / `204308779002`）——单进程集合对它无能为力。
 *
 * 做法：落盘**之前**重读目标日文件算一次哈希集，按 `路径 + 文件大小` 缓存
 * （文件是 append-only，大小不变 ⇒ 内容不变）。代价是当天文件最多多读一次。
 */
const dayFileHashes = new Map()

/** 当前 `path` 里所有 pair 的哈希集合（缓存键 = 路径 + 大小）。 */
function hashesInDayFile (file) {
  let size = -1
  try {
    size = fs.statSync(file).size
  } catch {
    return new Set()                     // 文件还不存在：没有任何已写记录
  }
  const key = `${file}\u0000${size}`
  const cached = dayFileHashes.get(key)
  if (cached) return cached

  const hashes = new Set()
  try {
    const raw = fs.readFileSync(file, 'utf8')
    for (const line of raw.split('\n')) {
      const trimmed = line.trim()
      if (trimmed === '') continue
      try {
        const entry = JSON.parse(trimmed)
        const user = (entry.user ?? '').trim()
        const assistant = (entry.assistant ?? '').trim()
        if (user && assistant) hashes.add(pairHash(user, assistant))
      } catch {
        /* 坏行不是本写入者的事 */
      }
    }
  } catch {
    return new Set()
  }
  dayFileHashes.set(key, hashes)
  return hashes
}

/**
 * Append-only writer for one vault directory.
 *
 * Holds the set of pair hashes already present so a replay is a no-op, and
 * writes with a single `appendFileSync` per record so a crash mid-backfill
 * leaves a valid JSONL file.
 */
export class ColdVault {
  /**
   * @param {object} options
   * @param {string} options.dir - vault directory (created if absent).
   * @param {(msg: string) => void} [options.onLog]
   */
  constructor ({ dir, onLog = () => {} }) {
    this.dir = dir
    this.onLog = onLog
    this.known = new Set()
    // No per-instance id counter here: the disambiguating digits are
    // process-wide (`entryId`), because two instances writing in the same
    // millisecond used to mint the same id. See `entryId` above.
    this.stats = { written: 0, skipped: 0, failed: 0 }
    this.indexed = false
  }

  /** Build the dedup set from every JSONL already in the vault. */
  index () {
    if (this.indexed) return this.known.size
    this.indexed = true
    if (!fs.existsSync(this.dir)) return 0
    for (const name of fs.readdirSync(this.dir)) {
      if (!name.endsWith('.jsonl')) continue
      let raw
      try {
        raw = fs.readFileSync(path.join(this.dir, name), 'utf8')
      } catch {
        continue
      }
      for (const line of raw.split('\n')) {
        const trimmed = line.trim()
        if (trimmed === '') continue
        try {
          const entry = JSON.parse(trimmed)
          const user = (entry.user ?? '').trim()
          const assistant = (entry.assistant ?? '').trim()
          if (user && assistant) this.known.add(pairHash(user, assistant))
        } catch {
          /* a pre-existing bad line is not this writer's business */
        }
      }
    }
    return this.known.size
  }

  /**
   * Append one conversation pair unless an identical pair is already stored.
   *
   * @param {{user: string, assistant: string}} pair
   * @returns {{ok: boolean, skipped?: boolean, ref?: string, error?: string}}
   */
  write ({ user, assistant }) {
    const userText = (user ?? '').trim()
    const assistantText = (assistant ?? '').trim()
    if (userText === '' || assistantText === '') {
      this.stats.skipped += 1
      return { ok: true, skipped: true, reason: 'empty' }
    }
    this.index()
    const hash = pairHash(userText, assistantText)
    if (this.known.has(hash)) {
      this.stats.skipped += 1
      return { ok: true, skipped: true, reason: 'duplicate' }
    }
    const now = new Date()
    const date = localDate(now)
    const file = path.join(this.dir, `${date}.jsonl`)

    // ★ 2026-10-12：**跨进程**第二道去重 —— 落盘前重读今天的文件。
    //   第一道（`this.known`）只在**本进程内**有效，实测被两个实例同时跑穿透了
    //   （见 `hashesInDayFile` 的注释）。这一道读的是磁盘上真实的现状，
    //   所以即使两个实例并发，后写的那一个也会在这里被拦下。
    if (hashesInDayFile(file).has(hash)) {
      this.stats.skipped += 1
      this.stats.duplicateBlocked = (this.stats.duplicateBlocked ?? 0) + 1
      return { ok: true, skipped: true, reason: 'duplicate-on-disk' }
    }

    const id = entryId(now)
    const entry = {
      id,
      time: now.toISOString(),
      user: userText,
      assistant: assistantText,
    }
    try {
      fs.mkdirSync(this.dir, { recursive: true })
      fs.appendFileSync(file, JSON.stringify(entry) + '\n', 'utf8')
    } catch (error) {
      this.stats.failed += 1
      return { ok: false, error: String(error) }
    }
    this.known.add(hash)
    // 刚写过的那份也要进缓存，否则下一次同进程写入会白读一遍文件
    const sizeKey = (() => {
      try {
        return `${file}\u0000${fs.statSync(file).size}`
      } catch {
        return null
      }
    })()
    if (sizeKey) {
      const set = dayFileHashes.get(sizeKey) ?? hashesInDayFile(file)
      set.add(hash)
      dayFileHashes.set(sizeKey, set)
    }
    this.stats.written += 1
    return { ok: true, skipped: false, ref: `${date}#${id}` }
  }
}
