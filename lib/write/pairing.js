/**
 * Turn DSH session events into cold-memory conversation pairs.
 *
 * The event log is not a tidy chat transcript, so three rules matter:
 *
 * 1. **Only `source.kind === 'user'` is human input.** DSH injects runtime
 *    snapshots and compaction checkpoints as `user/message` events with
 *    `source.kind === 'plugin'`. In the sessions measured here those injections
 *    outnumbered real human messages 61 to 33 — storing them would fill the
 *    cold vault with system boilerplate.
 * 2. **One turn holds many assistant messages.** The agent emits one
 *    `assistant/message` per step (narration, tool use, reasoning), and only
 *    some carry `text` blocks. The reply to a human message is therefore the
 *    concatenation of every text block up to the end of that turn.
 * 3. **`turn/end` closes a pair.** A turn may also end with `max-tokens` or an
 *    abort, in which case whatever the assistant produced is still a real
 *    answer, so it is kept.
 *
 * `extractPairs` replays a finished log; `SessionPairing` consumes events one at
 * a time for the live listener. Both share the same rules.
 *
 * @module dsh-cold-memory-watcher/pairing
 */

/** Compaction checkpoints are injected as `user/message` with this prefix. */
const CHECKPOINT_PREFIX = 'This is an automatically generated checkpoint'

/** Runtime snapshots are injected as `user/message` with this prefix. */
const RUNTIME_PREFIX = 'Current runtime context.'

/** Default per-turn cap on the stored assistant reply. */
export const DEFAULT_MAX_ASSISTANT_CHARS = 8000

/**
 * Collect the visible text of a message payload.
 *
 * Only `text` blocks count: `reasoning` is private chain of thought, and
 * `tool_use`/`tool_result` are not conversation.
 *
 * @param {Array<{type?: string, text?: string}>} content
 * @returns {string}
 */
export function textOf (content) {
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
}

/**
 * Decide whether a `user/message` event is real human input.
 *
 * @param {object} event
 * @returns {boolean}
 */
export function isHumanUserMessage (event) {
  if (event?.type !== 'user/message') return false
  const data = event.data ?? {}
  if (data.source?.kind !== 'user') return false
  const text = textOf(data.content)
  if (text === '') return false
  // Belt and braces: injected messages have also appeared with kind 'user'.
  if (text.startsWith(CHECKPOINT_PREFIX) || text.startsWith(RUNTIME_PREFIX)) return false
  return true
}

/**
 * Join the assistant text blocks of one turn into a single reply.
 *
 * @param {string[]} parts
 * @param {number} maxChars - hard cap, so one runaway turn cannot bloat the vault.
 * @returns {{text: string, blocks: number, chars: number, truncated: boolean}}
 */
export function joinAssistantParts (parts, maxChars = DEFAULT_MAX_ASSISTANT_CHARS) {
  const kept = []
  let total = 0
  let truncated = false
  for (const part of parts) {
    const text = (part ?? '').trim()
    if (text === '') continue
    if (total + text.length > maxChars) {
      const room = maxChars - total
      if (room > 200) {
        kept.push(text.slice(0, room))
        total += room
      }
      truncated = true
      break
    }
    kept.push(text)
    total += text.length
  }
  return { text: kept.join('\n\n'), blocks: kept.length, chars: total, truncated }
}

/**
 * Stateful pairing for a single session's live event stream.
 *
 * **One turn behind.** A completed turn is not handed to the caller
 * immediately: it is parked in `pending` and only released when the *next* turn
 * begins (`advance`), or at teardown (`finish`). So while a session is live the
 * cold vault always trails by exactly one turn and never contains the turn in
 * progress — which is what keeps a half-finished turn out of the vault.
 *
 * `push` returns the pair (zero or one) that the event *released*, so the caller
 * decides what to do with it. Releasing is always paired with parking the
 * current turn, so a turn that has been released is never released twice.
 */
export class SessionPairing {
  /**
   * @param {object} [options]
   * @param {number} [options.maxAssistantChars]
   * @param {string} [options.sessionId]
   */
  constructor ({ maxAssistantChars = DEFAULT_MAX_ASSISTANT_CHARS, sessionId = null } = {}) {
    this.maxAssistantChars = maxAssistantChars
    this.sessionId = sessionId
    /** @type {{user: string, seq: unknown, turn: unknown, time: unknown}|null} */
    this.open = null
    /** @type {string[]} */
    this.parts = []
    /**
     * The last completed turn, waiting for the next turn to start before it is
     * handed over. Held explicitly so a reader can see the one-turn lag.
     * @type {{user: string, assistant: string, meta: object}|null}
     */
    this.pending = null
  }

  /** Build a pair from the open message, or `null` when there is no reply text. */
  _close (endReason) {
    if (this.open === null) return null
    const joined = joinAssistantParts(this.parts, this.maxAssistantChars)
    const open = this.open
    this.open = null
    this.parts = []
    if (joined.text === '') return null
    return {
      user: open.user,
      assistant: joined.text,
      meta: {
        sessionId: this.sessionId,
        userSeq: open.seq,
        turn: open.turn,
        userTime: open.time,
        endReason,
        assistantBlocks: joined.blocks,
        assistantChars: joined.chars,
        truncated: joined.truncated,
      },
    }
  }

