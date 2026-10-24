#!/usr/bin/env node
/**
 * dsh-libmem 自检。
 *
 * 覆盖三件在真挂载之前就能查清的事：
 *
 *   1. **写入侧副本没漂**（`--check-copy`）：6 个文件与上游
 *      `dsh-cold-memory-watcher/lib` 逐字节比对，index.js 只允许存在
 *      `lib/write/README.md` 里那 4 处文档化改动。
 *   2. **配置与工具面能编译**：用假 ctx（只要 `tools.register` / `on` / `effect`）
 *      跑一遍 `apply()`，让 `defineTool` 真正编译 16 个工具的 JSON Schema ——
 *      这能在挂载到运行中的 dsh **之前**抓到 schema 写错。
 *   3. **端到端调得通**：真起一个 Python 桥（或复用 `--reuse` 指定的），
 *      逐个调用代表性工具。
 *
 * 用法：
 *   node bin/verify-libmem.mjs                 # 全量（自起桥，端口 8770，跑完停掉）
 *   node bin/verify-libmem.mjs --port 8768 --reuse   # 只连已有的服务（不动进程）
 *   node bin/verify-libmem.mjs --check-copy    # 只做副本比对
 *
 * ★ 自检**不碰**线上状态：状态/日志/写入侧心跳都写到 `.verify/`，不用包根那几个
 *   真文件（否则跑一次自检会把正在运行的插件的 `watch-status.json` 冲成 0/0）。
 */
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PACKAGE = path.join(HERE, '..')
const WRITE_DIR = path.join(PACKAGE, 'lib', 'write')
const UPSTREAM_DIR = 'D:\\Deepseek Harness\\dsh-cold-memory-watcher\\lib'
/** 自检自己的状态目录（绝不写包根的真状态文件）。 */
const STATE_DIR = path.join(PACKAGE, '.verify')

const argv = process.argv.slice(2)
const has = (flag) => argv.includes(flag)
const option = (flag, fallback) => {
  const index = argv.indexOf(flag)
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback
}

let failures = 0
function check (ok, label, detail = '') {
  console.log(`${ok ? '✅' : '❌'} ${label}${detail ? `  — ${detail}` : ''}`)
  if (!ok) failures += 1
}

const sha256 = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex')

/** 写入侧副本比对（见 lib/write/README.md）。 */
function checkCopy () {
  console.log('\n== 1. 写入侧副本比对 ==')
  const identical = ['pairing.js', 'poller.js', 'session-log.js', 'vault.js', 'zstd-frames.js']
  for (const file of identical) {
    const ours = path.join(WRITE_DIR, file)
    const theirs = path.join(UPSTREAM_DIR, file)
    if (!fs.existsSync(theirs)) {
      check(false, `${file} 上游不存在`, theirs)
      continue
    }
    check(sha256(ours) === sha256(theirs), `${file} 与上游逐字节一致`)
  }

  // index.js：允许头注释 + 4 处字面量改动，其余必须与上游相同。
  const ours = fs.readFileSync(path.join(WRITE_DIR, 'index.js'), 'utf8')
  const theirs = fs.readFileSync(path.join(UPSTREAM_DIR, 'index.js'), 'utf8')
  const stripHeader = (text) => text.slice(text.indexOf('*/') + 2)
  const expected = stripHeader(theirs)
    .replace("export const name = 'cold-memory-watcher'", "export const name = 'libmem-write'")
    .replaceAll("path.join(import.meta.dirname, '..', 'watch-status.json')",
      "path.join(import.meta.dirname, '..', '..', 'watch-status.json')")
    .replaceAll("path.join(import.meta.dirname, '..', 'poll-state.json')",
      "path.join(import.meta.dirname, '..', '..', 'poll-state.json')")
    .replace('`[cold-memory-watcher] ${message}\\n`', '`[libmem] ${message}\\n`')
  check(stripHeader(ours) === expected, 'index.js 只含文档化的 4 处改动（其余与上游相同）')
}

