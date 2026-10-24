/**
 * 自动回忆闸门 —— 「这句话要不要翻记忆」由插件判断，不再每次等模型主动去查。
 *
 * 一次自动回忆分三步：
 *
 *   1. **判断**（`decide`）：纯正则规则，可解释、可关。四档见下。
 *   2. **搜索**：命中就用 `libmem_search` 后台搜一次（`apply_heat` 由服务决定）。
 *   3. **注入**：等模型准备回答、harness 组装输入时，把结果作为**运行时上下文**贴进去
 *      （`ctx.systemPrompt.context`，落盘形态就是模型看到的那句
 *      "Current runtime context. This snapshot supersedes earlier…"）。
 *
 * ★ 2026-10-12：档位改造（照 `dsh-qwen-paint` 的「生图判断」四档 `drawTrigger` 抄）。
 *
 * 为什么改：原先只有 `off` / `smart` 两档，**没有"只在点名时翻"这一档**，
 * 也没有"每条都翻"；更要紧的是 —— 判断只管"什么时候搜"，**没写"搜到了要怎么办"**，
 * 于是记忆块贴上来了、模型却可以当没看见。用户的原话是「不想它只是单纯地录入，
 * 想要有录入有检索有输出」「录入之后又没有回应，那这个记忆插件有点没用」。
 *
 * ⇒ 两处补：
 *   ① **四档判断**（`RECALL_MODES`，与生图判断同构）：
 *      `off` 不翻 / `strict` 只认点名与回溯词 / `smart`（默认）再加问句与长句 /
 *      `eager` 每条都翻（只排除寒暄与斜杠命令）。
 *      任何一档底下，"点名要记忆"（`EXPLICIT_RE`）**永远优先** —— 这是用户要的
 *      「明确要求或者点名要求」。
 *   ② **纪律提示词**（`buildRecallDiscipline()`）：与判断分开放进
 *      **稳定的 systemPrompt 段**（不随每轮记忆块变化，不破坏 KV 缓存前缀），
 *      把「调用了」升级成「算数了」——要求模型真去读块、真引用、用了就回报
 *      `libmem_feedback`（那是 count 唯一的来源），不够就自己再 `libmem_search`。
 *      「看门狗」提示（`context-guard`）就是同一手法的先例：光注入不够，要配一句
 *      "这是给你的、你要照做"。
 *
 * ⚠️ 两个必须记住的约束（都来自读 harness 源码，不是猜的）：
 *
 *   * **上下文提供者是同步的**（`dsh-system-prompt` 里 `entry.text(context)` 直接取值，
 *     不 await）。所以这里**不能**在提供者里现场搜索 —— 必须在收到消息时就搜好、放缓存，
 *     提供者只读缓存。代价是：**第一句话可能赶不上**（组装输入比搜索快），
 *     从下一次组装（同一轮的第 2 步，或下一轮）起稳定生效。`status()` 会记下每次判断，
 *     用来核对"到底赶上了没有"。
 *   * **文本里不能出现 `{{`**（组装输入时会当作变量引用去解析，解析失败会直接抛错、
 *     把这一轮的模型调用弄挂）。记忆内容是用户原文提炼来的，**必须消毒**。
 *
 * @module dsh-libmem/recall
 */
import { isHumanUserMessage, textOf } from './write/pairing.js'

/** 默认设置（可被插件配置覆盖）。 */
export const DEFAULTS = {
  /** 总开关。 */
  enabled: true,
  /**
   * 判断档位（四档，见 `RECALL_MODES`）：
   * `off` / `strict` / `smart`（默认）/ `eager`。
   *
   * 兼容旧值：`always` ↔ `eager`（v1 只有 off/smart/always 三档，写法保留可用）。
   */
  mode: 'smart',
  /** 最多贴几条进输入。 */
  maxItems: 5,
  /** 贴进去的正文上限（字符），超出就截断。 */
  maxChars: 1200,
  /** 比这更短的话不值得翻（"好的""嗯"）。 */
  minChars: 6,
  /** 缓存多久算新鲜；过期不再注入（避免贴上一次对话的旧结果）。 */
  freshMs: 180000,
  /**
   * ★ 2026-10-12：是否注入「记忆纪律」提示词段。
   *
   * 关掉它就只剩"贴块"（v1 的行为）—— 模型仍可能无视记忆。
   * 只有在你确认噪音太大、想自己接管时才关。
   */
  discipline: true,
  /**
   * ★ 2026-10-10：**新会话开局**额外贴一段"最近在做的事"。
   *
   * 为什么：会话一长，每次调用都要背上几十万 token 的上下文（实测平均 42.9 万、
   * 峰值 67.6 万），真正新的内容不到 1%。省钱的正解是「短会话 + 按需检索」，
   * 但短会话开局是失忆的 —— 这一段就是补那个缺口：按时间从记忆库拉最近几条，
   * 替掉"把整段历史一直背在上下文里"。
   */
  coldStart: true,
  /** 开局贴几条。 */
  coldStartItems: 6,
  /** 只取最近多少天内的记忆。 */
  coldStartDays: 14,
}

