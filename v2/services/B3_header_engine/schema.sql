-- B3 表头引擎 · SQLite Schema
-- 来源：《书房 OS 迁入 dsh · 执行任务清单 P1-1》
-- 三表：memories（主表）+ audit_log（审计）+ frames（协议帧）

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- memories · 记忆主表（物理防火墙）
-- 10 万条规模带过滤查询目标 < 10ms
-- ============================================================
CREATE TABLE IF NOT EXISTS memories (
    memory_id       TEXT PRIMARY KEY,                -- 全局唯一 ID (md5 或 uuid4)
    layer           TEXT NOT NULL DEFAULT 'cold',    -- cold/warm/hot
    content_path    TEXT,                            -- 原文路径（冷记忆文件位置）
    summary         TEXT,                            -- 温记忆提炼的一句话摘要
    task_type       TEXT,                            -- 七类：研究/决策/验证/规划/闲聊/求助/其他
    session_id      TEXT,                            -- 来源会话
    time_bucket     TEXT,                            -- YYYY-MM 分桶，加速按月过滤
    core_entities   TEXT NOT NULL DEFAULT '[]',     -- JSON 数组，核心名词/项目/技术
    emotional_tone  TEXT,                            -- 情感基调（如中性/积极/焦虑）
    source_weight   REAL NOT NULL DEFAULT 1.0,      -- 来源权重（用户确认可加）
    token_peaks     INTEGER NOT NULL DEFAULT 0,      -- token 峰值（用于预算裁剪参考）
    heat            REAL NOT NULL DEFAULT 0.0,        -- 热度（时间衰减）
    count           INTEGER NOT NULL DEFAULT 0,       -- 被采用次数（只增不减；只由 B5 `adopted` 产生）
    legacy_retrieval_count INTEGER NOT NULL DEFAULT 0, -- 遗留归档：2026-09-25 前由「检索命中」累加的旧值。
                                                       -- 语义已作废（命中口径改为「被采用」），**不参与打分**，仅存档防丢。
                                                       -- 见 bin/clean-legacy-count.py
    confidence      REAL NOT NULL DEFAULT 1.0,        -- 置信度（0~1）
    created_at      TEXT NOT NULL,                   -- ISO 8601
    updated_at      TEXT NOT NULL,                   -- ISO 8601
    data_sovereignty TEXT NOT NULL DEFAULT 'app',      -- 数据主权：app/user
    relations       TEXT NOT NULL DEFAULT '[]',      -- Phase2: JSON 数组，人物/概念关系链（来自温层提炼）
    source_date     TEXT,                            -- Phase2: YYYY-MM-DD 源文件日期（从 content_path 提取）
    has_l3          INTEGER NOT NULL DEFAULT 0,       -- Phase2: 1=content_path 文件存在（可 drilldown 到 L3）
    -- ★ V1.1：最近一次「被检索命中 / 被采用」的时间（epoch 秒），0 = 从未。
    --   为什么不复用 updated_at：衰减任务会按设计把**全库** updated_at 刷成当下
    --   （它是「热度基准时刻」，不是「最近使用时刻」），所以后者不能当近期信号用。
    --   纯增量列，存量库由 `ensure_schema()` 自动 ALTER 补上。
    last_hit_epoch  REAL NOT NULL DEFAULT 0
);

