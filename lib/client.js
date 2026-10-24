/**
 * LibMem HTTP 客户端 —— 插件与 Python 桥之间**唯一**的通道。
 *
 * 为什么是 HTTP 而不是管道：dsh 的受限沙箱禁止以管道方式 spawn 子进程
 * （`EPERM`；只有 `inherit`/`ignore` 允许），所以 MCP stdio 那条路在沙箱下
 * 走不通。详见 `shufang-os/v2/services/C2_mcp_server/libmem_bridge.py` 的文件头。
 *
 * 错误分类是刻意做细的：`UNKNOWN_TOOL` / `INVALID_ARGS` 是**调用方**的问题，
 * `TOOL_FAILED` 是后端的问题，`UNREACHABLE` / `TIMEOUT` 是链路的问题。
 * 混成一个 "调用失败" 会让排障在「我名字写错了」和「服务没起来」之间来回猜。
 *
 * @module dsh-libmem/client
 */

/** LibMem 调用错误。 */
export class LibmemError extends Error {
  /**
   * @param {string} message - 面向模型的错误说明。
   * @param {{code?: string, status?: number|null, detail?: string}} [options]
   */
  constructor (message, options = {}) {
    super(message)
    this.name = 'LibmemError'
    /** @type {string} 机器可判的类别。 */
    this.code = options.code ?? 'LIBMEM_ERROR'
    /** @type {number|null} HTTP 状态码（链路层错误为 null）。 */
    this.status = options.status ?? null
    /** @type {string} 后端给的补充信息。 */
    this.detail = options.detail ?? ''
  }
}

/**
 * @param {string} host
 * @param {number} port
 * @returns {string} 服务基址。
 */
export function makeBaseUrl (host, port) {
  return `http://${host}:${port}`
}

/**
 * 探活。**任何**异常都返回 null —— 探活不该把调用方炸掉。
 *
 * @param {string} base - 服务基址。
 * @param {number} [timeoutMs]
 * @returns {Promise<object|null>} 健康报文；不是 LibMem 服务时也返回 null。
 */
export async function getHealth (base, timeoutMs = 3000) {
  try {
    const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) return null
    const payload = await response.json()
    // `service` 字段是**身份**校验：端口上蹲着别人的服务时不能当成自己人。
    return payload?.service === 'libmem' ? payload : null
  } catch {
    return null
  }
}

/**
 * 真探活模型服务（嵌入 / 重排）。
 *
 * `/health` 只证明**进程**活着；这一项证明**它算得动**。存在的理由是一条实测坑
 * （移植自 LibMem-AML）：「/health 报 embed_reachable=True 而实际不可用 ——
 * 只探了端口，没真调」。而 `phase2_rank` 的 fail-open 护栏生效后，嵌入挂掉
 * **不再表现为「查不到」**，而是「查到了但全是噪声」，从响应上更难分辨。
 *
 * @param {string} base - 服务基址。
 * @param {number} [timeoutMs]
 * @returns {Promise<object|null>} 探活结果；桥不提供该端点（旧版）时返回 null。
 */
export async function probeModels (base, timeoutMs = 8000) {
  try {
    const response = await fetch(`${base}/health/models`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  }
}

/**
 * 调一个工具。
 *
 * @param {string} base - 服务基址。
 * @param {string} name - Python 侧工具名（如 `memory_search`）。
 * @param {object} args - 工具参数。
 * @param {number} timeoutMs - 单次调用上限。
 * @param {AbortSignal} [signal] - 调用方（工具执行）的中断信号。
 * @returns {Promise<{result: any, elapsedMs: number|null}>}
 * @throws {LibmemError}
 */
export async function callTool (base, name, args, timeoutMs, signal) {
  const timeout = AbortSignal.timeout(timeoutMs)
  const composed = signal ? AbortSignal.any([signal, timeout]) : timeout

  let response
  try {
    response = await fetch(`${base}/tool`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name, args: args ?? {} }),
      signal: composed,
    })
  } catch (error) {
    const timedOut = error?.name === 'TimeoutError' || timeout.aborted
    const aborted = signal?.aborted === true
    if (aborted && !timedOut) {
      throw new LibmemError(`${name} 已被调用方取消`, { code: 'ABORTED' })
    }
    throw new LibmemError(
      timedOut
        ? `${name} 超时（${timeoutMs}ms）—— 记忆检索的冷启动（bge 缓存）可能还没热`
        : `连不上 LibMem 服务（${base}）：${error?.message ?? String(error)}`,
      { code: timedOut ? 'TIMEOUT' : 'UNREACHABLE' },
    )
  }

  let payload = null
  try {
    payload = await response.json()
  } catch {
    /* 非 JSON 响应统一在下面按状态码报错 */
  }

  if (!response.ok || payload?.ok !== true) {
    const detail = payload?.error ?? `HTTP ${response.status}`
    const code = response.status === 404
      ? 'UNKNOWN_TOOL'
      : response.status === 400 ? 'INVALID_ARGS' : 'TOOL_FAILED'
    throw new LibmemError(`${name} 失败：${detail}`, {
      code,
      status: response.status,
      detail: payload?.errorType ?? '',
    })
  }

  return { result: payload.result, elapsedMs: payload.elapsedMs ?? null }
}
