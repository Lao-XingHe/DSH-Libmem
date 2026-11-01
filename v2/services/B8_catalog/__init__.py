"""B8 编目层 —— 馆藏对象模型 + 标目（P0/P1 的基础）。

定位
----
Libmem 从「记忆插件」升级成「AI 馆藏」之后，一条记忆只是馆藏里的一种
**条目**。编目（cataloging）负责给每个条目打上检索用的「标目」：

    条目(item) ← 来自 → 资产(asset：对话/文档/图片…)
        └─ 标目(description)：分类 + 受控主题词 + 年月周日 + 活动类型 + 来源

现在这一版只做**对话**这一种资产；但表结构是通用的，后面加文档/图片不用改。
分类用**规则**判（关键词打分），不做 4B 打标 —— 857 条 × 6 秒不值得，而且规则可解释、
可回归测试。等受控词表跑顺了，再用它给分类做兜底。

本模块的纪律
------------
* 只**新增**表，绝不改 memories；
* 任何一步失败都不许影响原有写入/检索（调用方一律包 try/except）；
* 幂等：同一 memory_id 反复编目 = 覆盖同一条标目，不产生重复。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import unicodedata

# ---------------------------------------------------------------- 分类骨架
#
# 10 个一级类，是「个人馆藏」的骨架，不是学科分类（中图法对个人记忆没意义）。
# 关键词命中打分；`entities` 的权重 ×2（实体比摘要里的普通词更有指示性）。
CLASS_RULES: dict[str, list[str]] = {
    # ------------------------------------------------------------------
    # 通用骨架（2026-10-24 重构）：**按"知识工作域"分类，不按某个人的项目分类**。
    #
    # 图书馆的做法是「稳定类目 + 通用概念」，不是把某位读者的项目名写成类名 ——
    # 否则换个用户整套就不成立。所以这里：
    #   * 类目 = 跨行业都成立的工作域（软件/数据/系统/内容/研究/职业/生活）；
    #   * 词表 = 中英双语通用词，**不放任何个人项目名 / 产品名**；
    #   * 形式轴（决策/研究/验证…）不在这里 —— 它是 `task_type`，与主题域正交。
    # 将来别人用，只需加减自己行业的通用词，不必重写类目。
    # ------------------------------------------------------------------
    "软件与代码": ["代码", "接口", "api", "插件", "版本", "仓库", "git", "commit", "分支",
                   "合并", "bug", "修复", "重构", "调试", "部署", "编译", "依赖", "sdk",
                   "前端", "后端", "脚本", "函数", "模块", "程序", "软件", "命令行",
                   "系统", "架构", "算法", "检索", "索引", "缓存", "向量", "流程",
                   "自动化", "数据结构", "开发", "测试",
                   "code", "plugin", "release", "patch", "refactor", "debug", "deploy"],
    "数据与评测": ["数据", "指标", "评测", "基准", "分数", "排名", "召回", "准确率", "精确率",
                   "统计", "实验", "对比", "样本", "数据集", "基准测试", "榜单", "跑分",
                   "benchmark", "dataset", "metric", "eval", "score", "recall",
                   "precision", "leaderboard", "模型", "训练", "推理", "调参", "过拟合"],
    "系统与运维": ["服务", "服务器", "端口", "日志", "进程", "定时", "任务计划", "重启",
                   "监控", "容器", "数据库", "备份", "磁盘", "内存", "显存", "网络", "隧道",
                   "dns", "证书", "配置", "环境", "安装", "权限", "部署", "运维", "开机自启",
                   "cron", "server", "service", "log", "backup", "config", "install",
                   "docker", "sqlite"],
    "内容与创作": ["写作", "文档", "文章", "大纲", "小说", "剧本", "故事", "图片", "生图",
                   "图像", "提示词", "设计", "视频", "音频", "排版", "海报", "绘画", "素材",
                   "prompt", "image", "video", "design", "writing", "doc", "article"],
    "研究与学习": ["研究", "调研", "论文", "学习", "课程", "资料", "方法", "原理", "理论",
                   "综述", "参考", "文献", "阅读", "笔记", "research", "paper", "study",
                   "learn", "survey", "theory", "reading"],
    "工作与职业": ["工作", "求职", "简历", "面试", "职业", "公司", "客户", "会议", "汇报",
                   "合同", "工资", "同事", "团队", "需求", "交付", "排期",
                   "job", "resume", "interview", "client", "meeting", "team", "career"],
    "生活与个人": ["生活", "健康", "旅行", "饮食", "购物", "日程", "记账", "家庭", "朋友",
                   "爱好", "运动", "睡眠", "医院", "旅游", "孩子", "宠物",
                   "life", "health", "travel", "food", "family", "hobby", "schedule"],
}
# ---------------------------------------------------------------- 二级类目骨架（通用）
#
# 一级类是"哪个知识域"，二级类是"这个域里在干什么"。二级必须**可读且覆盖得住**，
# 否则全是"未细分"就等于没有小类。所以这里给一套通用二级骨架（中英双语），
# 用词尽量是行业通用动作/对象，不含个人项目名。
# 本地差异仍走 class_lexicon.yaml 的 sub_extends 扩展。
SUB_CLASS_RULES: dict[str, dict[str, list[str]]] = {
    "软件与代码": {
        "插件与扩展": ["插件", "扩展", "注入", "挂载", "钩子", "plugin", "extension", "hook"],
        "检索与索引": ["检索", "索引", "查询", "召回", "向量", "嵌入", "搜索", "排序", "重排",
                      "缓存", "分词", "search", "index", "retrieval", "embedding", "ranking", "cache"],
        "版本与发布": ["版本", "发布", "打包", "升级", "回滚", "基线", "release", "version", "package", "deploy"],
        "调试与修复": ["bug", "修复", "报错", "异常", "调试", "崩溃", "失败", "测试", "日志",
                      "error", "fix", "debug", "crash", "test"],
        "接口与集成": ["接口", "api", "集成", "对接", "协议", "sdk", "webhook", "integration", "protocol"],
        "架构与设计": ["架构", "设计", "模块", "重构", "方案", "分层", "系统", "流程",
                      "architecture", "design", "refactor", "module", "flow"],
    },
    "数据与评测": {
        "基准与榜单": ["基准", "榜单", "排名", "跑分", "benchmark", "leaderboard", "rank"],
        "指标与统计": ["指标", "统计", "分数", "准确率", "召回", "metric", "score", "recall", "precision"],
        "实验与对比": ["实验", "对比", "对照", "ab", "样本", "experiment", "compare", "control"],
        "数据集与处理": ["数据集", "数据", "清洗", "标注", "dataset", "data", "label"],
        "模型效果": ["模型", "训练", "推理", "调参", "过拟合", "model", "train", "inference"],
    },
    "系统与运维": {
        "服务与部署": ["服务", "部署", "进程", "重启", "启动", "server", "service", "deploy", "process"],
        "定时与任务": ["定时", "任务计划", "cron", "调度", "轮询", "schedule", "task", "poller"],
        "网络与证书": ["网络", "隧道", "dns", "域名", "证书", "端口", "代理", "network", "proxy", "port", "cert"],
        "配置与环境": ["配置", "环境", "安装", "依赖", "路径", "config", "install", "env", "setup"],
        "日志与监控": ["日志", "监控", "健康检查", "告警", "log", "monitor", "health", "alert"],
        "数据与备份": ["数据库", "备份", "磁盘", "存储", "database", "backup", "storage", "sqlite"],
    },
    "内容与创作": {
        "图像生成": ["生图", "图像", "图片", "绘画", "海报", "插画", "image", "draw", "illustration"],
        "文本写作": ["写作", "文章", "小说", "故事", "大纲", "剧本", "writing", "story", "outline"],
        "提示词": ["提示词", "prompt", "模板", "指令"],
        "音视频": ["视频", "音频", "剪辑", "配音", "video", "audio"],
        "设计与排版": ["设计", "排版", "视觉", "风格", "design", "layout", "style"],
    },
    "研究与学习": {
        "文献与方法": ["论文", "文献", "方法", "综述", "paper", "method", "survey"],
        "知识整理": ["笔记", "整理", "总结", "复盘", "note", "summary", "review"],
        "学习计划": ["学习", "课程", "计划", "练习", "learn", "course", "plan"],
    },
    "工作与职业": {
        "求职与简历": ["求职", "简历", "面试", "职业", "resume", "interview", "career"],
        "项目与交付": ["项目", "交付", "需求", "排期", "project", "delivery", "requirement"],
        "协作与沟通": ["会议", "汇报", "团队", "客户", "meeting", "team", "client", "report"],
    },
    "生活与个人": {
        "健康与运动": ["健康", "运动", "睡眠", "饮食", "医院", "health", "sport", "diet"],
        "日程与安排": ["日程", "安排", "计划", "提醒", "schedule", "plan", "reminder"],
        "消费与记账": ["购物", "记账", "消费", "预算", "shopping", "budget", "expense"],
        "家庭与社交": ["家庭", "朋友", "孩子", "宠物", "family", "friend", "pet"],
    },
}

DEFAULT_CLASS = "其他"


def _load_lexicon_extension() -> dict[str, list[str]]:
    """可选的用户扩展词表：`v2/class_lexicon.yaml`（不存在就跳过）。

    为什么要这个：默认类目必须**通用**（软件/数据/系统/内容/研究/职业/生活），
    但每个行业有自己的一套高频概念（医疗、法律、设计…）。图书馆的解法是
    「核心分类法 + 各馆本地扩展」，这里同构：
        extends:  给已有类目加词
        classes:  新增本地类目（不想加类目就用 extends）
    格式见同目录 `class_lexicon.example.yaml`。
    """
    import os

    path = os.environ.get("LIBMEM_CLASS_LEXICON") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "class_lexicon.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml  # 项目已依赖 pyyaml
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for cls, words in (data.get("extends") or {}).items():
        if isinstance(words, list):
            out.setdefault(cls, []).extend(str(w) for w in words)
    for cls, words in (data.get("classes") or {}).items():
        if isinstance(words, list):
            out.setdefault(cls, []).extend(str(w) for w in words)
    return out


_LOCAL_LEXICON = _load_lexicon_extension()
for _cls, _words in _LOCAL_LEXICON.items():
    if _cls in CLASS_RULES:
        CLASS_RULES[_cls] = list(dict.fromkeys(CLASS_RULES[_cls] + _words))
    else:
        CLASS_RULES[_cls] = list(dict.fromkeys(_words))


CLASS_ORDER = list(CLASS_RULES) + [DEFAULT_CLASS]


# ---------------------------------------------------------------- 文本归一
def normalize_term(text: str) -> str:
    """实体归一：全半角统一、去两端空白、压空格、小写、去尾部标点。"""
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    t = t.strip("，,。.;；:：!！?？、()（）[]【】\"'“”‘’")
    return t.lower()


def canonical_terms(entities: list, limit: int = 8) -> list[str]:
    """把核心实体转成规范主题词（规则归一，还没有同义词合并 —— 那是 P0 的词表）。"""
    out = []
    for e in entities or []:
        t = normalize_term(e)
        if not t or len(t) < 2:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 分类
def classify(summary: str, task_type: str = "", entities: list | None = None,
             text: str = "") -> tuple[str, float]:
    """规则分类：关键词命中打分，返回 (主类, 置信度 0~1)。"""
    hay_low = normalize_term(f"{summary or ''} {text or ''}")
    ents_low = normalize_term(" ".join(str(e) for e in (entities or [])))
    scores: dict[str, float] = {}
    for cls, words in CLASS_RULES.items():
        score = 0.0
        for w in words:
            wl = w.lower()
            if wl in hay_low:
                score += 1.0
            if wl and wl in ents_low:
                score += 2.0
        if score:
            scores[cls] = score
    if not scores:
        return DEFAULT_CLASS, "", 0.2
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    # 多标签：第二类得分达到主类 40% 就记成「副类」（图书馆的附加分类号）。
    second = ""
    rest = sorted(((c, s) for c, s in scores.items() if c != best), key=lambda kv: -kv[1])
    if rest and rest[0][1] >= scores[best] * 0.4:
        second = rest[0][0]
    return best, second, round(min(0.95, 0.35 + 0.6 * scores[best] / total), 2)


# ---------------------------------------------------------------- 时间标目
def time_parts(source_date: str) -> dict:
    """source_date → {year, month, week, day}；解析失败全 0。"""
    d = None
    if source_date:
        try:
            d = datetime.date.fromisoformat(str(source_date)[:10])
        except ValueError:
            d = None
    if d is None:
        return {"year": 0, "month": 0, "week": 0, "day": 0}
    iso = d.isocalendar()
    return {"year": d.year, "month": d.month, "week": int(iso[1]), "day": d.day}


# ---------------------------------------------------------------- 组装三行
def build_rows(memory: dict) -> tuple[dict, dict, dict]:
    """一条 memory → (asset 行, item 行, description 行)。纯函数，便于测试。"""
    mid = str(memory.get("memory_id") or "").strip()
    content_path = str(memory.get("content_path") or "")
    summary = str(memory.get("summary") or "")
    entities = memory.get("core_entities") or []
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except Exception:
            entities = [entities]

    # 资产：对话按「冷库文件」聚成一件（同一天的对话共享一个 asset）
    if content_path:
        aid = "dialog:" + hashlib.sha256(content_path.encode("utf-8")).hexdigest()[:12]
        uri = content_path
    else:
        aid = "dialog:unknown"
        uri = ""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    asset = {"asset_id": aid, "kind": "dialog", "uri": uri,
             "hash": hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16] if uri else "",
             "created_at": now, "meta_json": "{}"}

    # ★ 2026-10-24：item.created_at 用**记忆自己的创建时间**，不是编目时间 ——
    #   browse 要按真实时间倒序翻目录；用编目时间会让全库挤在同一秒。
    item_created = str(memory.get("created_at") or "") or now
    item = {"item_id": mid, "asset_id": aid, "seq": 0,
            "text": summary, "summary": summary,
            "char_start": None, "char_end": None, "created_at": item_created}

    cls, sub, conf = classify(summary, memory.get("task_type") or "", entities)
    tp = time_parts(memory.get("source_date") or str(memory.get("created_at") or "")[:10])
    desc = {"item_id": mid, "class": cls, "sub_class": "", "alt_class": sub or "",
            "terms": json.dumps(canonical_terms(entities), ensure_ascii=False),
            "year": tp["year"], "month": tp["month"], "week": tp["week"], "day": tp["day"],
            "task_type": memory.get("task_type") or "", "source": "dialog",
            "confidence": conf, "updated_at": now}
    return asset, item, desc


# ---------------------------------------------------------------- 落库
def ensure_tables() -> dict:
    """把 schema.sql 里新加的表建出来（幂等）。"""
    from services.B3_header_engine.sqlite_store import ensure_schema
    return ensure_schema()


def upsert_many(memories: list[dict], conn=None) -> int:
    """批量编目（幂等）。返回写入的 item 数。失败由调用方决定是否吞掉。"""
    if not memories:
        return 0
    from services.B3_header_engine.sqlite_store import get_conn
    own = conn is None
    if own:
        conn = get_conn()
    written = 0
    try:
        for m in memories:
            asset, item, desc = build_rows(m)
            if not item["item_id"]:
                continue
            # asset 只做「有则保留 created_at」，避免每次编目刷掉原始创建时间
            conn.execute(
                "INSERT OR IGNORE INTO assets(asset_id, kind, uri, hash, created_at, meta_json) "
                "VALUES(?,?,?,?,?,?)",
                (asset["asset_id"], asset["kind"], asset["uri"], asset["hash"],
                 asset["created_at"], asset["meta_json"]))
            conn.execute(
                "INSERT OR REPLACE INTO items(item_id, asset_id, seq, text, summary, "
                "char_start, char_end, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (item["item_id"], item["asset_id"], item["seq"], item["text"], item["summary"],
                 item["char_start"], item["char_end"], item["created_at"]))
            # ⚠️ 用 UPSERT 而不是 INSERT OR REPLACE：**重提炼不能把二级类目冲掉**。
            #   二级类目是 bin/build-subclasses.py 事后生成的，重索引时新记录里是空串；
            #   直接 REPLACE 会把已有小类清成空（2026-10-24 实测踩到：重启后
            #   软件与代码的"未细分"从 38 涨回 214）。空值一律保留旧值。
            conn.execute(
                "INSERT INTO descriptions(item_id, class, sub_class, alt_class, terms, "
                "year, month, week, day, task_type, source, confidence, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET "
                "class=excluded.class, alt_class=excluded.alt_class, terms=excluded.terms, "
                "year=excluded.year, month=excluded.month, week=excluded.week, day=excluded.day, "
                "task_type=excluded.task_type, source=excluded.source, "
                "confidence=excluded.confidence, updated_at=excluded.updated_at, "
                "sub_class=CASE WHEN excluded.sub_class IS NULL OR excluded.sub_class = '' "
                "THEN descriptions.sub_class ELSE excluded.sub_class END",
                (desc["item_id"], desc["class"], desc["sub_class"], desc["alt_class"], desc["terms"],
                 desc["year"], desc["month"], desc["week"], desc["day"],
                 desc["task_type"], desc["source"], desc["confidence"], desc["updated_at"]))
            written += 1
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    return written


def backfill(dry_run: bool = True, limit: int | None = None) -> dict:
    """把已有 memories 全量编目。dry_run=True 只统计、不写库。"""
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        conn.row_factory = None
        sql = ("SELECT memory_id, content_path, summary, task_type, core_entities, "
               "source_date, created_at FROM memories")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = list(conn.execute(sql))
    finally:
        conn.close()

    memories = [{"memory_id": r[0], "content_path": r[1], "summary": r[2],
                 "task_type": r[3], "core_entities": r[4], "source_date": r[5],
                 "created_at": r[6]} for r in rows]
    cls_count: dict[str, int] = {}
    for m in memories:
        asset, item, desc = build_rows(m)
        cls_count[desc["class"]] = cls_count.get(desc["class"], 0) + 1

    written = 0
    if not dry_run:
        written = upsert_many(memories)
    return {"total": len(memories), "written": written, "dry_run": dry_run,
            "classes": dict(sorted(cls_count.items(), key=lambda kv: -kv[1]))}



# ---------------------------------------------------------------- 二级类目
#
# 为什么二级类目要"数据驱动"：一级类目是通用骨架（软件/数据/系统/内容…），
# 但「小类才是重点」—— 同一个"软件与代码"里，插件开发、检索索引、发布部署
# 是完全不同的事。而小类**因人而异**：写作者的小类和开发者的不一样。
#
# 所以做法照图书馆的「按本馆需求细分」：类内按**主题词聚集**自动命名，
# 用自己馆藏里的术语当小类名，不预设。换一个人用，小类自动长成他的样子。
#
# 不做三级：三级需要稳定的类目体系才不吵，现在自动生成的二级已经够细；
# 再往下用「主题词浏览」（browse term=）代替，粒度由用户自己定。
def build_subclasses(k_per_class: int = 6, min_items: int = 3,
                     dry_run: bool = True) -> dict:
    """给每个条目定二级类目：通用二级骨架打分优先，未命中再退回数据驱动小类。幂等。"""
    import math

    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        rows = list(conn.execute(
            "SELECT d.item_id, d.class, i.summary, d.terms FROM descriptions d "
            "JOIN items i ON i.item_id = d.item_id"))
        total_items = max(1, len(rows))
        rows_terms = list(conn.execute(
            "SELECT mt.item_id, t.canonical, t.doc_freq FROM mem_terms mt "
            "JOIN terms t ON t.term_id = mt.term_id"))
    finally:
        conn.close()

    class_of = {mid: cls for mid, cls, _s, _t in rows}
    class_size: dict[str, int] = {}
    for cls in class_of.values():
        class_size[cls] = class_size.get(cls, 0) + 1
    cover: dict[str, dict[str, int]] = {}
    for mid, term, _df in rows_terms:
        cls = class_of.get(mid)
        if cls:
            cover.setdefault(cls, {})
            cover[cls][term] = cover[cls].get(term, 0) + 1

    # 每个类里，数据驱动的候选小类（覆盖够、又不过度通用）
    data_subs: dict[str, list[str]] = {}
    for cls, terms in cover.items():
        size = max(1, class_size.get(cls, 1))
        cands = [(term, n) for term, n in terms.items()
                 if n >= min_items and n <= max(min_items, size * 0.5)]
        cands.sort(key=lambda kv: (-kv[1], kv[0]))
        data_subs[cls] = [c[0] for c in cands[:k_per_class]]

    assigned: dict[str, str] = {}
    report: dict[str, dict] = {}
    for mid, cls, summary, terms_json in rows:
        hay = f"{summary or ''}"
        try:
            import json as _json
            termos = [str(x) for x in (_json.loads(terms_json or "[]") or [])]
        except Exception:
            termos = []
        hay_low = normalize_term(hay + " " + " ".join(termos))
        rules = SUB_CLASS_RULES.get(cls, {})
        best_sub, best_score = None, 0.0
        for sub, words in rules.items():
            score = sum(2.0 if w.lower() in " ".join(termos).lower() else
                        (1.0 if w.lower() in hay_low else 0.0) for w in words)
            if score > best_score:
                best_sub, best_score = sub, score
        if best_sub is None:
            # 没有骨架命中 → 用数据驱动小类：命中候选词最多者
            fallback = None
            for term in data_subs.get(cls, []):
                if term in termos:
                    fallback = term
                    break
            best_sub = fallback or "未细分"
        assigned[mid] = best_sub
        report.setdefault(cls, {}).setdefault(best_sub, 0)
        report[cls][best_sub] += 1

    if not dry_run:
        conn = get_conn()
        try:
            conn.executemany("UPDATE descriptions SET sub_class = ? WHERE item_id = ?",
                             [(sub, mid) for mid, sub in assigned.items()])
            conn.commit()
        finally:
            conn.close()
    detail = {cls: {"size": class_size.get(cls, 0), "subclasses": subs}
              for cls, subs in report.items()}
    return {"classes": len(detail), "items": len(assigned), "dry_run": dry_run,
            "detail": detail}

def stats() -> dict:
    """当前编目覆盖情况。"""
    from services.B3_header_engine.sqlite_store import get_conn
    conn = get_conn()
    try:
        def one(sql, *args):
            return conn.execute(sql, args).fetchone()[0]
        total_items = one("SELECT count(*) FROM items")
        total_desc = one("SELECT count(*) FROM descriptions")
        total_mem = one("SELECT count(*) FROM memories")
        cls = list(conn.execute(
            "SELECT class, count(*) FROM descriptions GROUP BY class ORDER BY count(*) DESC"))
        years = list(conn.execute(
            "SELECT year, count(*) FROM descriptions GROUP BY year ORDER BY year"))
    finally:
        conn.close()
    return {"memories": total_mem, "items": total_items, "descriptions": total_desc,
            "coverage": round(total_desc / total_mem, 3) if total_mem else 0,
            "classes": {k: v for k, v in cls}, "years": {str(y): c for y, c in years}}


# ---------------------------------------------------------------- P0：标目检索
#
# 这是"第五路召回"：query 先查受控词表（规范词 + 别名），再走 mem_terms 倒排。
# 与四路的关系是 RRF 等权融合；它的价值在**词形归一**（同义/别名/大小写）
# 和**绕过 Phase1 截断**（倒排命中集天然小，不依赖全表扫描）。
_CACHE = {"key": None, "terms": None}


def _vocab_cached(conn):
    """按写纪元缓存词表（2383 条，每查询重建太浪费）。"""
    from services.B3_header_engine.sqlite_store import write_epoch
    key = write_epoch()
    if _CACHE["key"] == key and _CACHE["terms"] is not None:
        return _CACHE["terms"]
    rows = conn.execute(
        "SELECT term_id, canonical, aliases_json, doc_freq FROM terms").fetchall()
    entries = []
    for tid, canon, aliases, df in rows:
        try:
            al = json.loads(aliases or "[]")
        except Exception:
            al = []
        entries.append((tid, normalize_term(canon), [normalize_term(x) for x in al if x], df or 0))
    _CACHE.update({"key": key, "terms": entries})
    return entries


def catalog_search(query_text: str, k: int = 60) -> list[str]:
    """【未接入检索 · 保留待用】标目检索（第五路召回）。

    2026-10-24 实测：作为第五路 RRF 会稀释另外四路（真实库 recall 0.8205 → 0.7949；
    LoCoMo 0.66 → 0.66/MRR 降），已从融合里撤掉，本函数暂时没有调用方。
    保留原因：等库规模上去、或分类/词表质量提升后，用它做「主题检索」可能重新划算。

    标目检索：返回按「命中规范词的 idf 之和」降序的 item_id 列表。

    失败返回空 list（调用方按"这一路没命中"处理，不影响其他四路）。
    """
    import math

    from services.B3_header_engine.sqlite_store import get_conn
    if not query_text or not query_text.strip():
        return []
    conn = get_conn()
    try:
        entries = _vocab_cached(conn)
        q = normalize_term(query_text)
        matched = []
        for tid, canon, aliases, df in entries:
            if canon and canon in q:
                matched.append((tid, df))
            elif aliases and any(a in q for a in aliases):
                matched.append((tid, df))
        if not matched:
            return []
        total = conn.execute("SELECT count(*) FROM mem_terms").fetchone()[0] or 1
        scores: dict[str, float] = {}
        for tid, df in matched:
            idf = math.log(1 + total / max(1, df))
            for (mid,) in conn.execute("SELECT item_id FROM mem_terms WHERE term_id=?", (tid,)):
                scores[mid] = scores.get(mid, 0.0) + idf
        return [mid for mid, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:k]]
    finally:
        conn.close()


def query_bonus_map(query_text: str, bonus: float = 0.03) -> dict[str, float]:
    """【未接入检索 · 保留待用】标目加分。

    2026-10-24 实测：两套数据都无收益（真实库已知条目 recall 0.8205→0.7949；
    LoCoMo 0.66→0.66，MRR 0.414→0.392），`retrieval.catalog_bonus` 默认 0。
    保留原因：加分形态本身是对的（不稀释融合），等分类质量提升后再评估。

    ★ P0' A3：命中「专指标目词」的条目 → 返回 {item_id: bonus}（加分，不改融合结构）。

    为什么改成加分而不是第五路：实测第五路会稀释四路（真实查询 recall −2.6pp），
    而加分只抬高命中条目、不改变其他条目的相对名次，风险低得多。
    数据源是 `mem_terms`（已在建表时剔除泛词，见 build-vocab 的 df 过滤）。
    """
    import math

    from services.B3_header_engine.sqlite_store import get_conn
    if bonus <= 0 or not query_text or not query_text.strip():
        return {}
    conn = get_conn()
    try:
        entries = _vocab_cached(conn)
        q = normalize_term(query_text)
        matched = []
        for tid, canon, aliases, df in entries:
            if canon and canon in q:
                matched.append((tid, df))
            elif aliases and any(a in q for a in aliases):
                matched.append((tid, df))
        if not matched:
            return {}
        total = conn.execute("SELECT count(*) FROM mem_terms").fetchone()[0] or 1
        scores: dict[str, float] = {}
        for tid, df in matched:
            idf = math.log(1 + total / max(1, df))
            for (mid,) in conn.execute("SELECT item_id FROM mem_terms WHERE term_id=?", (tid,)):
                scores[mid] = scores.get(mid, 0.0) + idf
        if not scores:
            return {}
        top = max(scores.values())
        return {mid: round(bonus * v / top, 4) for mid, v in scores.items()}
    finally:
        conn.close()


# ---------------------------------------------------------------- L2 聚合清单
# 2026-11-01：目录五功能里的「聚合」（卡特 collocation）。
# 与上面两路（catalog_search / query_bonus_map）的区别：
#   那两个是**排序增强**（找最像的），这一个是**完整性查询**（给全集）。
# 导入放在文件末尾：collocate.py 内部要用 normalize_term，避免循环导入。
from .collocate import collocate  # noqa: E402

# 2026-11-01（L1）：引文边 —— 四法里的「引文法」。
from .citations import (  # noqa: E402
    ensure_table as citations_ensure,
    link_many as citations_link_many,
    link_one as citations_link_one,
    neighbors as citations_neighbors,
    edges_for_many as citations_edges_for_many,
    stats as citations_stats,
    backfill as citations_backfill,
)

# 2026-11-01（L3）：著录导出 —— L2 的集合 × L1 的结构，合成一份可读清单。
from .catalog import catalog_export  # noqa: E402

# 2026-11-01（L4 临时版）：参见边 —— Svenonius 的导航功能（相关/同义）。
from .see_also import (  # noqa: E402
    build as see_also_build,
    see_also as see_also_lookup,
    stats as see_also_stats,
    ensure_table as see_also_ensure,
)
