/**
 * LibMem 面板的**宿主侧**：往界面里塞一个小面板，并提供它要用的同源接口。
 *
 * 为什么是"注入一小段脚本"而不是写一个正经的客户端插件
 * ------------------------------------------------------
 * 桌面端（Electron）的 `index.html` 是从安装包静态 dist 直出的，**永远不经过宿主的
 * `renderIndex()`** —— 所以 `tapIndex` 在桌面端不生效。桌面端唯一的注入通道是
 * `webserver/index-inject` 推的结构化行（宿主启动时 `collectIndexInjections()` 一次收集，
 * 经 IPC 交给页面侧逐行应用）。小鲸鱼（`dsh-whale-widget`）踩过这条路的两个坑，这里照抄它的结论：
 *
 *   1. **行必须尽早注册**：那张表是**一次性收集**的，订阅晚于那次收集就永远进不了表。
 *      所以注册动作放在 `apply()` 的第一件事，不放在 `inject([...])` 回调里。
 *   2. **必须用内联 `kind:'script'` 行，不能用 `kind:'script-src'`**：页面侧解释器对
 *      `script-src` 是"加载失败即 reject"，会连 `__DSH_BOOT_READY__` 一起弄挂（应用起不来）；
 *      而内联行由我们自己 `createElement` 并**吞掉 onerror** —— 路由在就加载，不在就静默失败，
 *      宿主永远不会因为面板起不来。
 *
 * 接口一律走**相对路径** `/libmem/*`（与页面同源）：面板不碰跨域，桥本身的 CORS 一个字都不用改。
 *
 * 安全：这些接口只读状态 + 触发几个既有的运维动作（温提炼 / 重建知识库索引 /
 * 切换自动回忆档位 / 开关记忆纪律 / ★ 卸载与拉起本地模型），不碰密钥。
 * ★ 2026-10-16 起 `/libmem/action` 多了两个**有副作用**的动作：`unload` 会结束
 *   bge 那两个 llama-server 进程、`load` 会把它们拉起来。它们和其余接口同源同栅栏
 *   （回环 Host + 非跨站 + Origin 校验），但因为动了进程，这里的栅栏不是可选项。
 * 仍然加一道轻栅栏：**Host 必须是回环**、
 * `Sec-Fetch-Site: cross-site` 一律拒、带 `Origin` 时必须与 Host 同源或是 `dsh-app://`
 * （桌面壳）。跨站页面既读不到状态，也触发不了动作。
 *
 * @module dsh-libmem/panel
 */
import fs from 'node:fs'
import path from 'node:path'
import { RECALL_MODES } from './recall.js'

/** 面板脚本的路径（随包分发）。 */
const PANEL_ASSET = path.join(import.meta.dirname, '..', 'assets', 'panel.js')
/** 注入行里那段内联脚本的指纹，用来去重（宿主可能收集多次）。 */
const FINGERPRINT = '/libmem/panel.js'

/**
 * 注册"把面板脚本挂进页面"的注入行。**必须在 apply() 开头调用**（见文件头第 1 条）。
 *
 * @param {object} ctx - 插件上下文。
 * @returns {() => void} 注销函数（幂等）。
 */
export function registerInjectionRow (ctx) {
  // 内联脚本：自己建 <script src>，**吞掉 onerror**（理由见文件头第 2 条）。
  const inlineText =
    '(function(){try{var d=document.body||document.head||document.documentElement;if(!d)return;' +
    'var s=document.createElement("script");s.src="' + FINGERPRINT + '";' +
    's.onerror=function(){};d.appendChild(s)}catch(e){}})()'

  return ctx.on('webserver/index-inject', (table) => {
    try {
      if (!Array.isArray(table)) return
      for (const row of table) {
        if (!row) continue
        if (row.kind === 'script-src' && row.src === FINGERPRINT) return   // 别的版本推过
        if (row.kind === 'script' && typeof row.text === 'string' &&
            row.text.indexOf(FINGERPRINT) >= 0) return
      }
      table.push({ kind: 'script', placement: 'body', text: inlineText })
    } catch {
      /* 注入失败不该影响宿主启动 */
    }
  })
}

