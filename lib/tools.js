/**
 * LibMem 的原生工具面（14 个）。
 *
 * 每个工具都是一层**薄壳**：参数 → HTTP → Python 侧 `libmem_tools.py` 的实现。
 * 这里不做任何业务判断，唯一例外是 `libmem_status`（本地诊断，不调后端）。
 *
 * 为什么返回值统一是**字符串**：工具输出会被 `validateJsonSchemaValue` 按
 * `output.schema` 校验，字符串是唯一不会因为后端把 dict 换成 list 而炸的类型；
 * 而且旧 MCP 路径给模型的本来就是 JSON 文本，这样模型看到的东西没变。
 *
 * @module dsh-libmem/tools
 */
import { defineTool } from '@deepseek-ai/dsh-tools'

/**
 * 把任意工具结果格式化成模型可读文本。
 * @param {any} result
 * @returns {string}
 */
function toText (result) {
  if (typeof result === 'string') return result
  if (result === null || result === undefined) return '(空结果)'
  try {
    return JSON.stringify(result, null, 2)
  } catch {
    return String(result)
  }
}

/**
 * 工具表。
 *
 * `remote` 是 Python 侧的名字（`libmem_tools.TOOLS` 的键）；工具名 = `<prefix>_<suffix>`。
 */
export const TOOL_SPECS = [
  {
    suffix: 'search',
    remote: 'memory_search',
    readOnly: true,
    title: '检索记忆',
    parameters: {
      query: { type: 'string', required: true, description: '搜索 query（自然语言）' },
      top_k: { type: 'integer', default: 5, description: '返回条数，默认 5' },
      depth: { type: 'string', enum: ['L1', 'L2', 'L3'], default: 'L1', description: 'L1=摘要 / L2=片段 / L3=原文' },
      // ★ 2026-10-12：时间窗口查询 —— >0 时**完全换管道**（窗口内 + 时间倒序，不看相关性）
      recent_days: {
        type: 'integer',
        default: 0,
        description: '只看最近 N 天（按时间倒序，不看相关性）。「最近几天我做了什么」用这个；0=按相关性正常检索',
      },
    },
    description:
      '记忆检索（B4 双阶段门控）：Phase1 SQLite 过滤 → Phase2 bge-m3 余弦 + heat/count 精排 → 预算裁剪。' +
      '默认返回 L1 摘要层；要细节用 libmem_drilldown。\n\n' +
      '★ **问"最近/这几天/这周"时，传 `recent_days`**（或直接用 `libmem_recent`）—— ' +
      '那类问题是**枚举**（"这段时间发生过什么"），按相关性排会翻出几个月前的老记忆，' +
      '而且它们跟问句**确实很像**，所以不管怎么调权重都会混进来。传了 `recent_days` 就换成' +
      '"窗口内 + 时间倒序"，不再看相关性。\n\n' +
      '返回里带一个 `dimension` 字段（这句话属于哪一类问题：查找 A / 汇合 B / 同一性 D / 操作 E…），' +
      '**只作提示** —— 要不要多要几条、要不要原文（depth=L2/L3）仍由你自己判断。\n\n' +
      '⚠️ 如果这一轮的输入里已经有【自动回忆】块（插件按你的话自动搜好贴进来的），先看那份，' +
      '需要更多或更准时再调本工具。**但"一次没搜到"不等于"记忆里没有"** —— ' +
      '换个说法、换关键词再搜一次是正解；不许就此说"我不记得"。\n\n' +
      '⚠️ 返回的只是**候选**：这次调用本身只说明「这些被翻出来了」，不代表被用上了。' +
      '如果你真的把某条的 summary 用进了回答（作为参考、依据，或影响了判断），' +
      '请调 libmem_feedback 回报它的 memory_id —— 那是 count（被采用次数）唯一的来源。',
  },
  {
    // ★ 2026-10-12：按时间**列清单**的专用工具（用户报"问最近却给几个月前"后加的）。
    suffix: 'recent',
    remote: 'memory_timeline',
    readOnly: true,
    title: '按时间列记忆清单',
    parameters: {
      days: { type: 'integer', default: 7, description: '最近多少天内（按日期；0=不限）' },
      limit: { type: 'integer', default: 20, description: '最多返回几条（默认 20，清单题可开到 50）' },
      max_tokens: { type: 'integer', default: 1500, description: '正文预算上限' },
      layer: { type: 'string', description: '只取某一层：hot / warm / cold（留空=全部）' },
    },
    description:
      '按时间列清单（**不看相关性**）：窗口内 + 时间倒序 + 尽量列全。' +
      '专治「这几天 / 最近一周我干了什么」这类**枚举**问题。\n\n' +
      '⚠️ 与 `libmem_search` 的分工：那个是"关于 X 我记得什么"（按相关性），' +
      '这个是"这段时间里发生过什么"（按时间）。**问时间范围就该用这个** —— ' +
      '用 libmem_search 会翻出几个月前的高热度老记忆（它们和问句同样相似）。\n\n' +
      '⚠️ 它**不给热度升温**（列清单不是"检索命中"），所以不会污染"常用浮现"的排序闭环。',
  },
  {
    suffix: 'search_simple',
    remote: 'memory_search_simple',
    readOnly: true,
    title: '简易检索记忆',
    parameters: {
      query: { type: 'string', required: true, description: '搜索 query（自然语言）' },
      top_k: { type: 'integer', default: 5, description: '返回条数，默认 5' },
    },
    description:
      '简易检索：不带 MAP 协议信封，直接返回摘要列表，适合快速扫一眼。\n\n' +
      '⚠️ 它**故意绕过 MAP validator**：返回类型是裸 list，装不下 `needs_clarification` / `empty` / `degraded`，' +
      '所以三条硬规则（不猜指代 / 不编造 / 不越权）在这条路上**不生效**。需要协议保证时用 libmem_search。\n\n' +
      '⚠️ 返回的同样是**候选**；真的用进了回答的那几条，请调 libmem_feedback 回报 memory_id。',
  },
  {
    suffix: 'drilldown',
    remote: 'memory_drilldown',
    readOnly: true,
    title: '深挖单条记忆',
    parameters: {
      memory_id: { type: 'string', required: true, description: '记忆 id（列表里那条 header 的 memory_id）' },
      depth: { type: 'string', enum: ['L2', 'L3'], default: 'L2', description: 'L2=片段 / L3=原文' },
      max_tokens: { type: 'integer', default: 1500, description: '返回文本的 token 上限' },
    },
    description:
      '深挖单条记忆：L1（摘要）→ L2（片段）或 L3（原文）。' +
      '校验、取原文、B7 审计都在 service 层（`B6.drilldown_handler`），这条路只是转发。',
  },
  {
    suffix: 'feedback',
    remote: 'memory_feedback',
    readOnly: false,
    title: '回报记忆被采用',
    parameters: {
      memory_ids: {
        type: 'array',
        items: { type: 'string' },
        required: true,
        description: '真正被采用的那几条 memory_id（重复项自动去重）',
      },
    },
    description:
      '回报「这些记忆我真的用上了」—— count（被采用次数）唯一的来源。\n\n' +
      '**什么时候该调**：你把 libmem_search / libmem_search_simple 返回的某条内容**实际用进了这一轮回答**。' +
      '**没用到就不要调**：宁可少报，不要凑数。也不要图省事把整批搜索结果原样报回来 —— ' +
      'count 就是用来区分「翻出来过」（heat）和「真的用过」（count）的。\n\n' +
      '有副作用：每条 count +1、heat +5.0，并留 B7 审计。调一次算一次采用，别在重试里重复调。',
  },
  {
    suffix: 'write',
    remote: 'memory_write',
    readOnly: false,
    title: '写入冷记忆',
    parameters: {
      user_message: { type: 'string', required: true, description: '用户那一轮的原话' },
      assistant_message: { type: 'string', required: true, description: '助手那一轮的回答' },
    },
    description:
      '写一条记忆进冷库（B1）并触发温提炼，返回落盘 id。\n\n' +
      '⚠️ 正常对话**不需要**手动调它：写入侧由插件的会话监听/文件轮询自动完成。' +
      '只在「自动路径没覆盖到的内容需要补记」时才用。',
  },
  {
    suffix: 'extract',
    remote: 'memory_extract',
    readOnly: false,
    title: '触发温提炼',
    parameters: {},
    description:
      '手动触发一次全量温提炼扫描（冷库 → warm → B3 索引）。' +
      '正常由插件托管的服务定时自动跑；只有刚补灌了一批历史、想让它们立刻可检索时才需要。',
  },
  {
    // ★ 2026-10-12：整链体检（P2）—— 让"有没有掉件"变成一个可随时问的问题。
    suffix: 'chain',
    remote: 'chain_check',
    readOnly: true,
    title: '整链体检',
    parameters: {},
    description:
      '整链体检（**只读**）：冷库 → 温层 → 索引 三层对账，回答"有没有掉件、有没有重复"。' +
      '检查项：冷库重复写入、温层 ref 失效、待提炼积压、索引孤儿行、索引同摘要重复、空摘要。\n\n' +
      '**什么时候该调**：① 怀疑记忆丢了或搜不到；② 觉得检索结果重复、名额被占；' +
      '③ 改过提炼/索引之后想确认没弄坏。\n\n' +
      '⚠️ 报告里「历史行」是 v1 迁移来的、没有温层对应的老行，**正常**，不要试图清它' +
      '（`index_warm(prune=True)` 会把它们全删 —— 那是丢数据）。',
  },
  {
    suffix: 'stats',
    remote: 'memory_stats',
    readOnly: true,
    title: '记忆统计',
    parameters: {},
    description:
      '记忆库现状：B3 SQLite 总条数、分层分布（cold/warm/hot）、嵌入缓存覆盖率与体积。' +
      '「搜不到」时先看这里 —— 总量为 0 和「没匹配」长得一样。',
  },
  {
    suffix: 'knowledge_search',
    remote: 'knowledge_search',
    readOnly: true,
    title: '知识库检索',
    parameters: {
      query: { type: 'string', required: true, description: '关键词 / 自然语言' },
      top_k: { type: 'integer', default: 5, description: '返回条数' },
      with_related: { type: 'boolean', default: false, description: '给前几条附上 related_memories（知识条目 ↔ 记忆条目双向关联）' },
    },
    description:
      '知识库（文档）全文检索。与记忆检索是两套索引，别混用。\n\n' +
      '⚠️ 返回裸 list：「索引是空的」和「没有匹配」在它这里长得一样 —— ' +
      '分不清时先调 libmem_knowledge_stats 看 docs_in_index。',
  },
  {
    suffix: 'knowledge_reindex',
    remote: 'knowledge_reindex',
    readOnly: false,
    title: '重建知识库索引',
    parameters: {},
    description:
      '重建知识库索引（新增/改动了知识库文件之后跑一次）。' +
      '返回完整报告：files / indexed / skipped（每项带跳过原因）/ truncated / docs_in_index / readers。',
  },
  {
    suffix: 'knowledge_stats',
    remote: 'knowledge_stats',
    readOnly: true,
    title: '知识库现状',
    parameters: {},
    description:
      '知识库现状：目录里的文件数、索引里的文档数、各类解析器是否可用。' +
      '存在的理由就是 knowledge_search 返回裸 list —— 「文档在但搜不到」这种状态不这样看会被瞒很久。',
  },
  {
    suffix: 'knowledge_read',
    remote: 'knowledge_read',
    readOnly: true,
    title: '读知识库文件',
    parameters: {
      path: { type: 'string', required: true, description: '知识库内文件路径（.txt/.md/.docx/.pdf 等）' },
      max_chars: { type: 'integer', default: 3000, description: '最多返回多少字符' },
    },
    description: '读取知识库文件的正文内容（检索命中之后取全文用）。',
  },
  {
    suffix: 'audit_recent',
    remote: 'audit_recent',
    readOnly: true,
    title: '最近审计日志',
    parameters: {
      limit: { type: 'integer', default: 20, description: '返回条数' },
    },
    description: 'B7 审计日志（写入 / 检索 / 深挖 / 热度衰减逐条留痕）。',
  },
  {
    suffix: 'audit_verify',
    remote: 'audit_verify',
    readOnly: true,
    title: '校验记忆完整性',
    parameters: {
      memory_id: { type: 'string', required: true, description: '记忆 id' },
    },
    description: '验证单条记忆的完整性 hash（B7）—— 判断库里这条有没有被外部改动过。',
  },
]