-- Phase1 过滤核心索引（WHERE 组合条件）
-- 10 万行规模下复合索引是 Phase1 从全表扫 → 索引扫的关键。
-- 单列索引保留作 fallback（只传一个条件时仍可用），但实际过滤几乎都是多条件组合。
CREATE INDEX IF NOT EXISTS idx_memories_task_type  ON memories(task_type);
CREATE INDEX IF NOT EXISTS idx_memories_time_bucket ON memories(time_bucket);
CREATE INDEX IF NOT EXISTS idx_memories_layer       ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_memories_heat_desc   ON memories(heat DESC);
-- 复合索引：覆盖检索最常见的过滤组合
-- (task_type, layer, time_bucket, heat DESC) —— 四层组合，heat DESC 让热门排在前面
CREATE INDEX IF NOT EXISTS idx_memories_combo  ON memories(task_type, layer, time_bucket, heat DESC);
-- (layer, task_type) —— 两层组合（很多场景只传这两个，比三层少一个约束）
CREATE INDEX IF NOT EXISTS idx_memories_layer_task ON memories(layer, task_type);
-- ★★ 2026-10-02 补：`session_id` 索引（**第三版才想到，而且是实测逼出来的**）
--
-- 为什么必须补：`session_id` 有两个用途，**两边都靠它做范围查**：
--   ① Phase1 限定语料（评测里"本题的 haystack"、生产里"只在这段对话里搜"）；
--   ② 「命中即扩窗」（`neighbours_of`）按 `session_id` 取会话内前后 N 条。
-- 而扩窗的写法是 `WHERE session_id=? AND rowid<=? ORDER BY rowid DESC LIMIT 9`：
-- 没有这个索引时，SQLite 只能从 `rowid` 处**一路向前扫**，
-- **当命中是某个会话的第一轮时，要扫过它前面的所有行** ——
-- 实测代价：第二轮全量跑到 **1 题/分钟**（LLM 调用本身只占 12.7s），
-- 整卷 500 题要 **8 小时以上**。补索引后是 O(9) 的索引定位。
--
-- 用 `(session_id)` 单列即可：SQLite 的索引天然带 rowid，
-- 所以 `session_id=? AND rowid<=?` 能同时吃到等值与范围两个条件。
CREATE INDEX IF NOT EXISTS idx_memories_session     ON memories(session_id);

-- core_entities JSON 查询支持（SQLite 3.38+ 的 json_each）
-- 不建索引，运行时用 json_each(memories.core_entities) 过滤
-- 10 万行以上考虑 FTS5 虚拟表，但当前规模单列索引 + 复合索引够用

-- ============================================================
-- audit_log · 审计日志（全动作覆盖）
-- 完整性保护：integrity_hash = sha256(header_snapshot || payload_ref)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    -- 主键是 (trace_id, action)，不是 trace_id 单列。
    --
    -- 为什么：架构书 §5.9 要求审计「覆盖 search/write/drilldown/export/delete 全动作」，
    -- 一条 trace 里本就会发生多个动作 —— 例如「检索命中(search) + 命中升温改库(write)」
    -- 是同一条因果链的两条证据。若主键只有 trace_id，配合 INSERT OR REPLACE 会互相覆盖：
    -- 实测升温和检索共用 trace_id 时，后写的把先写的顶掉，**检索记录直接消失**。
    trace_id          TEXT NOT NULL,                  -- 追踪 ID（同一链路的多个动作共用）
    request_id        TEXT,                           -- 请求 ID
    session_id        TEXT,                           -- 会话 ID
    ts                TEXT NOT NULL,                   -- ISO 8601
    actor             TEXT,                           -- 动作发起者（agent/user/system）
    action            TEXT NOT NULL,                  -- 动作类型：search/write/drilldown/export/delete
    header_snapshot   TEXT,                           -- 动作发生时的表头快照（JSON）
    payload_ref       TEXT,                           -- 载荷引用（memory_id 列表或文件路径）
    decided_by        TEXT NOT NULL DEFAULT 'user',   -- 硬规则：记忆层只执行，决策权在用户
    integrity_hash    TEXT NOT NULL,                  -- sha256(header_snapshot + payload_ref)
    PRIMARY KEY (trace_id, action)
);

CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts     ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_trace  ON audit_log(trace_id);