/** 请求是不是可信的第一方（见文件头「安全」）。 */
function trusted (req) {
  try {
    const host = String(req.headers.host || '').toLowerCase()
    const hostname = host.replace(/:\d+$/, '').replace(/^\[/, '').replace(/\]$/, '')
    const loopback = hostname === 'localhost' || hostname.endsWith('.localhost') ||
      hostname === '::1' || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname)
    if (!loopback) return false
    if (String(req.headers['sec-fetch-site'] || '').toLowerCase() === 'cross-site') return false
    const origin = req.headers.origin
    if (!origin) return true                                  // 同源导航 / 本地脚本
    if (String(origin).startsWith('dsh-app://')) return true   // 桌面壳
    try {
      const o = new URL(String(origin))
      return o.host.toLowerCase() === host                     // 与 Host 同源
    } catch {
      return false
    }
  } catch {
    return false
  }
}

/** 统一的 JSON 响应。 */
function sendJson (res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  })
  res.end(body)
}

/** 读请求体（限长，别让面板接口变成"谁都能塞 100MB"的洞）。 */
function readBody (req, limit = 64 * 1024) {
  return new Promise((resolve) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        resolve('')
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', () => resolve(''))
  })
}

/**
 * 组装状态报文（面板显示什么，这里就返回什么）。
 *
 * @param {object} deps
 * @returns {Promise<object>}
 */
async function buildStatus ({ service, gate, settings, toolCount, probeModels, writeStatusFile, modelControl }) {
  const status = service.status
  const health = await service.health(4000).catch(() => null)
  const models = await probeModels(service.base).catch(() => null)
  // ★ 2026-10-16：本地模型（4B / bge-m3 / bge-reranker）此刻在不在显存里 ——
  //   面板的「卸载模型 / 拉起模型」按钮靠它决定显示哪个、以及要不要禁用。
  const modelsCtl = modelControl ? await modelControl.state().catch(() => null) : null

  let write = null
  try {
    write = JSON.parse(fs.readFileSync(writeStatusFile, 'utf8'))
  } catch {
    write = null
  }

  return {
    at: new Date().toISOString(),
    tools: toolCount,
    service: {
      up: Boolean(health),
      mode: status.mode,
      pid: status.pid,
      base: service.base,
      version: health?.version ?? null,
      calls: status.calls,
      failures: status.failures,
      restarts: status.restarts,
      lastError: status.lastError ?? null,
      baseDir: health?.paths?.baseDir ?? null,
      coldVault: health?.paths?.coldVault ?? null,
      poller: health?.poller ?? null,
    },
    models: models ?? null,
    // ★ 2026-10-16：本地模型控制面（卸载 / 需要时自动拉起）。
    modelsCtl,
    recall: {
      enabled: gate.status.enabled,
      mode: gate.settings.mode,
      // ★ 2026-10-12：档位说明 + 四个可选值 + 纪律开关，直接给界面用 ——
      //   文案只在 `recall.js` 里写一份，界面不再自己编（生图判断踩过"档位只写不读"）。
      modeLabel: RECALL_MODES[gate.settings.mode]?.label ?? gate.settings.mode,
      modeHint: RECALL_MODES[gate.settings.mode]?.hint ?? '',
      modes: Object.entries(RECALL_MODES).map(([key, m]) => ({ key, label: m.label, hint: m.hint })),
      discipline: gate.status.discipline,
      decisions: gate.status.decisions,
      recalls: gate.status.recalls,
      explicit: gate.status.explicit,
      skipped: gate.status.skipped,
      injections: gate.status.injections,
      adopted: gate.status.adopted,
      failures: gate.status.failures,
      coldStarts: gate.status.coldStarts,
      last: gate.status.lastRecall,
      lastDecision: gate.status.lastDecision,
      lastFeedback: gate.status.lastFeedback,
    },
    write,
  }
}

