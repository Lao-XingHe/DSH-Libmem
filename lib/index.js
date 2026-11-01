/**
 * LibMem —— 书房 OS v2 记忆插件（B1~B7 / C1~C3）的 DSH 原生插件形态。
 *
 * 一个插件承担三件事（此前分散在三个地方）：
 *
 *   1. **托管 Python 服务**：`shufang-os/v2` 的记忆内核以 HTTP 桥常驻，
 *      插件负责起、探、守、停（`./service.js`）。此前是 `dsh-mcp-client` 用
 *      MCP stdio 拉子进程 —— 受限沙箱下管道 spawn 会 EPERM。
 *   2. **注册原生工具面**：`libmem_search` / `libmem_feedback` / … 共 14 个
 *      （`./tools.js`），替掉 `mcp__xilian__*` 的 MCP 前缀工具。
 *   3. **写入侧（B1 冷库）**：会话监听 + 文件轮询落冷库（`./write/`，自
 *      `dsh-cold-memory-watcher@2026-09-30` 逐字节搬入）。
 *
 * 设计约束（都来自这个仓踩过的坑）：
 *
 *   * **启动失败不许拖垮 dsh。** 旧 MCP 配置把 `failOnStartupError` 设成 true，
 *     结果「昔涟 MCP 任何一点启动抖动」都会掐死整个 harness。这里服务在**后台**
 *     起，失败只写状态 + stderr，第一次工具调用会自动再试一次。
 *   * **状态必须可被外部看见。** 服务模式 / pid / 健康报文 / 解析出的路径 /
 *     调用计数 / 日志尾部，全部落 `statusFile`。否则「插件挂了」和「查不到
 *     这条记忆」从外面看一模一样。
 *   * **日志走 stderr。** harness 把插件 stdout 当自己的协议流，`console.log`
 *     会污染它。
 *
 * @module dsh-libmem
 */
import fs from 'node:fs'
import path from 'node:path'
import { Config, DEFAULTS } from './config.js'
import { probeModels } from './client.js'
import { LibmemService } from './service.js'
import { ModelControl, RETRIEVAL_TOOLS } from './models.js'
import { TOOL_SPECS, registerLibmemTools } from './tools.js'
import { registerInjectionRow, registerRoutes } from './panel.js'
import { RecallGate } from './recall.js'
import { apply as mountWritePath } from './write/index.js'

/** 插件名（诊断里出现）。 */
export const name = 'libmem'

/** 工具注册表 + 提示词上下文（自动回忆靠它把记忆贴进模型的输入）。 */
export const inject = ['tools', 'systemPrompt']

export { Config }

/** 诊断输出：必须走 stderr（见文件头）。 */
function log (message) {
  process.stderr.write(`[libmem] ${message}\n`)
}

/**
 * ★ P1（2026-10-12）：读「管线心跳」—— 各工位跑完留的那一行记录。
 *
 * 心跳由 Python 侧（`services/shared/heartbeat.py`）写进
 * `data/index/chain_heartbeat.jsonl`；Node 侧只读不写（写入侧的进度本来就在
 * `watch-status.json` 里）。**读不到就安静地跳过、一个字都不提** ——
 * 心跳是观测数据，缺了不该让状态报告看起来像出错。
 *
 * @param {string} projectRoot 书房 OS 根目录。
 * @param {number} [limit] 最多看最近多少条记录。
 * @returns {string[]} 要拼进状态报告的几行；没有心跳时返回空数组。
 */