/**
 * ★ 2026-10-12：**四档判断**（与 `dsh-qwen-paint` 的「生图判断」`drawTrigger` 同构）。
 *
 * | 档位 | 行为 | 适合谁 |
 * |---|---|---|
 * | `off` | 完全不自动翻（`libmem_search` 工具照常可用） | 只想要个安静的记忆库 |
 * | `strict` | **只认点名与回溯词**：「查一下记忆」「上次那个方案」 | 怕误触发、嫌噪音 |
 * | `smart` | 在 strict 基础上**再加问句与长消息**（原默认行为） | 默认 |
 * | `eager` | **每条真人消息都翻**，只排除寒暄与斜杠命令 | 记忆即上下文，不怕 token |
 *
 * `describe` 会原样出现在界面下拉与 `libmem_status` 里 —— 改档位时**连说明一起改**，
 * 否则用户看到的解释跟实际行为会对不上（生图判断那边踩过：档位只写不读）。
 */
export const RECALL_MODES = {
  off: {
    label: '不自动翻（只留工具）',
    hint: '不主动检索；要记忆时点名让我查，或我按需调 libmem_search。',
  },
  strict: {
    label: '只认点名与回溯词',
    hint: '你说「查记忆 / 还记得 / 上次」这类话才翻，其余一律不翻（最省 token）。',
  },
  smart: {
    label: '自动判断（默认）',
    hint: '点名与回溯词、问句、长消息都翻；寒暄与斜杠命令不翻。',
  },
  eager: {
    label: '每条都翻（最积极）',
    hint: '每条真人消息都先翻一次记忆，只跳过寒暄与斜杠命令。',
  },
}

/** 旧值 → 新档位。`always` 是 v1 的写法，语义等同 `eager`。 */
const MODE_ALIASES = { always: 'eager', on: 'smart', auto: 'smart', true: 'smart', false: 'off' }

/**
 * 把任意输入收敛成合法档位（未知值一律回落到 `smart` 并说明）。
 * @param {unknown} value
 * @returns {'off'|'strict'|'smart'|'eager'}
 */
export function normalizeMode (value) {
  if (typeof value === 'boolean') return value ? 'smart' : 'off'
  const key = String(value ?? '').trim().toLowerCase()
  if (Object.hasOwn(RECALL_MODES, key)) return /** @type {any} */ (key)
  if (Object.hasOwn(MODE_ALIASES, key)) return /** @type {any} */ (MODE_ALIASES[key])
  return 'smart'
}

/** 档位说明文本（`libmem_status` / 界面共用一份，避免两处漂移）。 */
export function describeModes () {
  return Object.entries(RECALL_MODES).map(([key, m]) => `    ${key.padEnd(7)} ${m.label} —— ${m.hint}`).join('\n')
}

/** 寒暄 / 确认 / 道谢：明确不翻。 */
const ACK_RE = /^(好|好的|好吧|行|可以|嗯|哦|噢|收到|明白|懂了|对|是的|没错|谢谢|多谢|感谢|继续|不用了?|停下?|先这样|ok|okay|yes|no|thanks|thank you)[。.!！?？~～]*$/i

/** 用户明确说别翻记忆。 */
const DONT_RE = /(不要|别|不用)(去)?(查|翻|搜|用)(一下)?(记忆|历史)/

/**
 * ★ 点名要记忆（四档里**优先级最高**的正面信号；`off` 档也一样不翻，见 `decide`）。
 *
 * 用户原话：「我想有自动判断啊，或者说有明确要求或者点名要求」——
 * 这一条就是"点名"：只要说了"查记忆 / 翻记忆 / 记不记得 / 看看记忆"，
 * 不管这话多短、不管哪一档（`off` 除外，那是"我不想要自动"），都要翻。
 */
const EXPLICIT_RE = /(查|翻|搜|找|看|调|读)(一下|一查|一翻|一遍)?(我的|你(的|们)?)?(记忆|历史|记忆库)|记忆(库|里)?(有|有没有|查|搜|翻)|(还)?记(得|不记得)|想起来|提醒我/

/** 回溯词：出现就值得翻。 */
const RECALL_RE = new RegExp([
  '之前', '上次', '上回', '以前', '原来', '当初', '当时', '后来', '早先',
  '还记得', '记不记得', '我说过', '我们(说|定|聊|讨论|决定)过', '历史',
  '那件事', '那个(项目|文件|方案|插件|服务|问题|功能)',
  '接着上次', '上面(说|提)的', '刚(才)?(说|提)的',
].join('|'))