-- ============================================================
-- frames · 协议帧（MAP 三包落地）
-- 版本化的请求/响应包，支持 replay、审计签名
-- ============================================================
CREATE TABLE IF NOT EXISTS frames (
    frame_id          TEXT PRIMARY KEY,              -- 帧 ID
    frame_type        TEXT NOT NULL,                  -- request/response/drilldown
    protocol_ver      TEXT NOT NULL DEFAULT 'v0.1',   -- MAP 版本
    header_json       TEXT NOT NULL,                  -- 帧头（JSON）
    metadata_json     TEXT,                           -- 元数据（JSON）
    payload_path      TEXT,                           -- 载荷文件路径（或内联）
    footer_crc32      TEXT,                           -- 帧尾 CRC32 校验
    audit_signature   TEXT,                           -- 审计签名（关联 audit_log.trace_id）
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_frames_type     ON frames(frame_type);
CREATE INDEX IF NOT EXISTS idx_frames_created  ON frames(created_at);
CREATE INDEX IF NOT EXISTS idx_frames_audit    ON frames(audit_signature);

-- ==================================================================
-- P1 馆藏对象模型（2026-10-24）
--
-- 为什么加这三张表：Libmem 定位从「记忆插件」升级成「AI 馆藏」，
-- 记忆只是其中一种来源。三层对象：
--   assets       原始资产（对话/文档/图片…），一件资产可切出多条 items
--   items        可检索单元（现在一条记忆 = 一条 item，item_id 复用 memory_id）
--   descriptions 标目（分类/主题词/时间/类型/来源）—— P0 编目层的产出
--
-- 全部是**新增表**，不改 memories；老代码照跑，出问题删表即回滚。
-- ==================================================================
CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,                -- 资产 id（对话：按冷库文件去重）
    kind         TEXT NOT NULL DEFAULT 'dialog',  -- dialog / doc / image / video / web
    uri          TEXT,                            -- 原件位置（冷库路径 / 文件路径）
    hash         TEXT,                            -- 内容指纹（去重、迁移用）
    created_at   TEXT NOT NULL,                   -- ISO 8601
    meta_json    TEXT NOT NULL DEFAULT '{}'       -- 各类型自己的元数据
);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);

CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,                -- 现在复用 memory_id
    asset_id     TEXT,                            -- 指向 assets
    seq          INTEGER NOT NULL DEFAULT 0,      -- 一件资产内的顺序（文档分块/视频分段用）
    text         TEXT,                            -- 可检索正文（对话：摘要；文档：分块原文）
    summary      TEXT,                            -- 提炼摘要
    char_start   INTEGER,                         -- 在原件里的起止（可回溯）
    char_end     INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_asset ON items(asset_id);

CREATE TABLE IF NOT EXISTS descriptions (
    item_id      TEXT PRIMARY KEY,                -- 一条 item 一份标目
    class        TEXT NOT NULL DEFAULT '其他',     -- 一级类目（通用知识工作域）
    sub_class    TEXT,                            -- 二级类目（数据驱动：类内主题簇）
    alt_class    TEXT NOT NULL DEFAULT '',        -- 副类（第二分类号，多标签用）
    terms        TEXT NOT NULL DEFAULT '[]',      -- 受控主题词 JSON 数组
    year         INTEGER, month INTEGER, week INTEGER, day INTEGER,
    task_type    TEXT,                            -- 沿用原来的活动类型
    source       TEXT NOT NULL DEFAULT 'dialog',  -- 来源：dialog / doc / image
    confidence   REAL NOT NULL DEFAULT 0.5,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desc_class ON descriptions(class);
CREATE INDEX IF NOT EXISTS idx_desc_time  ON descriptions(year, month);

-- ==================================================================
-- P0 编目层：受控词表 + 倒排（2026-10-24）
-- terms     受控词表（canonical + 别名），doc_freq 用于 idf 加权
-- mem_terms 倒排表（哪条 item 含哪个规范词、权重）
-- ==================================================================
CREATE TABLE IF NOT EXISTS terms (
    term_id      TEXT PRIMARY KEY,                -- sha1(canonical)[:12]
    canonical    TEXT NOT NULL UNIQUE,            -- 规范词
    kind         TEXT NOT NULL DEFAULT 'entity',  -- entity / class / topic
    aliases_json TEXT NOT NULL DEFAULT '[]',      -- 同义/别名（原词保留在这）
    doc_freq     INTEGER NOT NULL DEFAULT 0       -- 含该词的 item 数
);
CREATE INDEX IF NOT EXISTS idx_terms_canonical ON terms(canonical);

CREATE TABLE IF NOT EXISTS mem_terms (
    item_id      TEXT NOT NULL,
    term_id      TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (item_id, term_id)
);
CREATE INDEX IF NOT EXISTS idx_mem_terms_term ON mem_terms(term_id);