/**
 * 注册面板要用的路由。**由调用方在 `ctx.inject(['webServer'], …)` 里调用**
 * —— 这样 webServer 缺席时只是没有面板，工具面照常。
 *
 * @param {object} ctx - 已注入 webServer 的上下文。
 * @param {object} deps - `{ service, gate, settings, toolCount, log }`。
 * @returns {() => void} 注销全部路由。
 */
export function registerRoutes (ctx, { service, gate, settings, toolCount, probeModels, writeStatusFile, modelControl = null, log = () => {} }) {
  const disposers = []

  const register = (route) => {
    try {
      disposers.push(ctx.webServer.register(route))
    } catch (error) {
      // 路由重复（比如同 profile 里挂了两份插件）不该把插件拖垮
      log(`面板路由 ${route.path} 注册失败：${String(error)}`)
    }
  }

  // ---- 面板脚本本身 ----
  register({
    kind: 'exact',
    path: '/libmem/panel.js',
    handler: (req, res) => {
      let js = ''
      try {
        js = fs.readFileSync(PANEL_ASSET, 'utf8')
      } catch (error) {
        res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end(`panel.js 读不到：${String(error)}`)
        return
      }
      const body = Buffer.from(js, 'utf8')
      res.writeHead(200, {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Content-Length': body.length,
        'Cache-Control': 'no-store',
      })
      res.end(body)
    },
  })

  // ---- 状态 ----
  register({
    kind: 'exact',
    path: '/libmem/status',
    handler: async (req, res) => {
      if (!trusted(req)) return sendJson(res, 403, { error: 'forbidden' })
      try {
        sendJson(res, 200, await buildStatus({ service, gate, settings, toolCount, probeModels, writeStatusFile, modelControl }))
      } catch (error) {
        sendJson(res, 500, { error: String(error?.message ?? error) })
      }
    },
  })

  // ---- 试搜 ----
  register({
    kind: 'exact',
    path: '/libmem/search',
    handler: async (req, res) => {
      if (!trusted(req)) return sendJson(res, 403, { error: 'forbidden' })
      try {
        const url = new URL(req.url, 'http://localhost')
        const q = (url.searchParams.get('q') || '').trim()
        if (!q) return sendJson(res, 200, { items: [] })
        const result = await service.call('memory_search', { query: q, top_k: 3, depth: 'L1' })
        const items = (result?.items ?? []).map((it) => ({
          memory_id: it.memory_id,
          summary: it.summary,
          task_type: it.header?.task_type ?? it.task_type ?? '',
          source_date: it.header?.source_date ?? it.source_date ?? '',
        }))
        sendJson(res, 200, { items, dimension: result?.dimension ?? null })
      } catch (error) {
        sendJson(res, 200, { items: [], error: String(error?.message ?? error) })
      }
    },
  })

  // ---- 动作 ----
  register({
    kind: 'exact',
    path: '/libmem/action',
    handler: async (req, res) => {
      if (req.method !== 'POST') return sendJson(res, 405, { error: 'method not allowed' })
      if (!trusted(req)) return sendJson(res, 403, { error: 'forbidden' })
      let payload = {}
      try {
        payload = JSON.parse(await readBody(req) || '{}')
      } catch {
        return sendJson(res, 400, { error: 'bad json' })
      }
      const action = String(payload.action || '')
      try {
        if (action === 'extract') {
          const r = await service.call('memory_extract', {})
          return sendJson(res, 200, { ok: true, started: true, detail: r?.status ?? '' })
        }
        if (action === 'reindex') {
          const r = await service.call('knowledge_reindex', {})
          return sendJson(res, 200, { ok: true, started: true, detail: r?.docs_in_index ?? '' })
        }
        if (action === 'recall') {
          // ★ 2026-10-12：档位从"只有 off/smart 两档"扩成四档（照生图判断）。
          //   合法值以 `RECALL_MODES` 为准；`enabled` 仍可单独传（兼容旧面板脚本）。
          let mode = gate.settings.mode
          if (payload.mode !== undefined) {
            if (!Object.hasOwn(RECALL_MODES, String(payload.mode))) {
              return sendJson(res, 400, {
                error: `unknown mode: ${payload.mode}`,
                allowed: Object.keys(RECALL_MODES),
              })
            }
            mode = gate.setMode(String(payload.mode))
          } else if (payload.enabled !== undefined) {
            mode = gate.setMode(payload.enabled ? 'smart' : 'off')
          }
          log(`面板把自动回忆切成 ${mode}（内存态，重启后回到配置值）`)
          return sendJson(res, 200, {
            ok: true, mode,
            label: RECALL_MODES[mode]?.label ?? mode,
            note: '内存态，重启后回到配置值；要持久就写 profile 的 recall.mode',
          })
        }
        if (action === 'discipline') {
          // 「记忆纪律」提示词段的开关：关掉就只剩贴块（v1 行为）。
          const on = gate.setDiscipline(payload.enabled !== false)
          log(`面板把记忆纪律切成 ${on ? '开' : '关'}（内存态）`)
          return sendJson(res, 200, { ok: true, discipline: on, note: '内存态，重启后回到配置值' })
        }
        // ★ 2026-10-16：本地模型的两个控件（用户要求）。
        //   `unload` = 立刻把 4B + 两个 bge 的显存还回去；
        //   `load`   = 手动拉起（卸载按钮在"已经卸载"时变成它，保证总有回来的路，
        //              哪怕「需要时自动拉起」是关的）；
        //   `autoload` = 那个开关本身，**持久**（落 models.stateFile）。
        if (action === 'unload') {
          if (!modelControl) return sendJson(res, 400, { error: '模型控制未挂载（插件未启用 models 段）' })
          const report = await modelControl.unload()
          log(`面板卸载本地模型：残留=${report.residual.length ? report.residual.join('、') : '无'}`)
          return sendJson(res, 200, { ok: true, report })
        }
        if (action === 'load') {
          if (!modelControl) return sendJson(res, 400, { error: '模型控制未启用' })
          // force：手动点的，不受「需要时自动拉起」开关限制。
          const result = await modelControl.ensureReady({ force: true })
          log(`面板手动拉起本地模型：${JSON.stringify(result.started ?? result.reason ?? result.skipped)}`)
          return sendJson(res, 200, { ok: true, ...result })
        }
        if (action === 'autoload') {
          if (!modelControl) return sendJson(res, 400, { error: '模型控制未启用' })
          const on = modelControl.setAutoload(payload.enabled !== false)
          return sendJson(res, 200, {
            ok: true,
            autoload: on,
            note: '持久开关：关掉之后，被卸载的模型不会被任何工具调用自动拉回来',
          })
        }
        return sendJson(res, 400, { error: `unknown action: ${action}` })
      } catch (error) {
        sendJson(res, 200, { ok: false, message: String(error?.message ?? error) })
      }
    },
  })

  // ---- 浏览器形态（非桌面端）也要能出现：tapIndex 那条路 ----
  try {
    disposers.push(ctx.webServer.tapIndex((html) => {
      if (html.indexOf(FINGERPRINT) !== -1) return html
      const tag = `<script defer src="${FINGERPRINT}"></script>`
      return html.indexOf('</body>') !== -1 ? html.replace('</body>', `${tag}</body>`) : html + tag
    }))
  } catch {
    /* 桌面端不生效，不影响 */
  }

  return () => {
    for (const dispose of disposers.reverse()) {
      try {
        dispose()
      } catch {
        /* 卸载期的失败不该盖住真正的错误 */
      }
    }
  }
}
