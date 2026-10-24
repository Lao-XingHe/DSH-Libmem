/**
 * File-polling fallback for the cold-memory watcher.
 *
 * ## Why this exists
 *
 * The live path (`ctx.on('session/event', …, { global: true })`) only works when
 * the host delivers session events to plugins. Measured 2026-09-30 on the
 * official desktop client (Electron, bundled dsh 0.2.0-rc.2): the plugin mounts
 * and observes `session/created`, but `session/event` never arrives —
 * `turnsSeen`, `vaultRequests` and `subagentEventsSkipped` all stay at 0 while
 * the session log keeps growing on disk. So on that host nothing is ever
 * recorded, silently.
 *
 * This module sidesteps the event bus entirely and reads the artifact the host
 * *does* write. It is deliberately version-robust: it depends only on the
 * on-disk log format (concatenated zstd frames of JSONL records), not on any
 * plugin-facing event contract.
 *
 * ## How it stays cheap and duplicate-free
 *
 * DSH appends **one independent zstd frame per durable batch**, so a byte offset
 * is a valid resume point. Each poll reads only the bytes appended since the
 * last poll, scans them for structurally complete frames, and ignores a torn
 * trailing frame (that data is picked up once the frame completes — the offset
 * deliberately stays at the start of the incomplete frame).
 *
 * State is keyed **per artifact file**, not per session: one session directory
 * can hold both `session.v3.jsonl.zstd` and `session.v4.jsonl.zstd` after a
 * release upgrade, and keying by session would make them overwrite each other's
 * offset and re-read forever.
 *
 * Deduplication is layered, so a replay cannot double-write:
 *   1. the byte offset — normal path, nothing is re-read;
 *   2. the emitted-content set — guards a re-read of an already-consumed frame,
 *      keyed by a hash of the pair text (DSH `seq` values are NOT globally
 *      unique across artifacts, so they cannot serve as identity);
 *   3. `pairHash` inside `ColdVault` — content-addressed, so even a full replay
 *      is a no-op.
 *
 * ## The last turn
 *
 * `SessionPairing` runs one turn behind by design, releasing a parked turn only
 * when the next turn starts or the pairing is finished. A session that simply
 * stops (host dies, or the bus never reports `session/disposed`) would therefore
 * strand its final turn. When an artifact has been quiet for `quietMs`, the
 * poller finishes its pairing and records what was held.
 *
 * @module dsh-cold-memory-watcher/poller
 */

import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { SessionPairing } from './pairing.js'
import { sessionsRoot } from './session-log.js'
import { scanZstdFrames, decompressZstdFrame, decompressZstdPrefix } from './zstd-frames.js'

/** Session directory names are `session-<uuid>`. */
const SESSION_DIR = /^session-(.+)$/

/** How long an artifact must be untouched before its parked turn is released. */
const DEFAULT_QUIET_MS = 180000

/**
 * Derive the vault's session key from an artifact path.
 *
 * DSH's own writer names the session directory `session-<uuid>`, and the vault
 * stores that key verbatim so polled and event-sourced rows agree. Falls back to
 * the bare directory name for artifacts written by other tooling.
 *
 * @param {string} file - absolute path to a `session.v*.jsonl.zstd` artifact.
 * @returns {string} session key.
 */
export function sessionKeyOf (file) {
  const dir = path.basename(path.dirname(file))
  const match = SESSION_DIR.exec(dir)
  return match ? match[1] : dir
}

/**
 * Content identity of a pair, independent of which artifact it came from.
 *
 * @param {{user: string, assistant: string}} pair - a released pair.
 * @returns {string} hex digest.
 */
export function pairRef (pair) {
  return crypto.createHash('md5').update(`${pair.user}\u0000${pair.assistant}`).digest('hex')
}

/** Bytes read from the head of an artifact when probing for its session header. */
const HEADER_PROBE_BYTES = 1 << 20

/**
 * Read the `type === 'session'` header from the head of an artifact.
 *
 * The header lives in the **first** zstd frame, which is tiny (170 bytes in the
 * artifacts measured 2026-09-30), and it carries the two marks that say whether
 * the session belongs to a delegated sub-agent. Reading it once per artifact is
 * what lets the poller apply the same MAP-D §7 filter the event-bus path does.
 *
 * @param {string} file - artifact path.
 * @returns {object|null} the header record, or null when it cannot be read.
 */