function heartbeatLines (projectRoot, limit = 300) {
  if (!projectRoot) return []
  const file = path.join(projectRoot, 'data', 'index', 'chain_heartbeat.jsonl')
  let raw = ''
  try {
    raw = fs.readFileSync(file, 'utf8')
  } catch {
    return []
  }
  /** @type {Map<string, {at: string, status: string, counts: Record<string, number>, note: string}>} */
  const byStage = new Map()
  for (const line of raw.split('\n').slice(-limit)) {
    if (line.trim() === '') continue
    try {
      const record = JSON.parse(line)
      const stage = String(record?.stage ?? 'unknown')
      // 倒着看没必要：只保留每个工位**第一条**遇到的？不 —— 这里按出现顺序覆盖，
      // 最后留下的就是文件里**最后一条**（= 最新一次），符合"各工位最近一次"的语义。
      byStage.set(stage, {
        at: String(record?.at ?? ''),
        status: String(record?.status ?? ''),
        counts: record?.counts ?? {},
        note: String(record?.note ?? ''),
      })
    } catch {
      /* 坏行跳过：心跳不该让状态报告失败 */
    }
  }
  if (byStage.size === 0) return []

  const order = ['b1.write', 'b2.extract', 'b3.index', 'b4.retrieve',
    'b5.heat', 'chain.check', 'chain.repair']
  const stages = [...byStage.keys()].sort((a, b) => {
    const ia = order.indexOf(a)
    const ib = order.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b)
  })
  const out = ['  管线心跳']
  for (const stage of stages) {
    const record = byStage.get(stage)
    const counts = Object.entries(record.counts)
      .filter(([, value]) => value !== 0 && value !== null && value !== undefined)
      .slice(0, 5)
      .map(([key, value]) => `${key}=${value}`)
      .join(' ')
    out.push(`          ${stage.padEnd(13)} ${record.status.padEnd(8)} ${record.at.slice(0, 19)}  ${counts}`)
  }
  out.push(`          文件 ${file}`)
  return out
}

/**
 * 读一个 JSON 文件；读不到就返回 null。
 * @param {string} file
 */