  /**
   * Close the current turn, park it, and release the previously parked turn.
   *
   * @param {string} endReason
   * @param {boolean} [release=true] - `false` discards rather than releases the
   *   parked turn (used when a turn turned out to be a re-send of the same turn).
   * @returns {{user: string, assistant: string, meta: object}|null}
   */
  _rotate (endReason, release = true) {
    const closed = this._close(endReason)
    const released = release ? this.pending : null
    this.pending = closed
    return released
  }

  /**
   * Release the parked turn without an incoming turn/start.
   *
   * Called when a turn begins without a preceding `turn/start` (defensive), so a
   * missing lifecycle event can never strand a turn in `pending`.
   */
  advance () {
    return this._rotate('interrupted')
  }

  /**
   * Feed one event.
   *
   * @param {object} event
   * @returns {{user: string, assistant: string, meta: object}|null} a released pair.
   */
  push (event) {
    switch (event?.type) {
      case 'session':
        this.sessionId = event.id ?? this.sessionId
        return null

      case 'turn/start':
        return this._rotate('superseded')

      case 'user/message':
        if (!isHumanUserMessage(event)) return null
        // A human message with no preceding turn/start still means a new turn,
        // so release the parked one rather than stranding it.
        {
          const released = this._rotate('interrupted')
          this.open = {
            user: textOf(event.data?.content),
            seq: event.seq,
            turn: event.data?.turn ?? null,
            time: event.time ?? null,
          }
          this.parts = []
          return released
        }

      case 'assistant/message': {
        if (this.open === null) return null
        const text = textOf(event.data?.message?.content)
        if (text !== '') this.parts.push(text)
        return null
      }

      case 'turn/end':
        // Close the turn but keep it parked: the caller only receives it when the
        // next turn starts (see the class comment on the one-turn lag).
        return this._rotate(event.data?.reason?.kind ?? null)

      default:
        return null
    }
  }

  /**
   * Snapshot the mutable state so a caller can roll a batch back.
   *
   * Two callers need this:
   *  - the poller, when a whole batch of records produced a pair the vault
   *    refused: it rewinds its byte offset so the batch is replayed, and the
   *    pairing must go back with it or the replay would pair the wrong turns;
   *  - `saveState`, so a restart does not lose the parked turn (the one-turn lag
   *    means the last turn of a session lives only here until the next turn or
   *    the quiet timer releases it).
   *
   * `open`/`parts`/`pending` are copied by value: neither `_rotate` nor `push`
   * mutates them in place, but `parts` IS pushed to in place, so a shallow
   * reference is not enough.
   *
   * @returns {{open: object|null, parts: string[], pending: object|null}}
   */
  snapshot () {
    return {
      open: this.open ? { ...this.open } : null,
      parts: [...this.parts],
      pending: this.pending ? { ...this.pending } : null,
    }
  }

  /**
   * Restore a snapshot taken by {@link snapshot}.
   *
   * @param {{open?: object|null, parts?: string[], pending?: object|null}} snap
   */
  restore (snap) {
    this.open = snap?.open ? { ...snap.open } : null
    this.parts = Array.isArray(snap?.parts) ? [...snap.parts] : []
    this.pending = snap?.pending ? { ...snap.pending } : null
  }

  /**
   * Release everything still held, without parking anything new.
   *
   * Called when a session is disposed or the plugin is unloaded. The turn that
   * was parked waiting for a next turn, plus any turn still open, are both
   * released here — a live session's last turn would otherwise never be written.
   *
   * @param {string} [reason]
   * @returns {Array<{user: string, assistant: string, meta: object}>} zero to two pairs.
   */
  finish (reason = 'open') {
    const released = []
    if (this.pending !== null) {
      released.push(this.pending)
      this.pending = null
    }
    const openTail = this._close(reason)
    if (openTail) released.push(openTail)
    return released
  }
}

/**
 * Replay a finished event stream into pairs.
 *
 * Because `SessionPairing` runs one turn behind, the final parked turn is
 * released by `finish()` here — so a replay yields exactly the same set of pairs
 * the live path produces once that path is torn down.
 *
 * @param {object[]} events - decoded session records (`{type, seq, time, data}`).
 * @param {object} [options]
 * @param {number} [options.maxAssistantChars]
 * @param {string} [options.sourceFile] - origin artifact, copied into metadata.
 * @returns {Array<{user: string, assistant: string, meta: object}>}
 */
export function extractPairs (events, options = {}) {
  const pairing = new SessionPairing({
    maxAssistantChars: options.maxAssistantChars ?? DEFAULT_MAX_ASSISTANT_CHARS,
  })
  const pairs = []
  for (const event of events) {
    const pair = pairing.push(event)
    if (pair) pairs.push(pair)
  }
  pairs.push(...pairing.finish('open'))
  if (options.sourceFile) {
    for (const pair of pairs) pair.meta.sourceFile = options.sourceFile
  }
  return pairs
}