export function readSessionHeader (file) {
  let fd
  try {
    fd = fs.openSync(file, 'r')
    const buf = Buffer.alloc(HEADER_PROBE_BYTES)
    const read = fs.readSync(fd, buf, 0, HEADER_PROBE_BYTES, 0)
    const chunk = buf.subarray(0, read)
    // `maxFrames: 1` — the scanner stops at the first complete frame, so this
    // never decompresses the rest of a multi-megabyte log.
    const { frames } = scanZstdFrames(chunk, 1)
    if (frames.length === 0) return null
    const records = splitRecords(decompressZstdFrame(chunk.subarray(frames[0].start, frames[0].end)))
    return records.find((r) => r?.type === 'session') ?? null
  } catch {
    return null
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd)
      } catch {
        /* closing a read handle cannot fail meaningfully here */
      }
    }
  }
}

/**
 * Is this artifact a delegated sub-agent's session?
 *
 * Mirrors `isSubagentSession` in `./index.js`: sub-agent sessions hold tool
 * schemas, call arguments and raw tool output — the "pipeline pollution" MAP-D
 * §7 keeps out of the memory library. Only the main agent's result summary (L1)
 * should land there.
 *
 * Defensive in the same direction as the event path: an unreadable header means
 * `false`, so a real conversation is never dropped just because the mark could
 * not be read.
 *
 * Measured 2026-09-30 (this is why the filter is required): of the 40 newest
 * artifacts, 31 were sub-agent sessions, and **every one of them produced a
 * pair** — a delegated task is injected as `user/message` with
 * `source.kind === 'user'`, so `isHumanUserMessage` cannot tell it apart. The
 * text is pure boilerplate (`"Read EXACTLY ONE file and nothing else: …"`).
 *
 * @param {string} file - artifact path.
 * @returns {boolean}
 */
export function isSubagentArtifact (file) {
  const header = readSessionHeader(file)
  if (!header) return false
  return header.origin === 'subagent' || header.parentSession != null
}

/**
 * Poll `<DSH_HOME>/sessions/**` for appended records and turn them into pairs.
 *
 * One instance owns the whole root (not one per session): it keeps a byte offset
 * and a released-content set per artifact in persistent state, so a restart
 * resumes exactly where it stopped.
 */
export class SessionLogPoller {
  /**
   * @param {object} options
   * @param {string} [options.root] - sessions root to scan; defaults to `<DSH_HOME>/sessions`.
   * @param {string} options.stateFile - JSON file holding per-artifact offsets.
   * @param {number} [options.maxAssistantChars] - per-turn reply cap.
   * @param {number} [options.quietMs] - idle time before a parked turn is released.
   * @param {(pair: object, key: string) => void} options.onPair - sink for released pairs.
   * @param {(message: string) => void} [options.onLog] - diagnostics sink.
   * @param {() => number} [options.now] - clock, injectable for tests.
   */
  constructor ({
    root = sessionsRoot(),
    stateFile,
    maxAssistantChars,
    quietMs = DEFAULT_QUIET_MS,
    lookbackMs = 0,
    onPair,
    onLog = () => {},
    now = Date.now,
  }) {
    this.root = root
    this.stateFile = stateFile
    this.maxAssistantChars = maxAssistantChars
    this.quietMs = quietMs
    /**
     * Only artifacts modified within this window are polled. `0` disables the
     * filter. Bounds the first run: a fresh state file would otherwise replay
     * every session ever recorded. Use `bin/backfill.mjs` for a deliberate
     * full history import.
     */
    this.lookbackMs = lookbackMs
    this.onPair = onPair
    this.onLog = onLog
    this.now = now
    /** @type {Map<string, object>} keyed by absolute artifact path. */
    this.files = new Map()
    /**
     * Content refs already emitted by **any** artifact.
     *
     * Per-file ref sets are not enough: one session directory holds both
     * `session.v3.jsonl.zstd` and `session.v4.jsonl.zstd` after a release
     * upgrade, and the turn covering the upgrade boundary appears in both.
     * Bounded because only recent content can be re-read.
     * @type {Set<string>}
     */
    this.emitted = new Set()
    /** Artifacts skipped this pass because they are sub-agent sessions. */
    this.skippedSubagent = 0
    /**
     * How many artifacts are known to be sub-agent sessions, cumulative.
     *
     * ⚠️ Derived in `loadState()` from the persisted flags, **not** left at 0.
     * A restarted instance restores `subagent: true` from the state file, so no
     * entry ever "flips" and a counter incremented only on flips reported **0**
     * after every restart while 379 artifacts were in fact excluded (measured
     * 2026-09-30) — exactly the kind of number that reads as "the filter is off"
     * while it is working.
     */
    this.subagentArtifacts = 0
    this.loadState()
  }