/**
 * 把工具面注册到 `ctx.tools`。
 *
 * @param {object} ctx - Cordis 插件上下文（须已注入 `tools`）。
 * @param {object} options
 * @param {string} options.prefix - 工具名前缀（config.toolPrefix）。
 * @param {import('./service.js').LibmemService} options.service
 * @param {() => Promise<string>} options.statusText - `libmem_status` 的正文。
 * @param {(info: {ids: string[]}) => void} [options.onFeedback]
 *   `libmem_feedback` 成功后的回调（★ 2026-10-12）—— 交给自动回忆闸门记一笔"采用"。
 *   失败不影响工具结果：这条只是诊断用的旁路。
 * @returns {() => void} 注销全部工具的 disposer。
 */
export function registerLibmemTools (ctx, { prefix, service, statusText, onFeedback = () => {} }) {
  const disposers = []

  for (const spec of TOOL_SPECS) {
    const name = `${prefix}_${spec.suffix}`
    disposers.push(ctx.tools.register(defineTool({
      name,
      description: spec.description,
      parameters: spec.parameters,
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute (args, exec) {
        const result = await service.call(spec.remote, args, exec.signal)
        // ★ 只认 `feedback`：它是"记忆真的被用上了"的唯一入口（count 的来源）。
        //   放在工具成功之后、try 里包住 —— 回报失败绝不能把工具结果改坏。
        if (spec.suffix === 'feedback') {
          try {
            const ids = Array.isArray(args?.memory_ids) ? args.memory_ids : []
            onFeedback({ ids })
          } catch {
            /* 旁路：诊断丢了就丢了 */
          }
        }
        return toText(result)
      },
      isConcurrencySafe: () => spec.readOnly === true,
      presentCall: (args) => ({
        card: 'generic',
        title: spec.title ?? name,
        kind: spec.readOnly ? 'search' : 'other',
        rawInput: args,
      }),
    })))
  }

  // 本地诊断工具：不碰后端，只报「服务在不在、路径解析成什么、写入侧写到哪」。
  disposers.push(ctx.tools.register(defineTool({
    name: `${prefix}_status`,
    description:
      'LibMem 插件自检：Python 服务是否在跑（复用/自起/掉线）、端口与 pid、' +
      '服务解析出的数据路径、调用计数与最近错误、写入侧（冷库）状态。' +
      '记忆工具报错时先看这个，它能区分「服务没起来」和「这条查不到」。',
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute () {
      return await statusText()
    },
    isConcurrencySafe: () => true,
    presentCall: () => ({ card: 'generic', title: 'LibMem 自检', kind: 'other' }),
  })))

  return () => {
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        /* 卸载期的失败不该盖住真正的错误 */
      }
    }
  }
}
