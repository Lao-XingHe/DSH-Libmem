/**
 * Concatenated Zstandard frame reader for DSH `session.v3.jsonl.zstd` artifacts.
 *
 * DSH appends one independent, checksummed Zstandard frame per durable batch, so
 * the file on disk is a *concatenated* frame stream. Node's one-shot
 * `zstdDecompressSync` (and its stream wrapper) only understands a single frame
 * and fails with "Unknown frame descriptor" once a second frame header appears.
 *
 * The fix is to locate frame boundaries structurally — without decompressing
 * blocks — and then hand each complete frame to Node's public one-shot API,
 * which is exactly what DSH's own persistence backend does internally. The
 * scanner below is a faithful port of that backend's `scanZstdFrames`.
 *
 * @module dsh-cold-memory-watcher/zstd-frames
 */

import zlib from 'node:zlib'

/** Zstandard frame magic `28 B5 2F FD`, read as a little-endian uint32. */
const ZSTD_MAGIC = 0xfd2fb528

/**
 * Locate structurally complete frames in a concatenated Zstandard stream.
 *
 * @param {Buffer} buffer - complete bytes currently present in the artifact.
 * @param {number} [maxFrames] - optional complete-frame limit.
 * @returns {{ frames: { start: number, end: number }[], tornStart?: number }}
 *   complete frame ranges in file order, plus the start of a torn final frame.
 */
export function scanZstdFrames (buffer, maxFrames = Number.POSITIVE_INFINITY) {
  const frames = []
  let offset = 0
  while (offset < buffer.length) {
    const start = offset
    if (buffer.length - offset < 4) return { frames, tornStart: start }
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) {
      throw new Error(`corrupt Zstandard session log: invalid frame magic at byte ${offset}`)
    }
    offset += 4
    if (offset === buffer.length) return { frames, tornStart: start }
    const descriptor = buffer.readUInt8(offset)
    offset += 1
    if ((descriptor & 24) !== 0) {
      throw new Error(`corrupt Zstandard session log: reserved frame-header bit at byte ${offset - 1}`)
    }
    const contentSizeFlag = descriptor >>> 6
    const singleSegment = (descriptor & 32) !== 0
    const checksum = (descriptor & 4) !== 0
    const dictionaryFlag = descriptor & 3
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : 1 << contentSizeFlag
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes
    if (buffer.length - offset < remainingHeaderBytes) return { frames, tornStart: start }
    offset += remainingHeaderBytes
    for (;;) {
      if (buffer.length - offset < 3) return { frames, tornStart: start }
      const blockHeader = buffer.readUIntLE(offset, 3)
      offset += 3
      const lastBlock = (blockHeader & 1) !== 0
      const blockType = (blockHeader >>> 1) & 3
      const blockSize = blockHeader >>> 3
      if (blockType === 3) {
        throw new Error(`corrupt Zstandard session log: reserved block type at byte ${offset - 3}`)
      }
      const payloadBytes = blockType === 1 ? 1 : blockSize
      if (buffer.length - offset < payloadBytes) return { frames, tornStart: start }
      offset += payloadBytes
      if (lastBlock) break
    }
    if (checksum) {
      if (buffer.length - offset < 4) return { frames, tornStart: start }
      offset += 4
    }
    frames.push({ start, end: offset })
    if (frames.length === maxFrames) return { frames }
  }
  return { frames }
}

/**
 * Decompress one structurally complete frame with Node's public one-shot API.
 *
 * @param {Buffer} frame - exactly one complete Zstandard frame.
 * @returns {Buffer} the frame plaintext.
 */
export function decompressZstdFrame (frame) {
  return zlib.zstdDecompressSync(frame)
}

/**
 * Recover plaintext from a torn final frame (`ZSTD_e_flush` omits frame and
 * checksum completion), so a session being written right now still reads.
 *
 * @param {Buffer} frame - bytes from a known incomplete final frame.
 * @returns {Buffer} whatever plaintext was already flushed.
 */
export function decompressZstdPrefix (frame) {
  return zlib.zstdDecompressSync(frame, {
    finishFlush: zlib.constants.ZSTD_e_flush,
  })
}

/**
 * Read a full concatenated Zstandard artifact into one plaintext buffer.
 *
 * @param {Buffer} buffer - the raw file bytes.
 * @returns {Buffer} concatenated plaintext of every recoverable frame.
 */
export function readZstdLog (buffer) {
  const { frames, tornStart } = scanZstdFrames(buffer)
  const parts = frames.map(({ start, end }) => decompressZstdFrame(buffer.subarray(start, end)))
  if (tornStart !== undefined) {
    try {
      parts.push(decompressZstdPrefix(buffer.subarray(tornStart)))
    } catch {
      // A torn frame we cannot recover is simply the tail we do not have yet.
    }
  }
  return Buffer.concat(parts)
}

/**
 * Read a full artifact and split it into decoded JSONL records, tolerating
 * blank lines, a final partial line, and unparsable records.
 *
 * @param {Buffer} buffer - the raw file bytes.
 * @returns {{ records: unknown[], badLines: number }}
 */
export function readZstdRecords (buffer) {
  const text = readZstdLog(buffer).toString('utf8')
  const records = []
  let badLines = 0
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed === '') continue
    try {
      records.push(JSON.parse(trimmed))
    } catch {
      badLines += 1
    }
  }
  return { records, badLines }
}