/** 假 ctx：足够跑通 apply()，把工具 schema 真正编译一遍。 */
function fakeContext () {
  const tools = new Map()
  const contexts = new Map()
  const handlers = new Map()
  const routes = new Map()
  const taps = []
  const disposers = []
  const ctx = {
    tools: {
      register (definition) {
        if (tools.has(definition.name)) throw new Error(`重复注册工具 ${definition.name}`)
        tools.set(definition.name, definition)
        const dispose = () => tools.delete(definition.name)
        disposers.push(dispose)
        return dispose
      },
    },
    systemPrompt: {
      context (definition) {
        if (contexts.has(definition.name)) throw new Error(`重复注册上下文 ${definition.name}`)
        contexts.set(definition.name, definition)
        const dispose = () => contexts.delete(definition.name)
        disposers.push(dispose)
        return dispose
      },
    },
    webServer: {
      register (route) {
        const key = `${route.kind} ${route.path}`
        if (routes.has(key)) throw new Error(`重复注册路由 ${key}`)
        routes.set(key, route)
        const dispose = () => routes.delete(key)
        disposers.push(dispose)
        return dispose
      },
      tapIndex (fn) {
        taps.push(fn)
        const dispose = () => {
          const i = taps.indexOf(fn)
          if (i >= 0) taps.splice(i, 1)
        }
        disposers.push(dispose)
        return dispose
      },
    },
    inject (_deps, callback) {
      callback(ctx)
      return () => {}
    },
    on (type, handler) {
      const list = handlers.get(type) ?? []
      list.push(handler)
      handlers.set(type, list)
      return () => {}
    },
    effect (fn) {
      const dispose = fn()
      if (typeof dispose === 'function') disposers.push(dispose)
      return () => {}
    },
    get () { return undefined },
    logger: { info () {}, warn () {}, error () {} },
  }
  return {
    ctx,
    tools,
    contexts,
    handlers,
    routes,
    taps,
    disposeAll: () => { for (const d of disposers.reverse()) { try { d() } catch {} } },
  }
}

/** 假 req：够面板路由用（headers / method / url / 一次性 body）。 */
function fakeReq (opts = {}) {
  const req = {
    method: opts.method ?? 'GET',
    url: opts.url ?? '/',
    headers: { host: '127.0.0.1:19387', ...(opts.headers ?? {}) },
    _h: {},
    on (event, fn) { (this._h[event] ||= []).push(fn); return this },
    destroy () {},
  }
  queueMicrotask(() => {
    if (opts.body !== undefined) for (const fn of req._h.data ?? []) fn(Buffer.from(opts.body))
    for (const fn of req._h.end ?? []) fn()
  })
  return req
}

/** 假 res：把状态码与正文收下来。 */
function fakeRes () {
  return {
    statusCode: 0,
    headers: null,
    body: '',
    writeHead (code, headers) { this.statusCode = code; this.headers = headers },
    end (body) { if (body) this.body += Buffer.isBuffer(body) ? body.toString('utf8') : String(body) },
  }
}

