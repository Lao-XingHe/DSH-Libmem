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
      '请调 libmem_feedback 回报它的 memory_id —— 那是 count（被采用次数）唯一的来源。\n\n' +
      '★ 2026-11-01 起，返回里多一个 `enrichment` 字段（**先读它，再决定要不要继续调工具**）：\n' +
      '· `enrichment.topic` = 「这次命中的主题在馆里共 N 条，只给了你 M 条」。' +
      '看到这条就说明**你看到的只是冰山一角**，要全集就去调 libmem_collocate，要整份清单调 libmem_catalog。\n' +
      '· `enrichment.citations` = 每条结果的引文邻居 id（cites / cited_by）。' +
      '想知道「这条从哪来的」就调 libmem_citations。',
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
  {
    suffix: 'library',
    remote: 'library',
    readOnly: true,
    title: '馆藏统一入口',
    parameters: {
      action: {
        type: 'string',
        enum: ['search', 'browse', 'facets', 'guide'],
        default: 'facets',
        description: 'search=一次查记忆+知识库（标来源）；browse=按标目浏览（分类/年月/主题词）；facets=标目分布；guide=馆藏分类工作表格（给新装用户的维护说明）',
      },
      query: { type: 'string', description: 'search 用：自然语言查询' },
      sources: { type: 'string', default: 'memory,knowledge', description: 'search 用：memory,knowledge（逗号分隔）' },
      class_name: { type: 'string', description: 'browse 用：一级类目（软件与代码 / 数据与评测 / 系统与运维 / 内容与创作 / 研究与学习 / 工作与职业 / 生活与个人）' },
      sub_class: { type: 'string', description: 'browse 用：二级类目（如 检索与索引 / 插件与扩展 / 定时与任务）' },
      year: { type: 'integer', default: 0, description: 'browse 用：年份，0=不限' },
      month: { type: 'integer', default: 0, description: 'browse 用：月份，0=不限' },
      term: { type: 'string', description: 'browse 用：受控主题词（如「书房 os」）' },
      top_k: { type: 'integer', default: 5, description: 'search 用：每个来源返回几条' },
      limit: { type: 'integer', default: 20, description: 'browse/facets 用：返回条数上限' },
    },
    description:
      '馆藏统一入口（2026-10-24 起 Libmem 定位为「AI 馆藏」，记忆只是来源之一）。\n\n' +
      '· action=search：一次查记忆 + 知识库，每条带 source 标签（先并列，不做跨来源融合打分）。\n' +
      '· action=browse：按标目浏览——分类 / 年月 / 受控主题词，图书馆的「目录浏览」。\n' +
      '· action=facets：标目分布（有哪些分类、哪些月份、哪些高频主题词）—— 先看地图再翻书。\n' +
      '什么时候用：问「我都记了些什么」「按类别看看」「上个月记过啥」用 browse/facets；' +
      '问「关于 X 的记录」用 search；找具体某一条仍用 libmem_search（相关性更强）。',
  },
  {
    suffix: 'export',
    remote: 'library_export',
    readOnly: false,
    title: '导出 / 备份',
    parameters: {
      action: { type: 'string', enum: ['export', 'backup'], default: 'export', description: 'export=导出记忆（md/jsonl）；backup=全量备份 zip' },
      format: { type: 'string', enum: ['md', 'jsonl'], default: 'md', description: 'export 用：Markdown（人读）或 JSONL（机器）' },
      days: { type: 'integer', default: 0, description: 'export 用：只要最近 N 天，0=全部' },
      layer: { type: 'string', description: 'export 用：逗号分隔 cold,warm,hot；空=全部' },
      with_raw: { type: 'boolean', default: false, description: 'export 用：是否附上原文（L3）' },
    },
    description:
      '导出 / 备份馆藏。' + '\n' +
      '· export：把记忆导出为 Markdown（按日期分文件，人读）或 JSONL（机器迁移），输出到 data/exports/。' + '\n' +
      '· backup：全量备份 zip（冷库 + 温层 + header.db 一致性快照 + 配置脱敏），输出到 backups/。' + '\n' +
      '备份是动数据前的保险，导出是换机器/留档用的。',
  },
  {
    // ★ 2026-11-01（L2）：目录五功能里的「聚合」（卡特 collocation）。
    //   与 search 的分工是**完整性 vs 相关性**：这个答「一共有哪些」，一条不许少。
    suffix: 'collocate',
    remote: 'collocate',
    readOnly: true,
    title: '聚合清单（给全集）',
    parameters: {
      target: { type: 'string', required: true, description: '实体名（如「书房OS」）或类目名（如「软件与代码」）' },
      kind: { type: 'string', enum: ['entity', 'class'], default: 'entity', description: 'entity=按实体/主题词；class=按一级或二级类目' },
      order: { type: 'string', enum: ['time', 'heat', 'class'], default: 'time', description: 'time=时间倒序（默认）/ heat=热度高在前 / class=按类目' },
      limit: { type: 'integer', default: 0, description: '0=全部（单次硬上限 500）；>0=只取这么多' },
      offset: { type: 'integer', default: 0, description: '分页偏移，配合返回里的 next_offset' },
    },
    description:
      '**聚合清单**：属于某个实体/类目的**全部**条目，不是最像的 N 条。\n\n' +
      '与 libmem_search 的根本区别：\n' +
      '· libmem_search 答「哪条最像这句话」→ 排序问题，top-k 就够；\n' +
      '· 本工具答「关于这个主题，本馆究竟有哪些」→ **完整性**问题，一条都不许少、也不许重复。\n\n' +
      '所以它**故意不做相关性排序**，默认按时间倒序全部返回；返回里带 ' +
      '`total / returned / truncated / next_offset`，少给了你能看见。\n\n' +
      '会用两种写法合并（「书房OS」与「书房 OS」算同一个），合并情况报在 `merged_spellings`；' +
      '每条还带 `matched_by`（term=词表命中 / entity=实体直扫），来源不会静默混在一起。\n\n' +
      '什么时候用：问「X 一共有多少条」「关于 X 的全部记录」「这个类目下都有些什么」。',
  },
  {
    // ★ 2026-11-01（L1）：四法里的「引文法」—— 另一个检索入口：谱系，而不是相似度。
    suffix: 'citations',
    remote: 'citations',
    readOnly: true,
    title: '引文邻居（从哪来 / 谁接着）',
    parameters: {
      memory_id: { type: 'string', required: true, description: '记忆 id（search / collocate 返回的那个）' },
      direction: { type: 'string', enum: ['both', 'out', 'in'], default: 'both', description: 'out=它引用了谁；in=谁引用了它；both=两边都要' },
      limit: { type: 'integer', default: 20, description: '每个方向最多几条' },
    },
    description:
      '**引文邻居**：这条记忆是从哪条长出来的、哪条接着它。\n\n' +
      '与 libmem_search 的区别是**关系类型**不同：\n' +
      '· libmem_search 走**相似度**（"这条像不像你说的"）；\n' +
      '· 本工具走**谱系**（"这条是从那条来的"）。\n' +
      '相似的东西可以毫不相干，谱系不会 —— 所以它是另一个入口，不是更好的排序。\n\n' +
      '边有三种，返回里都带 kind：explicit（自己承认了"上次/刚才…"，0.9）/' +
      'successor（同一段对话里紧接在后 + 共享实体，0.6）/' +
      'same_thread（同月同类型 + 实体重叠 ≥2 的弱边，0.2）。\n\n' +
      '什么时候用：问「这是从哪来的」「后来接着做了什么」「这条改过没有」；' +
      '或者拿到一条记忆后想顺着线索往回/往前翻。',
  },
  {
    // ★ 2026-11-01（L3）：著录导出 —— 不是第三个抽屉，是 L1×L2 的合成品。
    suffix: 'catalog',
    remote: 'catalog_export',
    readOnly: true,
    title: '著录导出（带谱系的清单）',
    parameters: {
      target: { type: 'string', required: true, description: '实体名（如「书房OS」）或类目名（如「软件与代码」）' },
      kind: { type: 'string', enum: ['entity', 'class'], default: 'entity', description: 'entity=按实体；class=按类目' },
      format: { type: 'string', enum: ['md', 'json'], default: 'md', description: 'md=人读（可直接贴给人）；json=DC 记录数组' },
      with_citations: { type: 'boolean', default: true, description: '是否带上引文边（关掉=只要清单，更快）' },
      save: { type: 'boolean', default: false, description: '同时写一份到 data/exports/，返回里带 path' },
    },
    description:
      '**著录导出**：把一个主题/类目的全部条目渲染成一份著录清单（极简 DC）。\n\n' +
      '★ 它不是第三个平行的抽屉，是 **L1 × L2 的合成品**：\n' +
      '· 集合来自 libmem_collocate（L2）—— 本工具直接调它，口径不会分叉；\n' +
      '· 结构来自 libmem_citations（L1）—— 每条记录的「引文」是**真实抽取的谱系**（谁引用了谁）。\n\n' +
      '输出是**能直接给人看的东西**：一张款目头（主题/记录数/时间跨度/合并写法）+ 逐条著录' +
      '（题名/类目/主题词/来源/层/引文）。`total` 一定等于清单里的记录条数。\n\n' +
      '什么时候用：用户说「把这些整理成一份清单」「导出这个主题的全部记录」「给我看看 X 的历史」。',
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