/** 问句形态：出现就值得翻（`smart` 及以上）。 */
const QUESTION_RE = /[?？]|什么|怎么|为什么|为啥|哪个|哪些|哪一|谁|多少|几时|多久|是不是|有没有|能不能|要不要|如何|该不该/

/**
 * 判定：要不要翻记忆。返回 `why` 便于事后核对（写进状态文件）。
 *
 * ★ 顺序即优先级（2026-10-12 修正）：**关 → 斜杠命令 → 用户说别翻 → 点名 → 寒暄
 * → 长度门槛 → 档位规则**。
 *
 * 两个反直觉的顺序，都是实测出来的：
 *   · **点名排在长度门槛之前**：`minChars`（6）本来是为了滤掉"好的""嗯"这类低信号
 *     短句，但「查一下记忆」**正好 5 个字** —— 摆在门槛后面就会被当成"太短"，
 *     而这恰恰是用户最明确的"我要你用记忆"的信号。所以点名必须是第一道正面判断。
 *   · **点名排在寒暄之前**：ACK_RE 是整串精确匹配（`^…$`），正常不会和点名撞；
 *     但"记得"这种词出现在寒暄里的可能性存在，倾向正面信号优先。
 *
 * @param {string} text 用户原话
 * @param {object} [settings] 插件配置里的 `recall` 段
 * @returns {{recall: boolean, why: string, mode: string, explicit: boolean}}
 */
export function decide (text, settings = DEFAULTS) {
  const s = { ...DEFAULTS, ...settings }
  const mode = normalizeMode(s.mode)
  const raw = (text ?? '').trim()
  const no = (why) => ({ recall: false, why, mode, explicit: false })

  if (s.enabled === false || mode === 'off') return no('disabled')
  if (raw.startsWith('/')) return no('slash-command')
  if (DONT_RE.test(raw)) return no('user-said-dont')

  // ★ 点名：任何档位都翻（off 已在上面退出），且**不受长度门槛限制**。
  if (EXPLICIT_RE.test(raw)) return { recall: true, why: 'explicit', mode, explicit: true }

  if (raw.length < s.minChars) return no(`too-short(${raw.length})`)
  if (ACK_RE.test(raw)) return no('ack')

  // 回溯词：**`strict` 也认**（该档的定义就是"只认点名与回溯词"）。
  if (RECALL_RE.test(raw)) return { recall: true, why: 'lookback-word', mode, explicit: false }

  if (mode === 'strict') return no('strict-no-signal')
  if (mode === 'eager') return { recall: true, why: 'eager', mode, explicit: false }

  // smart（默认）：在 strict 之上再加问句与长消息。
  if (QUESTION_RE.test(raw)) return { recall: true, why: 'question', mode, explicit: false }
  if (raw.length >= 40) return { recall: true, why: 'long-message', mode, explicit: false }
  return no('no-signal')
}

/**
 * ★ 2026-10-12：**记忆纪律**提示词 —— 与判断档位同构，四档共用。
 *
 * 为什么必须有这一段（这是本轮改造的另一半，也是"注入没回应"的解药）：
 *   判断只解决"**搜不搜**"，贴块只解决"**看不看得见**"。模型完全可以看见块、
 *   然后当它不存在 —— 用户的原话就是「录入之后又没有回应」。
 *   `dsh-context-guard` 的【上下文提醒】也是同一手法：**光注入不够，要配一句
 *   "这是给你的、你要照做"**。
 *
 * 与 `format()` 注入的那个块**分开注册**（`lib/index.js` 里是另一条 context）：
 *   本段文本**只随档位变**，每轮都一样 ⇒ systemPrompt 前缀稳定、KV 缓存不破；
 *   而记忆块每轮都在变。两件事混在一段里会让每轮都重写前缀。
 *
 * ⚠ 措辞纪律（照生图判断的教训）：
 *   · **禁止 + 给理由**（"建议优先"这种软话没用，模型会自己判断"这次不参考也行"）；
 *   · **给出边界**：检索为空 / 不相关时怎么办 —— 否则模型会硬凑记忆编答案；
 *   · **不承诺"我记得"**：记忆里没有就得说没有，不许拿摘要脑补原文。
 *
 * @param {string} mode 档位（`RECALL_MODES` 的键）
 * @returns {string} 要注入 systemPrompt 的段落；档位非法返回 `smart` 的版本。
 */