  /** Read persisted offsets; a missing or unreadable state file starts empty. */
  loadState () {
    try {
      const raw = JSON.parse(fs.readFileSync(this.stateFile, 'utf8'))
      for (const [file, value] of Object.entries(raw.files ?? {})) {
        this.files.set(file, this.newEntry(file, value))
      }
      for (const ref of raw.emitted ?? []) this.emitted.add(ref)
      // A persisted `subagent: true` is already-decided knowledge: count it now
      // so the cumulative figure survives a restart (see the field's comment).
      for (const entry of this.files.values()) {
        if (entry.subagent === true) this.subagentArtifacts += 1
      }
    } catch {
      /* no usable state: start from the beginning, the vault dedups anyway */
    }
  }

  /**
   * Build a tracking entry, optionally restoring persisted state.
   *
   * @param {string} file - artifact path.
   * @param {object} [value] - persisted fields (`offset`, `refs`, `finished`).
   * @returns {object} tracking entry.
   */
  newEntry (file, value = {}) {
    const pairs = new SessionPairing({
      maxAssistantChars: this.maxAssistantChars,
      sessionId: sessionKeyOf(file),
    })
    // Restore the parked turn. Without this a restart loses it: the byte offset
    // has already advanced past those records, so it can never be re-paired.
    if (value.pairing) pairs.restore(value.pairing)
    return {
      file,
      offset: Number(value.offset) || 0,
      finished: value.finished === true,
      // A persisted entry is already being followed, so the lookback window
      // must not drop it mid-session after a restart.
      tracked: value.tracked === true,
      /**
       * `true`/`false` once the artifact's header has been read, `null` while
       * unknown. `null` means "not yet probed", not "main session".
       * @type {boolean|null}
       */
      subagent: value.subagent === true ? true : (value.subagent === false ? false : null),
      pairs,
      refs: new Set(Array.isArray(value.refs) ? value.refs : []),
    }
  }

  /** Persist offsets atomically enough for a cache (tmp file, then rename). */
  saveState () {
    const files = {}
    for (const [file, entry] of this.files) {
      // The pairing snapshot holds the parked turn (`pending`) plus any turn
      // still open. It is what makes a restart lossless: the offset already
      // passed those records, so a turn held only in memory is gone for good.
      // Only written when non-empty, so 400+ idle artifacts do not bloat state.
      const snap = entry.pairs.snapshot()
      const held = (snap.pending !== null || snap.open !== null || snap.parts.length > 0)
        ? snap
        : undefined
      files[file] = {
        offset: entry.offset,
        finished: entry.finished,
        tracked: entry.tracked,
        subagent: entry.subagent,
        ...(held ? { pairing: held } : {}),
        // Bounded: only the recent tail can ever be re-read.
        refs: [...entry.refs].slice(-1000),
      }
    }
    const tmp = `${this.stateFile}.tmp`
    try {
      fs.mkdirSync(path.dirname(this.stateFile), { recursive: true })
      fs.writeFileSync(tmp, JSON.stringify({
        version: 3,
        files,
        emitted: [...this.emitted].slice(-50000),
      }, null, 2), 'utf8')
      fs.renameSync(tmp, this.stateFile)
    } catch (error) {
      this.onLog(`state save failed: ${String(error)}`)
    }
  }

