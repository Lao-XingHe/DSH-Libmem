/**
 * Discovery and decoding of DSH session artifacts on disk.
 *
 * DSH persists each session as a concatenated-zstd JSONL log:
 *
 *     <DSH_HOME>/sessions/<escaped-cwd>/<session-id>/session.v3.jsonl.zstd
 *
 * This module locates those artifacts and reads them into decoded event
 * records, relying on `./zstd-frames.js` for the multi-frame format. It is used
 * both by the live plugin (to replay what happened before it was mounted) and
 * by `bin/backfill.mjs`.
 *
 * @module dsh-cold-memory-watcher/session-log
 */

import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { readZstdRecords } from './zstd-frames.js'

/** Default DSH home (`DSH_HOME`, else `~/.dsh`). */
export function dshHome () {
  return process.env.DSH_HOME ?? path.join(os.homedir(), '.dsh')
}

/** Root directory holding every session project folder. */
export function sessionsRoot () {
  return path.join(dshHome(), 'sessions')
}

/**
 * Recursively collect every `session.v3.jsonl.zstd` artifact.
 *
 * @param {object} [options]
 * @param {string} [options.root] - sessions root override.
 * @returns {Array<{file: string, sessionId: string, project: string, size: number, mtimeMs: number}>}
 */
export function discoverSessions ({ root = sessionsRoot() } = {}) {
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
      if (entry.isDirectory()) {
        walk(full)
      } else if (entry.name.endsWith('.zstd')) {
        let stat
        try {
          stat = fs.statSync(full)
        } catch {
          continue
        }
        found.push({
          file: full,
          sessionId: path.basename(path.dirname(full)),
          project: path.basename(dir),
          size: stat.size,
          mtimeMs: stat.mtimeMs,
        })
      }
    }
  }
  walk(root)
  found.sort((a, b) => b.mtimeMs - a.mtimeMs)
  return found
}

/**
 * Decode one artifact into event records.
 *
 * @param {string} file
 * @returns {Promise<{events: object[], badLines: number, bytes: number, sessionId: string|null, cwd: string|null}>}
 */
export async function readSession (file) {
  const bytes = await fs.promises.readFile(file)
  const { records, badLines } = readZstdRecords(bytes)
  const header = records.find((r) => r?.type === 'session')
  return {
    events: records,
    badLines,
    bytes: bytes.length,
    sessionId: header?.id ?? path.basename(path.dirname(file)),
    cwd: header?.cwd ?? null,
  }
}

/**
 * Read every discovered session, newest first, tolerating unreadable files.
 *
 * @param {object} [options]
 * @param {string} [options.root] - sessions root override.
 * @param {number} [options.limit] - stop after this many sessions.
 * @param {(msg: string) => void} [options.onLog]
 * @returns {Promise<Array<{file: string, sessionId: string, cwd: string|null, events: object[], badLines: number}>>}
 */
export async function readAllSessions ({ root, limit = Infinity, onLog = () => {} } = {}) {
  const out = []
  for (const entry of discoverSessions({ root })) {
    if (out.length >= limit) break
    try {
      const session = await readSession(entry.file)
      out.push({ file: entry.file, ...session })
    } catch (error) {
      onLog(`skip ${entry.file}: ${String(error)}`)
    }
  }
  return out
}