export function buildRecallDiscipline (mode) {
  const key = normalizeMode(mode)
  const modeLine = {
    off: '★ 当前档位 `off`：插件**不自动检索**记忆。要历史信息时，你自己调 `libmem_search` / `libmem_drilldown` 去查 —— 不要凭印象说"我不记得"。',
    strict: '★ 当前档位 `strict`：只有你**点名**（「查一下记忆」「上次那个」）或说到回溯词时，插件才会自动翻。其余时候要历史信息，自己调 `libmem_search`。',
    smart: '★ 当前档位 `smart`（默认）：插件按规则自动翻 —— 回溯词 / 问句 / 长消息都会翻。块里已经给你的东西**先看块**，不够再自己调 `libmem_search`。',
    eager: '★ 当前档位 `eager`：每条消息插件都先翻一次，块里通常已经有相关资料 —— **默认先读块**，再决定要不要补一次 `libmem_search`。',
  }[key]

  return [
    '【记忆使用纪律】你有长期记忆库（`libmem_*` 工具），并且插件会把检索结果作为',
    '【最近在做什么】/【自动回忆】块贴进你的输入。★ **这些块是给你的资料，不是给你看的装饰** ——',
    '看见了就要用。具体三条：',
    '① **块里有相关内容 → 直接拿它回答**：把它当**事实依据**（用户的原话、决定、结论），',
    '不要重新猜一遍、不要只凭本轮对话推断；回答里自然带上出处（日期 / 那一条的 `id`）。',
    '② **块里没有、或明显不相关 → 先换一次问法或时间范围，再决定说"没有"**：',
    '★ 问的若是**时间范围**（"最近/这几天/这周/上个月"），**必须**改用 `libmem_recent`',
    '（或 `libmem_search(..., recent_days=N)`）走**时间**那条路 —— 按相关性搜会把几个月前的',
    '高热度老记忆翻上来（它们和问句同样相似）。那不是"记忆里没有"，是**走错了管道**：',
    '库里 800+ 条、最近几天的也在，只是相关性排序永远排不到它们。',
    '查**具体事实**时才用 `libmem_search` 换说法再搜一次，要细节用 `libmem_drilldown(depth=L2/L3)`。',
    '两轮都空、时间窗口也覆盖过了，才说"记忆里没有这条" —— 不许假装"我记得"，也不拿摘要脑补原文。',
    '③ **真的用上了就回报**：把被你**实际用进回答**的那几条 `memory_id` 交给 `libmem_feedback` ——',
    '那是 count（被采用次数）唯一的来源；没用上就别报（宁可少报，不要凑数）。',
    '★ 不要因为"我大概能推出来"就跳过检索：**先翻记忆，再回答** —— 推出来的和记下来的经常不一样。',
    modeLine,
  ].join('')
}

/**
 * 消毒：把 `{{` 拆开。
 *
 * 为什么必须有：harness 会把上下文文本当模板解析变量，遇到解析不了的 `{{` 直接抛错。
 * 记忆内容是用户原话来的（代码片段、JSON、模板里全是 `{{`），不消毒就等于埋雷。
 */