  /** Collect every `session.v*.jsonl.zstd` artifact under the root. */
  discover () {
    const found = []
    const walk = (dir) => {
      let entries
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true })
      } catch {
        return
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name)
        if (entry.isDirectory()) walk(full)
        else if (entry.name.startsWith('session.') && entry.name.endsWith('.zstd')) found.push(full)
      }
    }
    walk(this.root)
    return found
  }

  /**
   * Decode the complete frames inside `buffer`, keeping a torn tail unread.
   *
   * @param {Buffer} buffer - bytes from the saved offset to EOF.
   * @returns {{records: object[], consumed: number}} decoded records and how
   *   many bytes are safe to skip (i.e. up to the last complete frame).
   */
  decodeFrames (buffer) {
    const { frames, tornStart } = scanZstdFrames(buffer)
    const parts = []
    let consumed = 0
    for (const { start, end } of frames) {
      try {
        parts.push(decompressZstdFrame(buffer.subarray(start, end)))
      } catch (error) {
        // An undecodable frame is not safe to skip: stop advancing here so the
        // next poll retries it.
        //
        // ⚠️ But the frames **before** it decoded fine and their records were
        // already turned into pairs by the previous pass of this loop — they
        // must still be handed over. This used to `return { records: [], ... }`,
        // which dropped `parts` while still advancing `consumed` to the end of
        // the last good frame; the caller then did `entry.offset += consumed`,
        // so every record before the corrupt frame was skipped **and** thrown
        // away. Measured 2026-09-30 on a 1.1 MB artifact: corrupting frame 3
        // lost frames 1–2 (1,260 records) with `records=0, consumed=1115943`.
        this.onLog(`frame decode failed at ${start}: ${String(error)}`)
        return { records: splitRecords(Buffer.concat(parts)), consumed }
      }
      consumed = end
    }
    // A torn frame holds real data that is not complete yet. `ZSTD_e_flush`
    // usually still yields readable plaintext, but we must NOT advance past it,
    // or the remainder would be lost once the frame completes.
    if (tornStart !== undefined && tornStart === consumed) {
      try {
        parts.push(decompressZstdPrefix(buffer.subarray(tornStart)))
      } catch {
        /* not recoverable yet; the next poll retries from the same offset */
      }
    }
    return { records: splitRecords(Buffer.concat(parts)), consumed }
  }

  /**
   * Read whatever is new in one artifact and hand released pairs to the sink.
   *
   * @param {string} file - artifact path.
   * @returns {{read: number, emitted: number}} decoded-record and emitted-pair counts.
   */
  pollFile (file) {
    let entry = this.files.get(file)
    if (!entry) {
      entry = this.newEntry(file)
      this.files.set(file, entry)
    }
    let stat
    try {
      stat = fs.statSync(file)
    } catch {
      return { read: 0, emitted: 0 }
    }
    // Skip artifacts untouched for longer than the lookback window, unless we
    // already track them (a tracked file must keep being followed to its end).
    if (this.lookbackMs > 0 && !entry.tracked && this.now() - stat.mtimeMs > this.lookbackMs) {
      return { read: 0, emitted: 0 }
    }
    entry.tracked = true

    // ---- ★ MAP-D §7：子 Agent 会话不沉淀进记忆库 ----------------------------
    // 事件总线那条路有 `isSubagentSession()`，读盘的这条路原来**没有** —— 而在这台
    // 桌面端上（`session/event` 不派发）它是**唯一**在跑的路，所以子会话会被整份
    // 写进冷库。实测 2026-09-30：最近 40 个 artifact 有 31 个是子会话，且**51/51
    // 全部产出 pair** —— 委派任务是以 `source.kind === 'user'` 注入的，
    // `isHumanUserMessage` 分辨不了，正文全是 "Read EXACTLY ONE file and
    // nothing else: …" 这类样板。
    // 每个 artifact 只探一次，结论写进状态（header 在第一帧里，读过就不必再读）。
    if (entry.subagent === null) {
      entry.subagent = isSubagentArtifact(file)
      if (entry.subagent) {
        // Cumulative: this is a property of the artifact, so it must be counted
        // once, not once per 15-second pass.
        this.subagentArtifacts += 1
        this.onLog(`${path.basename(file)}: sub-agent session, not recorded (MAP-D §7)`)
      }
    }
    if (entry.subagent) {
      this.skippedSubagent += 1
      return { read: 0, emitted: 0, skipped: true }
    }

    if (stat.size < entry.offset) {
      // Truncated or replaced: rescan from the start rather than trusting a
      // stale offset. The vault dedups whatever was already recorded.
      this.onLog(`${path.basename(file)}: shrank to ${stat.size}B (< offset ${entry.offset}); rescanning`)
      entry.offset = 0
      entry.pairs = new SessionPairing({
        maxAssistantChars: this.maxAssistantChars,
        sessionId: sessionKeyOf(file),
      })
      entry.refs = new Set()
    }

    // The batch starts here. The offset is advanced only after every pair in it
    // was either written or already present — a refused write replays the batch.
    const batchStart = entry.offset
    const wasFinished = entry.finished
    let records = []
    let consumed = 0
    const fresh = stat.size - entry.offset
    if (fresh > 0) {
      let buffer
      let fd
      try {
        fd = fs.openSync(file, 'r')
        buffer = Buffer.alloc(fresh)
        fs.readSync(fd, buffer, 0, fresh, entry.offset)
      } catch (error) {
        this.onLog(`${path.basename(file)}: read failed: ${String(error)}`)
        return { read: 0, emitted: 0 }
      } finally {
        if (fd !== undefined) {
          try {
            fs.closeSync(fd)
          } catch {
            /* closing a read handle cannot fail meaningfully here */
          }
        }
      }
      const decoded = this.decodeFrames(buffer)
      records = decoded.records
      consumed = decoded.consumed
    }

    const before = entry.pairs.snapshot()
    let emitted = 0
    let failed = false
    for (const record of records) {
      const pair = entry.pairs.push(record)
      if (!pair) continue
      const state = this.emit(entry, pair)
      if (state === 'emitted') emitted += 1
      else if (state === 'failed') failed = true
    }

    // A quiet artifact is not coming back: release the turn it parked, or the
    // final exchange of every finished session would never be recorded.
    const idleFor = this.now() - stat.mtimeMs
    let released = 0
    if (!entry.finished && idleFor >= this.quietMs) {
      const beforeQuiet = entry.pairs.snapshot()
      for (const pair of entry.pairs.finish('quiet')) {
        const state = this.emit(entry, pair)
        if (state === 'emitted') {
          emitted += 1
          released += 1
        } else if (state === 'failed') {
          failed = true
        }
      }
      if (failed) {
        // The parked turn must stay parked so the next pass tries again.
        entry.pairs.restore(beforeQuiet)
      } else {
        entry.finished = true
        if (released > 0) {
          this.onLog(`${path.basename(file)}: quiet ${Math.round(idleFor / 1000)}s; released parked turn`)
        }
      }
    } else if (entry.finished && fresh > 0) {
      // The session woke up again after we closed it out.
      entry.finished = false
    }

    if (failed) {
      // Whole-batch rewind: the offset does not move and the pairing goes back
      // to its pre-batch state, so the next pass replays exactly these records.
      // Replaying cannot double-write — `entry.refs`, `this.emitted` and the
      // vault's content-addressed `pairHash` are three independent layers.
      entry.pairs.restore(before)
      entry.finished = wasFinished
      entry.offset = batchStart
      this.onLog(`${path.basename(file)}: ${records.length} record(s) replayed next pass (sink refused)`)
      return { read: records.length, emitted, failed: true }
    }

    entry.offset = batchStart + consumed
    return { read: records.length, emitted }
  }

  /**
   * Hand one pair to the sink unless its content was already emitted.
   *
   * @param {object} entry - tracking entry owning the ref set.
   * @param {object} pair - released pair.
   * @returns {'emitted'|'duplicate'|'failed'} what happened. `failed` means the
   *   caller must not advance past this batch.
   */
  emit (entry, pair) {
    const ref = pairRef(pair)
    if (entry.refs.has(ref) || this.emitted.has(ref)) return 'duplicate'
    let ok = true
    try {
      // ⚠️ The sink reports failure by **returning false**, not by throwing:
      // `record()` in ./index.js catches its own errors and only bumps a
      // counter. Checking only for a throw (as this did) treated a refused
      // write as success, so the ref was recorded and the pair was never
      // retried — while the byte offset had already moved past it. That is a
      // silent, permanent loss; hence the return value is checked too.
      ok = this.onPair(pair, sessionKeyOf(entry.file)) !== false
    } catch (error) {
      this.onLog(`${path.basename(entry.file)}: sink failed: ${String(error)}`)
      ok = false
    }
    if (!ok) {
      this.onLog(`${path.basename(entry.file)}: sink refused the pair; batch will be replayed`)
      return 'failed'
    }
    entry.refs.add(ref)
    this.emitted.add(ref)
    return 'emitted'
  }

  /**
   * One pass over every artifact.
   *
   * @returns {{files: number, records: number, emitted: number, subagent: number,
   *   subagentArtifacts: number, failed: number}} totals.
   */
  poll () {
    let records = 0
    let emitted = 0
    let failed = 0
    this.skippedSubagent = 0
    const files = this.discover()
    for (const file of files) {
      const result = this.pollFile(file)
      records += result.read
      emitted += result.emitted
      if (result.failed) failed += 1
    }
    // Persist every pass: an offset that outlives a crash must reflect what was
    // actually handed to the sink, and refs are what make a re-read harmless.
    this.saveState()
    return {
      files: files.length,
      records,
      emitted,
      subagent: this.skippedSubagent,
      subagentArtifacts: this.subagentArtifacts,
      failed,
    }
  }
}

/**
 * Split concatenated JSONL plaintext into records, tolerating blank and torn lines.
 *
 * @param {Buffer} buffer - concatenated plaintext of the decoded frames.
 * @returns {object[]} parsed records.
 */
function splitRecords (buffer) {
  const records = []
  for (const line of buffer.toString('utf8').split('\n')) {
    const trimmed = line.trim()
    if (trimmed === '') continue
    try {
      records.push(JSON.parse(trimmed))
    } catch {
      /* a partial or malformed line is skipped, as in ./zstd-frames.js */
    }
  }
  return records
}