function readJson (file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

/**
 * 安装插件。
 *
 * @param {object} ctx - Cordis 插件上下文。
 * @param {object} [config] - 已按 `Config` 解析的配置。
 */
export function apply (ctx, config = {}) {
  const settings = { ...DEFAULTS, ...config }
  if (settings.enabled === false) {
    log('disabled by configuration')
    return
  }

  // ---------------------------------------------------------------------
  // 本地模型控制（★ 2026-10-16，用户要求：卸载按钮 + 需要时自动拉起开关）
  //
  // 三个本地模型在吃显存：4B（路由，~1.9-4.0G）、bge-m3（18099，~730M）、
  // bge-reranker（18098，~460M）。前两个 bge 是**独立进程**，llama.cpp 单实例
  // 没有卸载 API，只能结束进程 —— 所以"现在把显存还回来"必须由插件动手。
  // 判断与动作全在 `./models.js`，这里只负责接线。
  //
  // ⚠️ 嵌套 schema 的默认值兜底（同下面 write 段那条踩坑注释）：空串一律回落
  //    DEFAULTS，否则面板上的按钮会拿着空路径去拉服务。
  // ---------------------------------------------------------------------
  const modelsSettings = { ...DEFAULTS.models, ...(settings.models ?? {}) }
  for (const key of ['stateFile', 'routerUrl', 'routerModel', 'embedBase', 'embedStartScript', 'rerankBase', 'rerankStartScript']) {
    if (!String(modelsSettings[key] ?? '').trim()) modelsSettings[key] = DEFAULTS.models[key]
  }
  const modelControl = new ModelControl({ settings: modelsSettings, log })

  const service = new LibmemService({
    settings,
    log,
    // 「需要时自动拉起」：模型被卸载之后，第一次**真正要用模型**的调用在这里
    // 把它拉回来。只对 RETRIEVAL_TOOLS 生效 —— 写记忆、看统计、审计不碰向量，
    // 不该因为一次写入就把 1.2GB 拉回显存。钩子本身永不让调用失败（见 service.js）。
    beforeCall: async (name) => {
      if (!RETRIEVAL_TOOLS.has(name)) return
      await modelControl.ensureReady()
    },
  })

  // ---------------------------------------------------------------------
  // 0. 界面面板：**注入行必须是第一件事**。
  //    桌面端那张注入表是宿主启动时**一次性收集**的，晚一步就永远进不了表
  //    （小鲸鱼踩过这个竞态；理由与两条硬约束都在 lib/panel.js 的文件头）。
  // ---------------------------------------------------------------------
  ctx.effect(() => registerInjectionRow(ctx))

  // ---------------------------------------------------------------------
  // 1. 写入侧（B1 冷库）：与读侧同一个包、同一份 projectRoot 解析。
  //    原来这是另一个插件（dsh-cold-memory-watcher），两边各自解析一遍根目录。
  //
  //    ⚠️ 这里必须兜底：嵌套 schema（`write: WriteConfig`）会把**未提供**的字段
  //    填成 WriteConfig 自己的默认值 —— 而那些默认值是空串，会覆盖掉
  //    `DEFAULTS.write` 里的真实路径。实测（2026-10-10）：只写 `{poll:false}`
  //    时校验结果是 `statusFile: ''`、`projectRoot: ''`。
  //    空串在写入侧的含义是「自己去猜」，而会话 cwd 是 `<DSH_WORKSPACE>`，
  //    那里没有 config.yaml —— 猜不出来，于是一条也不记。所以三个字段显式兜底。
  // ---------------------------------------------------------------------
  const writeSettings = { ...DEFAULTS.write, ...(settings.write ?? {}) }
  if (!writeSettings.projectRoot) writeSettings.projectRoot = settings.projectRoot
  if (!writeSettings.statusFile) writeSettings.statusFile = DEFAULTS.write.statusFile
  if (!writeSettings.pollStateFile) writeSettings.pollStateFile = DEFAULTS.write.pollStateFile
  // vault / pollRoot 留空是**有意的**：空 = 用 projectRoot 推 / 用 DSH_HOME/sessions。
  mountWritePath(ctx, writeSettings)
  log(`写入侧已挂载（root=${writeSettings.projectRoot || 'auto'}, poll=${writeSettings.poll !== false}, status=${writeSettings.statusFile}）`)

  // ---------------------------------------------------------------------
  // 2. Python 服务：后台启动。**不能 await 成硬失败**（见文件头）。
  // ---------------------------------------------------------------------
  service.start().catch((error) => {
    log(`服务启动失败（首次工具调用时会自动再试）：${error.message}`)
  })

  // ---------------------------------------------------------------------
  // 3. 自动回忆：插件自己判断"这句话要不要翻记忆"，翻好了贴进模型输入。
  //    （判断规则见 ./recall.js；两个硬约束也在那个文件的头注释里：
  //     上下文提供者是同步的、注入文本里不能有 `{{`。）
  //
  //    ★ 2026-10-12：三条 context，各管一件事 ——
  //      ① `libmem-recall`            每轮变（记忆块），order 130
  //      ② `libmem-recall-discipline` 只随档位变（"必须用"的纪律），order 131
  //      ③ 两者都由 `gate` 的内存态控制，面板改档位**立即生效**（无需重启）
  //    ⚠️ ①② 分开注册不是洁癖：混成一段的话，每轮变化的文本会把
  //       systemPrompt 前缀搅动一次，KV 缓存命中率掉下来（成本直接上去）。
  // ---------------------------------------------------------------------
  const recallSettings = { ...DEFAULTS.recall, ...(settings.recall ?? {}) }
  if (!recallSettings.mode) recallSettings.mode = DEFAULTS.recall.mode
  const gate = new RecallGate({ service, settings: recallSettings, log })

  // `{ global: true }` 必须带：会话的 emitCtx 带作用域过滤，不带标记的监听器会在
  // 会话不在本插件作用域时被丢掉（这条是写入侧实测出来的，见 write/index.js）。
  ctx.on('session/event', (session, event) => { gate.note(session, event) }, { global: true })

  try {
    ctx.systemPrompt.context({
      name: 'libmem-recall',
      // 130：排在 harness 自己的三条上下文（沙箱 110 / 审批 115 / 委派 120）之后。
      order: 130,
      text: (context) => gate.textFor(context?.scope),
    })
    // 131：纪律紧跟在记忆块后面 —— 先给资料，再给"怎么用这份资料"的要求。
    // 返回空串时 harness 会跳过（`renderPrompt` 里 `.filter(text.length > 0)`）。
    ctx.systemPrompt.context({
      name: 'libmem-recall-discipline',
      order: 131,
      text: () => gate.disciplineText(),
    })
    log(`自动回忆已注册（mode=${gate.status.mode}, 最多 ${recallSettings.maxItems} 条, ` +
      `记忆纪律=${gate.status.discipline ? '开' : '关'}）`)
  } catch (error) {
    // 注不进去也不能拖垮插件：工具面照常可用，只是少了"自动"。
    log(`⚠️ 自动回忆注册失败（工具不受影响）：${String(error)}`)
  }

  // ---------------------------------------------------------------------
  // 4. 工具面。
  // ---------------------------------------------------------------------
  const statusText = async () => {
    const health = await service.health(4000)
    const lines = ['LibMem 状态']
    lines.push(`  服务    ${service.base}`)
    lines.push(`  模式    ${service.status.mode ?? '未启动'}   pid=${service.status.pid ?? '-'}` +
      (service.status.restarts ? `   重启=${service.status.restarts}` : ''))

    if (health) {
      lines.push(`  版本    ${health.version}`)
      lines.push(`  内核    ${health.python}`)
      lines.push(`  工具    ${health.toolCount} 个`)
      lines.push(`  轮询    ${health.poller?.state ?? '?'}${health.poller?.detail ? ` (${health.poller.detail})` : ''}`)
      lines.push('  数据路径')
      for (const key of ['baseDir', 'coldVault', 'warmDir', 'indexDir']) {
        lines.push(`          ${key}=${health.paths?.[key] ?? '?'}`)
      }
      if (health.paths?.error) lines.push(`          ⚠️ ${health.paths.error}`)
    } else {
      lines.push('  ⚠️ 健康检查失败：服务没在跑（下一次工具调用会自动尝试拉起）')
    }

    // 模型服务真探活：区分「服务活着」与「它算得动」。
    // 嵌入挂掉时检索仍有结果（phase2_rank 的 fail-open 护栏兜住），但分数是噪声 ——
    // 光看工具响应分辨不出来，必须在这里报。
    const models = await probeModels(service.base)
    if (models) {
      const fmt = (m) => (m?.ok ? `ok ${m.ms}ms` : `✗ ${m?.error ?? '不可用'}`)
      lines.push(`  模型    嵌入 ${fmt(models.embed)}（${models.embed?.dim ?? '?'} 维）` +
        `   重排 ${fmt(models.rerank)}${models.cached ? '   (30s 缓存)' : ''}`)
      if (models.degraded) {
        lines.push('          ⚠️ 降级：模型服务不可用 ⇒ 检索分数不可信（护栏保证不返回空，但结果质量会掉）')
      }
    }

    // ★ 2026-10-16：本地模型此刻**占不占显存**（与上面那条"算得动吗"互补）——
    //   面板上的「卸载模型 / 需要时自动拉起」读的就是同一份事实，这里给 agent 一份。
    const ctl = await modelControl.state().catch(() => null)
    if (ctl) {
      const routerText = ctl.router.ok
        ? ({ loaded: '已加载', sleeping: '睡眠', unloaded: '已卸载' }[ctl.router.value] ?? ctl.router.value)
        : '路由不可达'
      const serverText = (s) => (!s.up ? '已卸载' : (s.sleeping ? '睡眠' : '在线'))
      lines.push(`  显存    ${ctl.holds ? '占用中' : '已全部释放'}` +
        `   4B=${routerText}  嵌入=${serverText(ctl.embed)}  重排=${serverText(ctl.rerank)}` +
        `   自动拉起=${ctl.autoload ? '开' : '关'}`)
    }

    lines.push(`  调用    ${service.status.calls} 次，失败 ${service.status.failures} 次` +
      (service.status.lastCall
        ? `；最近 ${service.status.lastCall.name} ${service.status.lastCall.ms}ms @ ${service.status.lastCall.at}`
        : ''))
    if (service.status.lastError) lines.push(`  错误    ${service.status.lastError}`)

    // 自动回忆的状态：判断了多少次、翻了几次、贴进去了几次。
    // 「注入」那个数字是核对"赶不赶得上组装输入"的唯一凭据（见 recall.js 文件头）。
    lines.push(gate.describe())

    const write = readJson(writeSettings.statusFile)
    if (write) {
      lines.push('  写入侧')
      lines.push(`          冷库=${write.vaultDir ?? '(未解析)'}  根=${write.projectRoot ?? '(未解析)'}  来源=${write.rootSource ?? '-'}`)
      lines.push(`          已写 ${write.pairsWritten ?? 0} 轮 / 跳过 ${write.pairsSkipped ?? 0} / 失败 ${write.pairsFailed ?? 0}` +
        `；轮询文件 ${write.pollFiles ?? 0}，子 Agent 跳过 ${write.pollSubagentArtifacts ?? 0}`)
      if (write.lastWrite) lines.push(`          最近落盘 ${write.lastWrite.at ?? JSON.stringify(write.lastWrite)}`)
      if (write.lastError) lines.push(`          ⚠️ ${write.lastError}`)
    } else {
      lines.push(`  写入侧  状态文件读不到：${writeSettings.statusFile}`)
    }

    // ★ P1（2026-10-12）：管线心跳 —— 各工位"最近一次跑得怎么样"。
    //   P2 的体检（chain.check）也在这张表里，所以这一段就是"看板"。
    lines.push(...heartbeatLines(settings.projectRoot))

    lines.push(`  状态文件 ${settings.statusFile}`)
    lines.push(`  服务日志 ${settings.logFile}`)
    return lines.join('\n')
  }

  if (settings.registerTools !== false) {
    registerLibmemTools(ctx, {
      prefix: settings.toolPrefix,
      service,
      statusText,
      // ★ 2026-10-12：`libmem_feedback` 成功后回报给自动回忆闸门 ——
      //   这样 `libmem_status` / 面板上的「采用」才是**有来源**的数字，
      //   而不是一个永远为 0 的摆设（用户抱怨的"看不到输出"就靠它证伪）。
      onFeedback: (info) => gate.noteFeedback(info),
    })
    log(`工具面已注册：前缀 ${settings.toolPrefix}_（14 个）`)
  }

  // ---------------------------------------------------------------------
  // 5. 界面面板的同源接口（`/libmem/status` `/libmem/search` `/libmem/action`）。
  //    用 `ctx.inject` 而不是对象级 inject：webServer 缺席时**只是没有面板**，
  //    工具面与自动回忆照常 —— 记忆能力不该被一个界面按钮拖住。
  // ---------------------------------------------------------------------
  if (settings.panel !== false) {
    ctx.inject(['webServer'], (webCtx) => {
      const disposeRoutes = registerRoutes(webCtx, {
        service,
        gate,
        settings,
        probeModels,
        modelControl,
        toolCount: TOOL_SPECS.length + 1,
        writeStatusFile: writeSettings.statusFile,
        log,
      })
      webCtx.effect(() => () => disposeRoutes())
      log('面板已挂：界面右下角会出现「记忆」按钮（/libmem/*）')
    })
  }

  // 卸载：停掉自己起的服务。复用来的服务不动（不是我们的进程）。
  ctx.effect(() => () => {
    service.dispose().catch((error) => log(`卸载时停服务失败：${String(error)}`))
  })

  service.writeStatus()
  log(`已挂载（base=${service.base}, projectRoot=${settings.projectRoot}, manageProcess=${settings.manageProcess}）`)
}