/** 轮询进程是否还活着。 */
function alive (pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function main () {
  if (has('--check-copy')) {
    checkCopy()
    console.log(`\n${failures === 0 ? '全部通过' : `${failures} 项失败`}`)
    return failures === 0 ? 0 : 1
  }

  checkCopy()

  console.log('\n== 2. 模块与配置 ==')
  const mod = await import(pathToFileURL(path.join(PACKAGE, 'lib', 'index.js')).href)
  check(mod.name === 'libmem', '导出 name = libmem')
  check(Array.isArray(mod.inject) && mod.inject.includes('tools'), '导出 inject 含 tools')
  check(typeof mod.apply === 'function', '导出 apply()')
  check(mod.Config && typeof mod.Config['~standard']?.validate === 'function',
    "导出 Config 且带 '~standard'.validate（Cordis 的解析入口）")

  const empty = mod.Config['~standard'].validate({})
  // schemastery 的 `~standard` 接口：成功时只有 `value`，失败时才有 `issues`。
  check(empty.value !== undefined && empty.issues === undefined,
    `Config 接受空配置（全部走默认值，含嵌套 write：${Object.keys(empty.value?.write ?? {}).length} 个键）`,
    JSON.stringify(empty.issues ?? ''))
  const emptyWrite = empty.value?.write ?? {}
  check(emptyWrite.enabled === true && emptyWrite.poll === true,
    '未提供 write 时嵌套默认值仍成立')
  const partial = mod.Config['~standard'].validate({ port: 8771, write: { poll: false } })
  check(partial.value?.port === 8771 && partial.value?.write?.poll === false &&
    partial.value?.write?.statusFile !== undefined,
  'Config 支持局部覆盖并补齐嵌套默认值')

  console.log('\n== 3. apply() + 工具面编译（假 ctx） ==')
  const { ctx, tools, contexts, handlers, routes, taps, disposeAll } = fakeContext()
  const port = Number(option('--port', '8770'))
  const reuse = has('--reuse')
  fs.mkdirSync(STATE_DIR, { recursive: true })

  // 收起插件自己的 stderr，用来断言挂载时实际生效的配置。
  const captured = []
  const realStderrWrite = process.stderr.write.bind(process.stderr)
  process.stderr.write = (chunk, ...rest) => {
    captured.push(String(chunk))
    return true
  }

  mod.apply(ctx, {
    port,
    // 发布版的默认值是空的（不能写死作者机器上的路径），所以自检必须像
    // 「一台已配置好的机器」那样显式给值。换机器时用环境变量覆盖。
    python: process.env.LIBMEM_PYTHON ||
      'C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe',
    projectRoot: process.env.LIBMEM_PROJECT_ROOT || 'D:\\Deepseek Harness\\dsh-libmem\\v2',
    manageProcess: !reuse,
    reuseExisting: true,
    bridgePoller: false,           // 验证期不要让第二个进程抢温提炼
    startTimeoutMs: 90000,
    toolCallTimeoutMs: 180000,
    // 状态与日志都挪到 .verify/：这个脚本**不许**扰动正在运行的插件的状态文件
    logFile: path.join(STATE_DIR, 'bridge.log'),
    statusFile: path.join(STATE_DIR, 'status.json'),
    // 写入侧：只挂载、不轮询、不喂事件 —— 事件总线在假 ctx 里是空的，
    // 所以既验证了挂载与路径兜底，又**写不进**真冷库一个字节。
    write: {
      enabled: true,
      poll: false,
      statusFile: path.join(STATE_DIR, 'watch-status.json'),
      pollStateFile: path.join(STATE_DIR, 'poll-state.json'),
    },
  })
  process.stderr.write = realStderrWrite
  for (const line of captured) process.stderr.write(line)

  // 嵌套 schema 会把未提供的 write 字段填成空串，插件必须把它们兜回真实路径
  // （见 lib/index.js 的注释）。挂载日志里那行就是生效值。
  const writeStatusFile = path.join(STATE_DIR, 'watch-status.json')
  let writeStatus = null
  try { writeStatus = JSON.parse(fs.readFileSync(writeStatusFile, 'utf8')) } catch {}
  check(writeStatus?.plugin === 'libmem-write', '写入侧已挂载且状态落自检目录', writeStatusFile)
  const mountLine = captured.find((line) => line.includes('写入侧已挂载')) ?? ''
  check(mountLine.includes('root=D:\\Deepseek Harness\\dsh-libmem\\v2'),
    '写入侧 projectRoot 兜底生效（没被空串覆盖）', mountLine.trim())
  check(writeStatus?.pairsWritten === 0, '写入侧未产生任何落盘（验证期零副作用）')

  const expectedNames = [
    'libmem_search', 'libmem_search_simple', 'libmem_drilldown', 'libmem_feedback',
    'libmem_write', 'libmem_extract', 'libmem_stats',
    // ★ 2026-10-12：按时间列清单（"这几天我干了什么"）—— 见 tools.js 里 recent 的说明
    'libmem_recent',
    // ★ 2026-10-12（P2）：整链体检 —— 冷库/温层/索引三层对账，只读
    'libmem_chain',
    'libmem_knowledge_search', 'libmem_knowledge_reindex', 'libmem_knowledge_stats',
    'libmem_knowledge_read', 'libmem_audit_recent', 'libmem_audit_verify', 'libmem_status',
  ].sort()
  const actual = [...tools.keys()].sort()
  check(actual.length === expectedNames.length, `注册了 ${actual.length} 个工具（期望 ${expectedNames.length}）`,
    actual.filter((n) => !expectedNames.includes(n)).join(','))
  check(JSON.stringify(actual) === JSON.stringify(expectedNames), '工具名单逐个吻合')

  for (const def of tools.values()) {
    if (def.parameters?.type !== 'object') { check(false, `${def.name} 入参 schema 根不是 object`); continue }
    if (def.output?.schema?.type !== 'string') check(false, `${def.name} 输出 schema 根不是 string`)
  }
  console.log(`   （已逐个检查 ${tools.size} 个工具的入参/输出 schema 根类型）`)

  console.log('\n== 4. 端到端调用 ==')
  const call = async (toolName, args) => {
    const def = tools.get(toolName)
    if (!def) throw new Error(`工具 ${toolName} 没注册`)
    const controller = new AbortController()
    try {
      return await def.execute(args, {
        signal: controller.signal,
        agent: undefined,
        callId: 'verify',
        name: toolName,
        arguments: args,
      })
    } catch (error) {
      throw new Error(`${toolName}: ${error.message}${error.code ? ` [${error.code}]` : ''}`)
    }
  }

  // 先等服务就绪（apply 是后台启动，不阻塞）
  const statusFile = path.join(STATE_DIR, 'status.json')
  const deadline = Date.now() + 120000
  let status = null
  while (Date.now() < deadline) {
    try { status = JSON.parse(fs.readFileSync(statusFile, 'utf8')) } catch {}
    if (status?.health) break
    await new Promise((r) => setTimeout(r, 500))
  }
  check(Boolean(status?.health), `服务就绪（模式=${status?.mode} pid=${status?.pid}）`,
    status?.lastError ?? '')
  const childPid = status?.pid ?? null

  try {
    const text = await call('libmem_status', {})
    check(typeof text === 'string' && text.includes('LibMem 状态'), 'libmem_status 返回状态正文')
    console.log(text.split('\n').map((l) => `   ${l}`).join('\n'))

    const stats = JSON.parse(await call('libmem_stats', {}))
    check(Number.isFinite(stats.sqlite_total), `libmem_stats 可用（sqlite_total=${stats.sqlite_total}）`)

    const search = JSON.parse(await call('libmem_search', { query: '记忆插件 整合 DSH', top_k: 2 }))
    check(Array.isArray(search.items), `libmem_search 可用（items=${search.items?.length}，depth=${search.depth}）`)

    const simple = JSON.parse(await call('libmem_search_simple', { query: '冷库 轮询', top_k: 1 }))
    check(Array.isArray(simple), `libmem_search_simple 可用（${simple.length} 条）`)

    // ★ 2026-10-12：按时间列清单 —— 用户报的问题是"问最近却给几个月前"，
    //   这条断言就守两件事：① 工具通了；② 回来的**每一条都在窗口内**（不看相关性）。
    const timeline = JSON.parse(await call('libmem_recent', { days: 5, limit: 10 }))
    const tlItems = timeline.items ?? []
    const cutoff = timeline.window?.cutoff ?? ''
    const tlDates = tlItems.map((it) => String(it.source_date ?? '').slice(0, 10))
    const inWindow = tlDates.every((d) => d && (!cutoff || d > cutoff))
    check(tlItems.length > 0 && inWindow,
      `libmem_recent 可用且**只给窗口内**的条目（${tlItems.length} 条，cutoff=${cutoff}）`,
      `日期=${[...new Set(tlDates)].join(',')}`)
    // 顺带验"换管道"那半边：`libmem_search` 带 recent_days 必须走同一条时间管道
    const recentViaSearch = JSON.parse(await call('libmem_search',
      { query: '最近几天做了什么', top_k: 5, recent_days: 3 }))
    check((recentViaSearch.window?.mode === 'timeline'),
      'libmem_search(recent_days=N) 走的是时间管道（window.mode=timeline）',
      `mode=${recentViaSearch.window?.mode} pipeline=${recentViaSearch.pipeline}`)

    // ★ 2026-10-12（P2）：整链体检必须能跑出结构化的三层对账 + 会写心跳。
    const chain = JSON.parse(await call('libmem_chain', {}))
    check(typeof chain.ok === 'boolean' && chain.counts && Array.isArray(chain.problems),
      'libmem_chain 可用（三层对账）',
      `cold=${chain.counts?.cold_lines} warm=${chain.counts?.warm_entries} index=${chain.counts?.index_rows}`)
    check(typeof chain.counts?.index_legacy === 'number' && chain.legacy?.note,
      '体检报告区分「历史行」（v1 迁移，正常）与「孤儿行」（异常）',
      `legacy=${chain.counts?.index_legacy} orphan=${chain.counts?.index_orphan}`)

    const audit = JSON.parse(await call('libmem_audit_recent', { limit: 1 }))
    check(Array.isArray(audit), `libmem_audit_recent 可用（${audit.length} 条）`)

    const kb = JSON.parse(await call('libmem_knowledge_stats', {}))
    check(typeof kb === 'object' && kb !== null, `libmem_knowledge_stats 可用（docs_in_index=${kb.docs_in_index}）`)

    // 参数错误必须是可判别的 INVALID_ARGS，而不是笼统失败
    let code = null
    try {
      await call('libmem_search', { bogus: 1 })
    } catch (error) {
      code = /\[INVALID_ARGS\]/.test(error.message) ? 'INVALID_ARGS' : error.message
    }
    check(code === 'INVALID_ARGS', '参数错误被翻成 INVALID_ARGS', String(code))
  } catch (error) {
    check(false, '端到端调用', error.message)
  }

  console.log('\n== 5. 自动回忆（四档判断 + 纪律提示词 + 注入） ==')
  const recall = await import(pathToFileURL(path.join(PACKAGE, 'lib', 'recall.js')).href)

  // ★ 2026-10-12：判断改成四档（照千问绘图的「生图判断」`drawTrigger`）。
  //   每一档都有自己的正/负用例 —— 只测默认档等于只测了四分之一的规则。
  const byMode = {
    smart: [
      ['好的', false], ['嗯', false], ['/compact', false], ['不要查记忆', false],
      ['把这句话改成英文', false],
      ['之前我们定的那个插件方案是什么？', true],
      ['上次那个报错怎么解决的', true],
      ['数据库里有多少条记忆', true],
      ['还记得我说过显存的事吗', true],
    ],
    strict: [
      // 只认点名与回溯词：问句、长句在这档**不该**翻。
      ['为什么会这样', false],
      ['把这句话改成英文然后写进文件里，顺便帮我检查一下格式对不对', false],
      ['查一下记忆', true],          // 5 个字：必须不被 minChars 挡掉
      ['看看记忆库里有没有沙丘', true],
      ['上次那个报错怎么解决的', true],
      ['/compact', false], ['好的', false],
    ],
    eager: [
      ['随便聊聊天吧', true], ['继续', false], ['好的', false], ['/compact', false],
      ['把这句话改成英文', true],
    ],
    off: [
      ['查一下记忆', false], ['上次那个报错怎么解决的', false], ['随便聊聊天吧', false],
    ],
  }
  let ruleOk = true
  let ruleCount = 0
  for (const [mode, cases] of Object.entries(byMode)) {
    for (const [sample, want] of cases) {
      ruleCount += 1
      const verdict = recall.decide(sample, { mode })
      if (verdict.recall !== want) {
        ruleOk = false
        check(false, `decide(${JSON.stringify(sample)}, ${mode}) 期望 ${want}，得到 ${verdict.recall}（${verdict.why}）`)
      }
      if (verdict.mode !== mode) {
        ruleOk = false
        check(false, `decide(..., ${mode}) 回报的 mode 是 ${verdict.mode}`)
      }
    }
  }
  check(ruleOk, `四档判断 ${ruleCount} 条用例全部符合预期`)

  // 档位归一：旧值 `always` 必须还能用，写错的值回落 smart 而不是抛错。
  check(recall.normalizeMode('always') === 'eager', '旧值 always 归一到 eager（向后兼容）')
  check(recall.normalizeMode('AUTO') === 'smart' && recall.normalizeMode('乱写的') === 'smart',
    '未知档位回落 smart（写错一个字母不至于把插件弄挂）')
  check(recall.normalizeMode(false) === 'off' && recall.normalizeMode(true) === 'smart',
    '布尔档位也认（false → off）')
  check(recall.decide('查一下记忆', { mode: 'strict' }).why === 'explicit',
    '「查一下记忆」在 strict 档算点名（点名排在长度门槛之前）')

  // ★ 2026-10-12：时间范围问句**必须**走时间管道（`recentWindowDays()`）。
  //   这条守的正是用户报的问题：「问最近，却给我几个月前」。
  const winCases = [
    ['最近这几天做了什么', 7], ['最近一周的工作', 7], ['最近三天做了啥', 3],
    ['这两天的图', 2], ['前天生成的图片', 2], ['大前天生成的那张图', 3],
    ['上个月做了什么', 30], ['最近一个月的工作', 30],
    // ⚠️ 这些**不该**被判成时间窗口 —— 尤其"上次"：它是回溯指代，不是时间范围。
    //   判错的代价不对称：多判只是"多贴一块清单"，少判就是"又贴回老记忆"。
    ['上次那个插件方案怎么样了', null], ['把这句话改成英文', null], ['为什么会这样', null],
  ]
  let winOk = true
  for (const [q, want] of winCases) {
    const got = recall.recentWindowDays(q)
    const gotDays = got ? got.days : null
    if (gotDays !== want) {
      winOk = false
      check(false, `recentWindowDays(${JSON.stringify(q)}) 期望 ${want}，得到 ${gotDays}`)
    }
  }
  check(winOk, `时间窗口识别 ${winCases.length} 条用例全部符合预期（含"上次"不该命中）`)

  const dirty = recall.sanitize('模板 {{name}} 与 {{a.b}}')
  check(!dirty.includes('{{'), '注入文本里的 `{{` 已消毒（否则会弄挂模板解析）', dirty)

  check(contexts.has('libmem-recall'), '已注册提示词上下文 libmem-recall')
  // ★ 2026-10-12：纪律提示词是**独立**一段（只随档位变 → 不搅动 KV 缓存前缀）。
  check(contexts.has('libmem-recall-discipline'),
    '已注册提示词上下文 libmem-recall-discipline（「记忆纪律」独立成段）')
  const disciplineProvider = contexts.get('libmem-recall-discipline')
  const disciplineText = disciplineProvider?.text?.({ scope: { id: 'verify-session' } }) ?? ''
  check(disciplineText.includes('记忆使用纪律') && disciplineText.includes('libmem_feedback'),
    '纪律提示词要求「用了就回报」（count 的来源）', `${disciplineText.length} 字符`)
  check(!disciplineText.includes('{{'), '纪律提示词无 `{{`')
  check(disciplineText !== recall.buildRecallDiscipline('eager'),
    '纪律文本随档位不同（当前措辞 ≠ eager 档措辞）')

  const sessionHandlers = handlers.get('session/event') ?? []
  check(sessionHandlers.length > 0, '已监听 session/event（自动回忆的入口）')
  const session = { id: 'verify-session', header: {} }
  const sample = '之前我们定的那个记忆插件整合方案是什么？'
  for (const handler of sessionHandlers) {
    handler(session, {
      type: 'user/message',
      data: { source: { kind: 'user' }, content: [{ type: 'text', text: sample }] },
    })
  }
  const provider = contexts.get('libmem-recall')
  let injected = ''
  const recallDeadline = Date.now() + 60000
  while (Date.now() < recallDeadline) {
    injected = provider.text({ scope: { id: 'verify-session' } })
    if (injected) break
    await new Promise((r) => setTimeout(r, 400))
  }
  check(injected.includes('【自动回忆】'),
    '真人消息 → 自动搜到记忆并生成注入文本', `${injected.length} 字符`)
  // ★ 开局块（2026-10-10 新增）：新会话第一条消息要额外带"最近在做的事"。
  //   这是"短会话省钱"方案的另一半 —— 会话不背几十万 token 历史，但开局得接得上。
  check(injected.includes('【最近在做什么】'),
    '会话开局带「最近在做什么」（按时间取，不按相关性）')
  check(!injected.includes('{{'), '注入文本无 `{{`')
  check(provider.text({ scope: { id: '另一个会话' } }) === '', '别的会话取不到这段记忆（不会贴错）')
  check(provider.text({}) === '', '认不出会话时不注入')

  // 同一会话的第二条消息：不该再重复贴开局块（"好的"则应该什么都不贴）
  for (const handler of sessionHandlers) {
    handler(session, {
      type: 'user/message',
      data: { source: { kind: 'user' }, content: [{ type: 'text', text: '好的' }] },
    })
  }
  await new Promise((r) => setTimeout(r, 1500))
  const second = provider.text({ scope: { id: 'verify-session' } })
  check(!second.includes('【最近在做什么】'), '同一会话第二条消息不再重复贴开局块', `len=${second.length}`)

  // ★ 2026-10-12 回归：**慢的旧检索不许盖掉新消息的缓存**（竞态）。
  //   事故形态：连发两条 —— 第 1 条（长句，要翻）的检索要 1.3s，第 2 条（"好的"，
  //   不翻）瞬间返回；第 1 条的 Promise 后完成、把结果写进缓存 ⇒ 第 2 条这一轮
  //   反而贴上了"关于上一条消息"的记忆。修法见 recall.js 里的代次（`gen`）。
  const raceSession = { id: 'verify-race', header: {} }
  for (const handler of sessionHandlers) {
    handler(raceSession, {
      type: 'user/message',
      data: {
        source: { kind: 'user' },
        content: [{ type: 'text', text: '之前我们定的那个记忆插件整合方案是什么？' }],
      },
    })
    handler(raceSession, {
      type: 'user/message',
      data: { source: { kind: 'user' }, content: [{ type: 'text', text: '好的' }] },
    })
  }  await new Promise((r) => setTimeout(r, 6000))
  const raceText = provider.text({ scope: { id: 'verify-race' } })
  check(raceText === '',
    '竞态：慢的旧检索不会盖掉新消息的缓存（过期结果被丢弃 → 这一轮不注入）',
    `len=${raceText.length}`)

  // ★ 2026-10-12：**时间范围问句必须走时间管道**（不是"多搜一次相关性"）。
  //   这条端到端守的就是用户报的那个问题：问"最近这几天做了什么"，
  //   插件自己贴进来的若是相关性结果，就还是几个月前的老记忆。
  const tlSession = { id: 'verify-timeline', header: {} }
  for (const handler of sessionHandlers) {
    handler(tlSession, {
      type: 'user/message',
      data: {
        source: { kind: 'user' },
        content: [{ type: 'text', text: '最近这几天我干了什么工作？' }],
      },
    })
  }
  const tlDeadline = Date.now() + 45000
  let tlText = ''
  while (Date.now() < tlDeadline) {
    tlText = provider.text({ scope: { id: 'verify-timeline' } })
    if (tlText) break
    await new Promise((r) => setTimeout(r, 400))
  }
  check(tlText.includes('按时间清单'),
    '时间问句走的是「按时间清单」块（`memory_timeline`，不看相关性）',
    `${tlText.length} 字符`)
  const tlDates = (tlText.match(/\[(20\d\d-\d\d-\d\d)/g) ?? [])
    .map((s) => s.slice(1))
  const today = new Date().toISOString().slice(0, 10)
  const weekAgo = new Date(Date.now() - 8 * 86400000).toISOString().slice(0, 10)
  check(tlDates.length > 0 && tlDates.every((d) => d >= weekAgo && d <= today),
    '清单里的日期全在最近一周内（没有几个月前的老条目）',
    `日期=${[...new Set(tlDates)].join(',')}`)

  if (injected) console.log(injected.split('\n').map((l) => `   ${l}`).join('\n'))

  console.log('\n== 6. 界面面板（注入行 + 同源接口） ==')
  const injectHandlers = handlers.get('webserver/index-inject') ?? []
  check(injectHandlers.length > 0, '已订阅 webserver/index-inject（桌面端唯一注入通道）')
  const table = []
  for (const handler of injectHandlers) handler(table)
  check(table.length === 1 && table[0].kind === 'script' && table[0].text.includes('/libmem/panel.js'),
    '注入表拿到 1 条**内联** script 行（不是 script-src：那条加载失败会把界面 boot 弄挂）')
  const table2 = [...table]
  for (const handler of injectHandlers) handler(table2)
  check(table2.length === 1, '宿主重复收集不会推第二条（去重）')

  const paths = [...routes.keys()].sort()
  check(paths.length === 4, `注册了 4 条面板路由`, paths.join(' | '))

  const statusRoute = routes.get('exact /libmem/status')
  const r1 = fakeRes()
  await statusRoute.handler(fakeReq({ url: '/libmem/status' }), r1)
  check(r1.statusCode === 200, 'GET /libmem/status → 200')
  let panelStatus = {}
  try { panelStatus = JSON.parse(r1.body) } catch {}
  check(Boolean(panelStatus.service) && Boolean(panelStatus.recall) && typeof panelStatus.tools === 'number',
    '状态报文含 service / recall / tools', `tools=${panelStatus.tools} mode=${panelStatus.service?.mode}`)
  check(panelStatus.service?.up === true, '状态里服务在线')

  const r2 = fakeRes()
  await statusRoute.handler(fakeReq({ headers: { 'sec-fetch-site': 'cross-site' } }), r2)
  check(r2.statusCode === 403, '跨站请求被拒（403）')

  const r3 = fakeRes()
  await routes.get('exact /libmem/panel.js').handler(fakeReq({ url: '/libmem/panel.js' }), r3)
  check(r3.statusCode === 200 && r3.body.includes('libmem-panel'),
    'GET /libmem/panel.js → 200 + 内容', `${r3.body.length} 字节`)
  let syntaxOk = true
  try { new Function(r3.body) } catch (error) { syntaxOk = false; check(false, `panel.js 语法错误：${error.message}`) }
  check(syntaxOk, 'panel.js 语法可编译（不是一段跑不起来的字符串）')

  const r4 = fakeRes()
  await routes.get('exact /libmem/search').handler(
    fakeReq({ url: '/libmem/search?q=' + encodeURIComponent('记忆插件') }), r4)
  let panelSearch = {}
  try { panelSearch = JSON.parse(r4.body) } catch {}
  check(r4.statusCode === 200 && !panelSearch.error, 'GET /libmem/search 可用', `${(panelSearch.items ?? []).length} 条`)

  const r5 = fakeRes()
  await routes.get('exact /libmem/action').handler(fakeReq({ method: 'GET' }), r5)
  check(r5.statusCode === 405, '动作接口只收 POST（GET → 405）')

  const r6 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'recall', mode: 'off' }) }), r6)
  check(r6.statusCode === 200, 'POST action=recall 被接受')
  const r7 = fakeRes()
  await statusRoute.handler(fakeReq({ url: '/libmem/status' }), r7)
  let panelAfter = {}
  try { panelAfter = JSON.parse(r7.body) } catch {}
  check(panelAfter.recall?.mode === 'off', '面板能把自动回忆切成 off（内存态）', String(panelAfter.recall?.mode))
  const r8 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'recall', mode: 'smart' }) }), r8)
  check(r8.statusCode === 200, '再切回 smart')

  // ★ 2026-10-12：四档白名单 —— 非法档位必须**明确拒绝**，不能静默当 smart
  //   （静默回落会让用户以为"我切到 strict 了"，实际还在 smart 上翻）。
  const r9 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'recall', mode: 'strictish' }) }), r9)
  let badMode = {}
  try { badMode = JSON.parse(r9.body) } catch {}
  check(r9.statusCode === 400 && Array.isArray(badMode.allowed) && badMode.allowed.length === 4,
    '非法档位被拒（400 + allowed 列出四档）', JSON.stringify(badMode.allowed ?? null))

  const r10 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'recall', mode: 'strict' }) }), r10)
  const r11 = fakeRes()
  await statusRoute.handler(fakeReq({ url: '/libmem/status' }), r11)
  let strictPanel = {}
  try { strictPanel = JSON.parse(r11.body) } catch {}
  check(strictPanel.recall?.mode === 'strict' && strictPanel.recall?.modeLabel,
    '面板能切到 strict，且报文里带档位说明（界面不自己编文案）',
    `${strictPanel.recall?.mode} / ${strictPanel.recall?.modeLabel}`)
  check(Array.isArray(strictPanel.recall?.modes) && strictPanel.recall.modes.length === 4,
    '报文带四档选项（界面下拉直接用）')

  // 「记忆纪律」开关：关掉后纪律段返回空串（harness 会跳过空上下文）。
  const r12 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'discipline', enabled: false }) }), r12)
  check(r12.statusCode === 200, 'POST action=discipline 被接受')
  check((contexts.get('libmem-recall-discipline')?.text?.({}) ?? '') === '',
    '关掉纪律后该段返回空串（既不注入、也不报错）')
  const r13 = fakeRes()
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'discipline', enabled: true }) }), r13)
  check((contexts.get('libmem-recall-discipline')?.text?.({}) ?? '').includes('记忆使用纪律'),
    '再打开纪律，段落恢复')

  // 切回默认档，别把状态留给后面的用例
  await routes.get('exact /libmem/action').handler(
    fakeReq({ method: 'POST', body: JSON.stringify({ action: 'recall', mode: 'smart' }) }), fakeRes())

  console.log('\n== 7. 卸载 ==')
  disposeAll()
  check(tools.size === 0, '工具已全部注销')
  await new Promise((r) => setTimeout(r, 1200))
  if (!reuse && childPid) {
    check(!alive(childPid), `自己起的服务已被停掉（pid=${childPid}）`)
  } else {
    console.log(`   复用的服务不动（pid=${childPid ?? '-'}）`)
  }

  console.log(`\n${failures === 0 ? '✅ 全部通过' : `❌ ${failures} 项失败`}`)
  return failures === 0 ? 0 : 1
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error('自检脚本自身出错：', error)
  process.exit(2)
})