export function sanitize (text) {
  return String(text ?? '').replace(/\{\{/g, '{ {')
}

/** 把检索结果拼成给模型看的短块。 */
export function format (items, settings = DEFAULTS, query = '') {
  const s = { ...DEFAULTS, ...settings }
  const list = Array.isArray(items) ? items.slice(0, s.maxItems) : []
  if (list.length === 0) return ''
  const lines = [`【自动回忆】按你刚说的话，从长期记忆里翻到 ${list.length} 条（新的在前）：`]
  let used = lines[0].length
  for (const it of list) {
    const date = (it?.header?.source_date) || (it?.source_date) || '无日期'
    const type = (it?.header?.task_type) || (it?.task_type) || ''
    const id = it?.memory_id ?? ''
    const summary = String(it?.summary ?? '').replace(/\s+/g, ' ').trim()
    const line = `- [${date}${type ? '·' + type : ''}] ${summary}${id ? `  (id=${id})` : ''}`
    if (used + line.length > s.maxChars) {
      lines.push(`…（还有 ${list.length - lines.length + 1} 条被字符上限截掉）`)
      break
    }
    lines.push(line)
    used += line.length
  }
  lines.push('（只给摘要：相关内容**直接拿去用**并带上出处；要片段/原文用 ' +
    'libmem_drilldown(memory_id, depth=L2/L3)；不够就换说法再 libmem_search；真用上了用 libmem_feedback 回报 id）')
  return sanitize(lines.join('\n'))
}

/**
 * 开局块：新会话开始时按**时间**贴一段"最近在做的事"（不按相关性）。
 *
 * 与 `format()` 的分工：那个是"关于你刚说的话我记得什么"，这个是
 * "在你开口之前，我最近在忙什么" —— 短会话开局靠它接上。
 */
export function formatRecent (items, settings = DEFAULTS) {
  const s = { ...DEFAULTS, ...settings }
  const list = Array.isArray(items) ? items.slice(0, s.coldStartItems) : []
  if (list.length === 0) return ''
  const lines = [`【最近在做什么】记忆库里最近 ${list.length} 条（按时间倒序，供你接上下文用）：`]
  let used = lines[0].length
  for (const it of list) {
    const line = `- [${it?.source_date || '无日期'}${it?.task_type ? '·' + it.task_type : ''}] ` +
      `${String(it?.summary ?? '').replace(/\s+/g, ' ').trim()}${it?.memory_id ? `  (id=${it.memory_id})` : ''}`
    if (used + line.length > s.maxChars) break
    lines.push(line)
    used += line.length
  }
  return sanitize(lines.join('\n'))
}

/** 子 Agent 会话不参与自动回忆（与写入侧同一条 MAP-D §7 规则）。 */
function isSubagentSession (session) {
  const header = session?.header
  if (!header || typeof header !== 'object') return false
  return header.origin === 'subagent' || header.parentSession != null
}

/**
 * ★ 2026-10-12：这句话是不是在问**最近一段时间**？是的话返回窗口天数。
 *
 * 为什么 JS 侧也要判一次（Python 侧 `retrieval.query_time_window()` 已经有一份）：
 * 这件事决定的是**走哪条管道** —— 时间问句必须走 `memory_timeline`（不看相关性），
 * 否则"最近做了什么"会被按相关性搜成几个月前的高热度老条目。JS 侧这一步发生在
 * **发请求之前**，所以只能在 JS 里再判一次。
 *
 * ⚠️ 两份正则必须**同步改**（Python 那份在 `retrieval._RE_CN_RECENT` 一带）：
 * 判错方向的代价不对称 —— JS 多判一次只是"多贴一块清单"，少判一次则是"又贴回老记忆"。
 * 所以这里**宁可多判**：只要带"最近/这几天/前天/上周/上个月/今年"这类词就算。
 *
 * @param {string} text
 * @returns {{days: number, raw: string}|null}
 */
export function recentWindowDays (text) {
  const t = String(text ?? '')
  if (!t) return null
  // 精确的"N 天/周/月"（含中文数字）
  const cn = { 一: 1, 两: 2, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
  const numbered = /(?:最近|近|前|这)\s*(\d+|[一二两三四五六七八九十])\s*(天|日|周|星期|个?月|年)/.exec(t)
  if (numbered) {
    const raw = numbered[1]
    const n = /^\d+$/.test(raw) ? Number(raw) : (cn[raw] ?? 0)
    if (n > 0) {
      const mult = { 天: 1, 日: 1, 周: 7, 星期: 7, 月: 30, 个月: 30, 年: 365 }[numbered[2]] ?? 1
      return { days: n * mult, raw: numbered[0] }
    }
  }
  // 明确数字的"这两天/这两天"（"几"是不定量，交给默认 7 天）
  const bare = /(?:最近|近|这)\s*(一|半|两)\s*(天|日|周|星期)/.exec(t)
  if (bare) {
    const n = { 一: 1, 半: 1, 两: 2 }[bare[1]]
    const mult = { 天: 1, 日: 1, 周: 7, 星期: 7 }[bare[2]] ?? 1
    return { days: n * mult, raw: bare[0] }
  }
  // 锚定式相对词（顺序：长的在前，避免"大前天"被"前天"截胡）
  const rel = [
    ['大前天', 3], ['前天', 2], ['昨天', 1], ['今天', 1], ['今日', 1],
    ['上上个月', 60], ['上个月', 30], ['上月', 30], ['这个月', 30], ['本月', 30],
    ['上上周', 14], ['上周', 7], ['这周', 7], ['本周', 7],
    ['去年', 365], ['今年', 365],
  ].find(([word]) => t.includes(word))
  if (rel) return { days: rel[1], raw: rel[0] }
  // 最宽一档：只说了"最近/近日/这几天"
  const loose = /(最近|近日|这几天|这几日|这两天|这两天|这些天|这段时间|近期|近来|最新)/.exec(t)
  if (loose) return { days: 7, raw: loose[0] }
  return null
}

/**
 * 自动回忆闸门：按会话缓存"搜好的记忆块"，供同步的上下文提供者读取。
 */
export class RecallGate {
  /**
   * @param {object} options
   * @param {import('./service.js').LibmemService} options.service - 用来调 memory_search。
   * @param {object} options.settings - 插件配置里的 `recall` 段。
   * @param {(message: string) => void} [options.log]
   */
  constructor ({ service, settings, log = () => {} }) {
    this.service = service
    this.settings = { ...DEFAULTS, ...(settings ?? {}), mode: normalizeMode((settings ?? {}).mode) }
    this.log = log
    /** @type {Map<string, {text: string, at: number, query: string, why: string, items: number}>} */
    this.cache = new Map()
    /** @type {Map<string, number>} 每个会话见过几条真人消息（用来识别"开局"）。 */
    this.seen = new Map()
    /**
     * @type {Map<string, number>} 每个会话的**代次**：每来一条真人消息 +1。
     * 用来丢掉"慢搜索回来时已经过期"的结果（见 `note()` 里的竞态注释）。
     */
    this.gen = new Map()
    /** 诊断：最近几次判断与注入情况。 */
    this.status = {
      enabled: this.settings.enabled !== false && this.settings.mode !== 'off',
      mode: this.settings.mode,
      discipline: this.settings.discipline !== false,
      decisions: 0,
      recalls: 0,
      /** 其中走"时间管道"（`memory_timeline`）的次数 —— 与按相关性的分开记，便于核对。 */
      recencyRecalls: 0,
      explicit: 0,
      coldStarts: 0,
      lastColdStart: null,
      skipped: 0,
      failures: 0,
      injections: 0,
      /** 因会话里来了更新的消息而被丢弃的过期结果（竞态保护，见 `note()`）。 */
      superseded: 0,
      /** 注入过记忆块、且模型确实回报过采用的轮数（插件侧唯一能看到的"算数了"凭据）。 */
      adopted: 0,
      lastDecision: null,
      lastRecall: null,
      lastInjection: null,
      lastFeedback: null,
    }
  }

  /**
   * 改档位（界面面板用）。**内存态**：重启回到配置值。
   *
   * 允许 `strict` / `smart` / `eager` / `off`；非法值回落 `smart`。
   * 面板只会传白名单里的值，这里再收一次是为了防手写 JSON 打进来。
   *
   * @param {unknown} mode
   * @returns {string} 实际生效的档位
   */
  setMode (mode) {
    const next = normalizeMode(mode)
    this.settings.mode = next
    this.status.mode = next
    this.status.enabled = this.settings.enabled !== false && next !== 'off'
    return next
  }

  /**
   * 开关「记忆纪律」提示词段（`lib/index.js` 的 `text()` 每次组装都会读它）。
   * @param {unknown} on
   * @returns {boolean}
   */
  setDiscipline (on) {
    const next = on !== false
    this.settings.discipline = next
    this.status.discipline = next
    return next
  }

  /** 当前该注入的纪律提示词（关掉时返回空串，harness 会跳过空上下文）。 */
  disciplineText () {
    if (this.settings.discipline === false) return ''
    if (this.settings.enabled === false) return ''
    return sanitize(buildRecallDiscipline(this.settings.mode))
  }

  /**
   * 收到会话事件：判断 + 搜索 + 落缓存。**永远不抛**（不能因为回忆失败影响对话）。
   *
   * @param {object} session
   * @param {object} event
   */
  async note (session, event) {
    // 档位可能在会话中途被面板改掉（off ↔ 其它）——每次事件都看当前值，不看构造时那个。
    if (this.settings.enabled === false || normalizeMode(this.settings.mode) === 'off') {
      this.status.enabled = false
      return
    }
    this.status.enabled = true
    try {
      if (!isHumanUserMessage(event)) return
      if (isSubagentSession(session)) return
      const sessionId = session?.id ?? 'unknown'
      const text = textOf(event.data?.content).trim()

      // ★ 2026-10-12 修竞态：本会话的**代次**。`note()` 是 `ctx.on` 触发的，
      //   返回的 Promise 没人 await ⇒ 两条消息的搜索可以并发；而搜索慢的时候
      //   （实测 1.3s），**旧消息的结果会晚于新消息写进缓存**，
      //   于是"好的"之后那一轮反而贴上了「关于上一个问题」的记忆（自检抓到的原话：
      //   断言 len=0、日志却记着 `lookback-word → 5 条`）。
      //   做法：每条消息进来自增代次，慢搜索回来时对不上就**丢弃**，不写缓存。
      const gen = (this.gen.get(sessionId) ?? 0) + 1
      this.gen.set(sessionId, gen)

      // 这是本会话（本插件生命周期内）第一条真人消息吗？→ 开局要多带一段"最近在做的事"
      const isFirstOfSession = !this.seen.has(sessionId)
      this.seen.set(sessionId, (this.seen.get(sessionId) ?? 0) + 1)

      const verdict = decide(text, this.settings)
      this.status.decisions += 1
      if (verdict.explicit) this.status.explicit += 1
      this.status.lastDecision = {
        at: new Date().toISOString(), recall: verdict.recall, why: verdict.why,
        explicit: verdict.explicit, mode: verdict.mode,
        chars: text.length, sessionId, coldStart: isFirstOfSession,
      }

      const blocks = []

      // ① 开局块：即使这条消息"不值得翻记忆"，也贴（短会话靠它接上下文）
      if (isFirstOfSession && this.settings.coldStart !== false) {
        try {
          const recent = await this.service.call('memory_recent', {
            days: this.settings.coldStartDays, limit: this.settings.coldStartItems,
          })
          const recentBlock = formatRecent(recent, this.settings)
          if (recentBlock) {
            blocks.push(recentBlock)
            this.status.coldStarts += 1
            this.status.lastColdStart = {
              at: new Date().toISOString(), items: Array.isArray(recent) ? recent.length : 0,
              chars: recentBlock.length, sessionId,
            }
            this.log(`开局回忆：最近 ${Array.isArray(recent) ? recent.length : 0} 条（${recentBlock.length} 字符）`)
          }
        } catch (error) {
          this.log(`开局回忆失败（不影响对话）：${String(error)}`)
        }
      }

      // ② 按这条消息的相关性再翻一次
      if (verdict.recall) {
        // ★ 2026-10-12：**时间范围问句要走"时间管道"**。
        //
        // 事故（用户原话）：「我是问最近或者这几天的工作做了什么，而你排上来是几个月前
        // 的工作」。根因不是排序调不好 —— 是**这一句本来就不该按相关性搜**：
        // 库里几个月前有大量"做了什么"的记录，与问句**确实很像**，所以怎么调权重都会混进来。
        // ⇒ 问"最近/这几天/N 天"时改用 `memory_timeline`（窗口内 + 时间倒序，不看相关性），
        //   并且**只贴清单块**（不再叠一份相关性结果，否则老的又混回来了）。
        const win = recentWindowDays(text)
        if (win) {
          try {
            const tl = await this.service.call('memory_timeline', {
              days: win.days, limit: Math.max(this.settings.maxItems, 12), max_tokens: 1500,
            })
            const items = tl?.items ?? []
            // ⚠️ 字段形状：`memory_search` 的条目把日期/类型放在 `header` 里，
            //    而 `memory_timeline`（B4 直出）放在**顶层** —— `formatRecent` 读顶层。
            //    不归一就会渲染成一排"无日期"（这类形状差异不报错，只是悄悄变丑）。
            const normalized = items.map((it) => ({
              memory_id: it?.memory_id,
              summary: it?.summary ?? '',
              source_date: it?.source_date ?? it?.header?.source_date ?? '',
              task_type: it?.task_type ?? it?.header?.task_type ?? '',
            }))
            const block = formatRecent(normalized, {
              ...this.settings, coldStartItems: Math.max(this.settings.maxItems, 12),
            }).replace('【最近在做什么】记忆库里最近', '【最近在做什么·按时间清单】记忆库里最近')
            if (block) blocks.push(block)
            this.status.recalls += 1
            this.status.recencyRecalls += 1
            this.status.lastRecall = {
              at: new Date().toISOString(), items: items.length, why: `timeline(${win.raw})`,
              chars: block.length, sessionId, query: text.slice(0, 60),
            }
            this.log(`自动回忆（时间管道 ${win.raw} → ${win.days} 天）：${items.length} 条（${block.length} 字符）`)
          } catch (error) {
            // 时间管道不可用（老内核没有 memory_timeline）→ 退回相关性检索，绝不让回忆失败
            this.log(`时间管道不可用，退回相关性检索：${String(error)}`)
            const result = await this.service.call('memory_search', {
              query: text, top_k: this.settings.maxItems, depth: 'L1',
            })
            const items = result?.items ?? []
            const block = format(items, this.settings, text)
            if (block) blocks.push(block)
            this.status.recalls += 1
          }
        } else {
          const result = await this.service.call('memory_search', {
            query: text, top_k: this.settings.maxItems, depth: 'L1',
          })
          const items = result?.items ?? []
          const block = format(items, this.settings, text)
          if (block) blocks.push(block)
          this.status.recalls += 1
          this.status.lastRecall = {
            at: new Date().toISOString(), items: items.length, why: verdict.why,
            chars: block.length, sessionId, query: text.slice(0, 60),
          }
          this.log(`自动回忆：${verdict.why} → ${items.length} 条（${block.length} 字符）`)
        }
      } else {
        this.status.skipped += 1
      }

      // 明确不翻时也落一条记录：否则上一轮的旧回忆会在这一轮继续被贴进去。
      // ★ 代次对不上 = 这条消息之后又来了新消息（它的缓存才是"当前"的）⇒ 丢弃，
      //   既不写缓存也不记 recalls（否则计数会虚高，掩盖了真正生效的那次）。
      if (this.gen.get(sessionId) !== gen) {
        this.status.superseded += 1
        this.log(`自动回忆：这条结果已过期（会话里来了更新的消息），丢弃（${verdict.why}）`)
        return
      }
      const merged = blocks.filter(Boolean).join('\n\n')
      this.cache.set(sessionId, {
        text: merged, at: Date.now(), query: text, why: verdict.why, gen,
        items: blocks.length ? undefined : 0,
      })
    } catch (error) {
      this.status.failures += 1
      this.log(`自动回忆失败（不影响对话）：${String(error)}`)
    }
  }

  /**
   * 组装输入时同步取缓存（**不能 await**，见文件头）。
   *
   * @param {unknown} scope - `AssembleContext.scope`，即当前 Agent（其 `id` 是会话 id）。
   * @returns {string} 要注入的文本；不需要注入时返回空串（harness 会跳过空上下文）。
   */
  textFor (scope) {
    if (this.status.enabled !== true) return ''
    const sessionId = scope?.id
    // 认不出是哪个会话时**宁可不注入** —— 贴错会话等于把别人的记忆灌进来。
    if (typeof sessionId !== 'string' || sessionId === '') return ''
    const entry = this.cache.get(sessionId)
    if (!entry || entry.text === '') return ''
    if (Date.now() - entry.at > this.settings.freshMs) return ''
    // 代次是防御性的第二道：写缓存时已经过滤过一次，这里再挡一次"条目比当前会话旧"。
    if (entry.gen != null && entry.gen !== this.gen.get(sessionId)) return ''
    this.status.injections += 1
    this.status.lastInjection = {
      at: new Date().toISOString(), sessionId, chars: entry.text.length, items: entry.items,
    }
    return entry.text
  }

  /**
   * ★ 2026-10-12：**模型真的把记忆用上了**的信号 —— 由工具侧在每次
   * `libmem_feedback` 成功后调一次。
   *
   * 为什么插件里还要记一笔：`injections` 只说明"贴进去了"，
   * `count`（Python 侧）说明"被采用过"。两者都涨才叫"有录入有检索有**输出**"，
   * 而用户抱怨的正是看不到输出。这里把"最近一次回报"和累计次数留在状态里，
   * 面板和 `libmem_status` 就能直接回答"记忆到底算不算数"。
   *
   * @param {{ids?: string[], sessionId?: string, query?: string}} info
   */
  noteFeedback (info = {}) {
    const ids = Array.isArray(info.ids) ? info.ids.filter(Boolean) : []
    this.status.adopted += 1
    this.status.lastFeedback = {
      at: new Date().toISOString(), ids, count: ids.length,
      sessionId: info.sessionId ?? null,
    }
  }

  /** 诊断文本（`libmem_status` 用）。 */
  describe () {
    const s = this.status
    const d = s.lastDecision
    const r = s.lastRecall
    const i = s.lastInjection
    const f = s.lastFeedback
    const lines = [
      `  自动回忆  ${s.enabled ? `开（mode=${s.mode}）` : '关'}` +
      `   判断 ${s.decisions} 次 · 翻 ${s.recalls}（时间管道 ${s.recencyRecalls}） · 点名 ${s.explicit} · 开局 ${s.coldStarts}` +
      ` · 跳过 ${s.skipped} · 注入 ${s.injections} · 采用 ${s.adopted} · 过期丢弃 ${s.superseded} · 失败 ${s.failures}`,
      `          档位      ${s.mode} —— ${RECALL_MODES[s.mode]?.hint ?? ''}`,
      `          记忆纪律  ${s.discipline ? '开（要求模型先读块、引用出处、用了就 libmem_feedback）' : '关（只贴块，不约束模型）'}`,
    ]
    if (d) {
      lines.push(`          最近判断：${d.recall ? '翻' : '不翻'}（${d.why}${d.explicit ? '·点名' : ''}，${d.chars} 字）` +
        `${d.coldStart ? ' · 会话开局' : ''}`)
    }
    if (s.lastColdStart) lines.push(`          最近开局：最近 ${s.lastColdStart.items} 条（${s.lastColdStart.chars} 字符）`)
    if (r) lines.push(`          最近翻到：${r.items} 条（${r.why}）「${r.query}…」`)
    if (i) lines.push(`          最近注入：${i.chars} 字符 @ ${i.at}`)
    if (f) lines.push(`          最近采用：${f.count} 条（${f.ids.join(', ') || '—'}）@ ${f.at}`)
    lines.push('          四档      ' + Object.keys(RECALL_MODES).join(' / ') +
      '（改 `recall.mode`，或在右下角「记忆」面板里切）')
    return lines.join('\n')
  }
}
