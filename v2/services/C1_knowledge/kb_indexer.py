"""C1 知识库索引（架构书 §5.10 知识库索引与文件工具）。

职责
----
| 能力 | 入口 |
|---|---|
| 扫描知识库目录 → 建 Whoosh 全文索引 | `reindex_knowledge()` / `scan_knowledge_base()` |
| 全文检索（jieba 分词 + Multifield） | `search_knowledge_index()` |
| 读文件正文（含 docx/pdf） | `read_any_file()` / `read_kb_file_content()` |
| 目录/文件清单 | `list_kb_files()` / `list_kb_tree()` |
| 知识库现状（**空索引要说得出来**） | `knowledge_stats()` |
| §5.10 步骤 3 双向关联（知识 → 记忆） | `related_memories_for()` |

2026-09-23 修的三处「静默」（C1 补课）
------------------------------------
1. **PDF 读不了却装作没有**：原来 `.pdf` 走 `PyPDF2`，而**这台机器没装 PyPDF2**，
   失败后被 `except: continue` 吞掉 —— 知识库里放 PDF 等于没放。
   现在改用 **pymupdf**（已装、无额外依赖；PyPDF2 只作老环境兜底），
   并且**不支持的扩展名会显式报出来**（`read_any_file` 抛错 → 进报告）。
2. **扫描结果只有个数**：`scan_knowledge_base()` 只返回 `count`，
   跳过了什么、为什么跳过全看不见。现在 `reindex_knowledge()` 返回完整报告
   （每个跳过项带原因），`scan_knowledge_base()` 保留为兼容壳。
3. **空索引 = 静默返回 `[]`**：`knowledge_search` 的返回是裸 list，
   「没有匹配」和「索引根本没建/目录是空的」长得一模一样 ——
   这正是「上传了大纲却搜不到」被瞒了很久的原因。现在：
   * 索引为空时**往 stderr 打明确告警**；
   * 新增 `knowledge_stats()` 让这个状态可查询。

另外：正文截断上限从 `[:10000]` 提到可配（默认 200k），并在报告里标 `truncated` ——
截断意味着「文档后半截搜不到」，这属于必须让人知道的信息。
"""
import os, re, shutil, time

import jieba
from whoosh.index import create_in, open_dir
from whoosh.fields import Schema, TEXT, ID, NUMERIC
from whoosh.qparser import MultifieldParser, OrGroup
from whoosh.analysis import Token, Tokenizer, LowercaseFilter

INDEX_NAME = "knowledge_index"

# 直接按文本读的扩展名
TEXT_EXTS = (".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log",
             ".py", ".js", ".ts", ".tsx", ".html", ".css")

# 需要解析器才能读的扩展名
DOC_EXTS = (".docx", ".pdf")

# 单篇正文进索引的字符上限（超出即截断，并在报告里标出来）
DEFAULT_MAX_CHARS = 200_000


class JiebaTokenizer(Tokenizer):
    def __call__(self, value, positions=False, start_pos=0, **kwargs):
        if not isinstance(value, str):
            return
        words = jieba.lcut(value)
        pos = start_pos
        for word in words:
            word = word.strip()
            if not word:
                continue
            t = Token(positions=True, **kwargs)
            t.text = word
            t.boost = 1.0
            t.pos = pos
            pos += 1
            yield t


JIEBA_ANALYZER = JiebaTokenizer() | LowercaseFilter()

SCHEMA = Schema(
    # ★ 2026-11-03：文档级 → **块级**索引。
    #   原因（实测）：一份 67KB 的章节被当成一篇文档，搜「受控词表」时命中的预览
    #   永远是章节开头那几句 —— 能告诉你"在这本书里"，但送不到具体那一段。
    #   图书馆的说法：目录卡告诉你"在哪个馆藏"，索引款目才能告诉你"在哪一页"。
    path=ID(stored=True, unique=True),      # 唯一 id：原文件路径#块号
    doc_path=ID(stored=True),               # 原文件路径（knowledge_read 要用它）
    chunk=NUMERIC(stored=True),             # 块号（从 0 起）
    name=TEXT(stored=True, analyzer=JIEBA_ANALYZER),
    content=TEXT(analyzer=JIEBA_ANALYZER, stored=True),
    size_kb=NUMERIC(stored=True),
)

# 分块参数：目标 1500 字，最多 2500 字（中文按字符算，1500 字≈一段完整论述）
CHUNK_TARGET = 1500
CHUNK_MAX = 2500
_CHUNK_HEAD = re.compile(
    r"^(?:#{1,6}\s+|第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+(?:\.\d+)+\s)")


def chunk_text(content: str, target: int = CHUNK_TARGET, max_len: int = CHUNK_MAX) -> list:
    """把长文切成**按小节对齐**的块。

    为什么要按小节切而不是定长切：定长会把一个定义从中间劈开，
    而"受控语言的规范化包括三方面"这种句子一旦被劈开，后半句就搜不到了。
    优先在小节标题处断开；只有一节本身超长时才按 max_len 硬切。
    """
    lines = (content or "").splitlines()
    chunks, buf, size = [], [], 0
    for ln in lines:
        s = ln.strip()
        if buf and size >= target * 0.5 and _CHUNK_HEAD.match(s):
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
        if size >= max_len:
            chunks.append("\n".join(buf))
            buf, size = [], 0
    if buf:
        chunks.append("\n".join(buf))
    # 太短的尾巴并进上一块，避免产生一堆没信息的碎块
    out = []
    for c in chunks:
        if out and len(c.strip()) < 200:
            out[-1] = out[-1] + "\n" + c
        elif c.strip():
            out.append(c)
    return out or [content or ""]


def _get_index_dir():
    from services.shared.config.loader import get_data_path
    path = get_data_path("index_dir", "./data/index")
    os.makedirs(path, exist_ok=True)
    return path


def _get_kb_dir():
    from services.shared.config.loader import get_data_path
    path = get_data_path("knowledge_dir", "./data/knowledge")
    os.makedirs(path, exist_ok=True)
    return path


def _index_dir_path() -> str:
    return os.path.join(_get_index_dir(), INDEX_NAME)


_CURRENT_INDEX = None


def _get_or_create_index():
    global _CURRENT_INDEX
    if _CURRENT_INDEX:
        return _CURRENT_INDEX
    kb_idx = _index_dir_path()
    if not os.path.exists(kb_idx):
        os.makedirs(kb_idx, exist_ok=True)
        _CURRENT_INDEX = create_in(kb_idx, SCHEMA)
    else:
        try:
            _CURRENT_INDEX = open_dir(kb_idx)
        except Exception:
            shutil.rmtree(kb_idx)
            os.makedirs(kb_idx, exist_ok=True)
            _CURRENT_INDEX = create_in(kb_idx, SCHEMA)
    return _CURRENT_INDEX


# ---------------------------------------------------------------- 读文件

def read_any_file(path: str) -> str:
    """把知识库文件读成纯文本。

    **读不了要抛错，不许静默跳过** —— 原先的 `except: continue` 让
    「PDF 缺 PyPDF2」这种问题完全隐形（知识库里放 PDF = 没放）。
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in TEXT_EXTS:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    if ext == ".docx":
        # docx2txt 优先（已装）；缺失时退回 pymupdf（1.23+ 直接支持 docx）
        try:
            import docx2txt
            return docx2txt.process(path) or ""
        except ImportError:
            pass
        import pymupdf
        with pymupdf.open(path) as doc:
            return "".join(page.get_text() for page in doc)

    if ext == ".pdf":
        # ⚠️ 不能用 PyPDF2 当主路：本机没装，而失败会被吞掉（2026-09-23 实测）
        try:
            import pymupdf
            with pymupdf.open(path) as doc:
                return "".join(page.get_text() for page in doc)
        except ImportError:
            import PyPDF2  # 老环境兜底
            reader = PyPDF2.PdfReader(path)
            return "\n".join(p.extract_text() or "" for p in reader.pages)

    raise ValueError(f"不支持的扩展名 {ext or '(无)'}（文本类 {TEXT_EXTS} / 文档类 {DOC_EXTS}）")


def read_kb_file_content(path: str, max_chars: int = 3000) -> str:
    """读文件正文（MCP `knowledge_read` 用）。读不了返回空串，但**会往 stderr 说明原因**。"""
    try:
        if not os.path.exists(path):
            print(f"  [KB] 文件不存在: {path}")
            return ""
        content = read_any_file(path)
        if len(content) > max_chars:
            return content[:max_chars] + "\n...（已截断）"
        return content
    except Exception as error:  # noqa: BLE001
        print(f"  [KB] 读取失败 {os.path.basename(path)}: {type(error).__name__}: {error}")
        return ""


# ---------------------------------------------------------------- 建索引

def reindex_knowledge(max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """全量重建知识库索引，返回**可解释的报告**（不是只给一个数字）。

    Returns: {
        kb_dir, index_dir, files, indexed, skipped = [{name, path, reason}],
        truncated = [name...], docs_in_index, elapsed_ms, readers
    }
    """
    global _CURRENT_INDEX
    t0 = time.time()
    kb_dir = _get_kb_dir()
    kb_idx = _index_dir_path()

    # Whoosh 的写锁残留会让重建卡住：先清掉（原先也有这一步，保留）
    lock_path = os.path.join(kb_idx, "MAIN_WRITELOCK")
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except OSError:
            pass
    if os.path.exists(kb_idx):
        try:
            shutil.rmtree(kb_idx)
        except OSError:
            pass
    time.sleep(0.1)
    os.makedirs(kb_idx, exist_ok=True)
    _CURRENT_INDEX = create_in(kb_idx, SCHEMA)

    writer = _CURRENT_INDEX.writer()
    indexed, skipped, truncated = 0, [], []
    chunks_total = 0
    files = 0
    for root, _dirs, names in os.walk(kb_dir):
        for fname in sorted(names):
            fp = os.path.join(root, fname)
            files += 1
            try:
                content = read_any_file(fp)
            except Exception as error:  # noqa: BLE001 - 读不了的必须留痕
                skipped.append({"name": fname, "path": fp,
                                "reason": f"{type(error).__name__}: {error}"})
                continue
            if not content.strip():
                skipped.append({"name": fname, "path": fp, "reason": "读出来是空的"})
                continue
            if len(content) > max_chars:
                truncated.append(fname)
                content = content[:max_chars]
            # ★ 2026-11-03：按小节切块，一块一篇文档（块级索引）
            parts = chunk_text(content)
            for ci, part in enumerate(parts):
                writer.update_document(
                    path=f"{fp}#{ci}",
                    doc_path=fp,
                    chunk=ci,
                    name=fname,
                    content=part,
                    size_kb=round(len(part.encode("utf-8")) / 1024, 1),
                )
            chunks_total += len(parts)
            indexed += 1
    writer.commit()

    # ★ 2026-11-03：**入库即编目**。用户的原话：「进来的东西按道理就要做一个编目」。
    #   只建正文索引、不建款目，就会出现「记得大概名字却搜不到」
    #   （`《万情补天录》.docx` 文件名里没有"大纲"，靠正文永远搜不出来）。
    #   失败不拖垮索引：款目是旁路，最多是这一次没编上。
    try:
        from services.C1_knowledge.catalog import build as _catalog_build
        catalog_stats = _catalog_build(dry_run=False)
    except Exception as error:  # noqa: BLE001
        catalog_stats = f"failed: {type(error).__name__}: {error}"
        print(f"  [KB] 编目失败（不影响正文索引）: {error}")

    # ★ 2026-11-03：规则编不出来的（体裁=未分类/主题词为空），交给本机小模型补。
    #   **限量**（默认 8 份/次）：入库不能因为要等模型而卡住；
    #   积压的存量用 `bin/catalog-llm.py --all` 一次补完。
    #   关掉：config `knowledge.llm_catalog: false`。
    llm_stats = None
    try:
        from services.shared.config import loader
        want = ((loader.get_config() or {}).get("knowledge", {}) or {}).get("llm_catalog", True)
        if want:
            from services.C1_knowledge.llm_catalog import catalog_missing, enrich
            todo = catalog_missing(limit=8)
            if todo:
                llm_stats = enrich(todo)
    except Exception as error:  # noqa: BLE001 - 模型不可用不影响入库
        llm_stats = f"skipped: {type(error).__name__}: {error}"

    report = {
        "kb_dir": kb_dir,
        "index_dir": kb_idx,
        "files": files,
        "indexed": indexed,
        "chunks": chunks_total,          # 实际进索引的块数（≥ indexed）
        "catalogued": catalog_stats,     # 入库即编目（款目数）
        "llm_catalog": llm_stats,        # 本轮用小模型补的编目
        "skipped": skipped,
        "truncated": truncated,
        "docs_in_index": docs_in_index(),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "readers": reader_availability(),
    }
    if indexed == 0:
        print(f"  [KB] ⚠️ 索引里现在 0 篇文档（目录 {kb_dir} 下 {files} 个文件）"
              f" —— 知识库搜索会一律返回空，别把它当成「没有匹配」")
    return report


def scan_knowledge_base() -> int:
    """兼容壳：老的 FastAPI 路由 `/knowledge/scan` 要的是「索引了多少个」。

    要完整报告（跳过了谁、为什么）请用 `reindex_knowledge()`。
    """
    return reindex_knowledge()["indexed"]


# ---------------------------------------------------------------- 查索引

def docs_in_index() -> int:
    """索引里现在有多少篇文档（用来区分「没有匹配」和「索引是空的」）。"""
    try:
        idx = _get_or_create_index()
        return idx.doc_count()
    except Exception as error:  # noqa: BLE001
        print(f"  [KB] 读索引文档数失败: {type(error).__name__}: {error}")
        return -1


def reader_availability() -> dict:
    """各解析器装没装 —— 这决定了哪类文件真的能被索引。"""
    out = {}
    for mod in ("docx2txt", "pymupdf", "PyPDF2"):
        try:
            __import__(mod)
            out[mod] = True
        except Exception:  # noqa: BLE001
            out[mod] = False
    return out


def knowledge_stats() -> dict:
    """知识库现状。存在的理由：`knowledge_search` 返回裸 list，
    没法表达「索引是空的」—— 于是「上传了大纲却搜不到」这种状态被瞒了很久。
    """
    kb_dir = _get_kb_dir()
    kb_idx = _index_dir_path()
    files, exts = 0, {}
    for root, _dirs, names in os.walk(kb_dir):
        for fname in names:
            files += 1
            ext = os.path.splitext(fname)[1].lower() or "(无)"
            exts[ext] = exts.get(ext, 0) + 1
    return {
        "kb_dir": kb_dir,
        "index_dir": kb_idx,
        "index_built": os.path.exists(os.path.join(kb_idx, "_MAIN_1.toc")),
        "files_on_disk": files,
        "by_ext": exts,
        "docs_in_index": docs_in_index(),
        "readers": reader_availability(),
        "hint": ("files_on_disk>0 而 docs_in_index==0 → 从没重建过索引，"
                 "调 knowledge_reindex（MCP 工具）或跑 bin\\import-knowledge.py --apply"),
    }


def search_knowledge_index(query: str, top_k: int = 10,
                           with_related: bool = False) -> list:
    """全文检索。

    Args:
        with_related: 顺带给每条命中附 `related_memories`（§5.10 步骤 3 双向关联，
            知识条目 → 记忆条目）。**只读不升温**：走 `retrieve(apply_heat=False)`，
            免得「为了做关联」把记忆加热、污染 heat 语义。
    """
    try:
        idx = _get_or_create_index()
        total = idx.doc_count()
        if total == 0:
            print(f"  [KB] ⚠️ 索引为空（0 篇文档），`{query[:40]}` 必然搜不到 —— "
                  f"这不是「没有匹配」，请先 knowledge_reindex")
        with idx.searcher() as searcher:
            qp = MultifieldParser(
                ["content", "name"],
                idx.schema,
                group=OrGroup,
                fieldboosts={"content": 1.0, "name": 2.0},
            )
            try:
                q = qp.parse(query)
            except Exception:  # noqa: BLE001 - 语法错误就退化成全量
                q = qp.parse("*")
            results = searcher.search(q, limit=top_k)
            hits = [{
                "name": r["name"],
                # path 给的是**原文件路径**（knowledge_read 要拿它读全文），
                # 块号另给 —— 否则调用方会拿着 "xxx#3" 去 open 一个不存在的文件
                "path": r["doc_path"],
                "chunk": r.get("chunk"),
                "size_kb": r["size_kb"],
                "content_preview": r["content"][:200],
                "_score": round(r.score, 2),
                "matched_by": "content",
            } for r in results]

        # ★ 2026-11-03：**先查款目**（题名/体裁/主题词/项目），再查正文。
        #   用户的原话：「人的记忆不精确，往往只记得大概名字」——
        #   `《万情补天录》.docx` 文件名里没有"大纲"二字，靠正文检索永远搜不到，
        #   但它编目后 `genre=大纲`，一查款目就出来。
        try:
            from services.C1_knowledge.catalog import lookup as _cat_lookup
            seen = {h["path"] for h in hits}
            cat = [c for c in _cat_lookup(query, top_k=max(3, top_k // 2))
                   if c["path"] not in seen]
            # ⚠️ 2026-11-03 修：第一版在循环里 `hits.insert(0, …)`，
            #   于是**分数最高的那条被挤到最后**（插入顺序反了）。
            #   现在先攒成列表、再整体拼到前面，顺序就是 `lookup` 给的分数序。
            cat_hits = []
            for c in cat:
                cat_hits.append({
                    "name": os.path.basename(c["path"]),
                    "path": c["path"],
                    "chunk": None,
                    "size_kb": None,
                    "content_preview": "【著录】%s ｜ 体裁：%s ｜ 项目：%s ｜ 主题词：%s"
                                        % (c["title"], c["genre"], c["project"],
                                           "、".join(c["subjects"][:5])),
                    "_score": c["catalog_score"],
                    "matched_by": "catalog",
                })
            hits = cat_hits + hits
        except Exception as error:  # noqa: BLE001 - 款目查不到不影响正文检索
            print(f"  [KB] 款目检索跳过: {type(error).__name__}: {error}")
        if with_related:
            for hit in hits[:3]:          # 只给前 3 条做关联，避免多次嵌入
                hit["related_memories"] = related_memories_for(hit["name"], top_k=3)
        return hits
    except Exception as error:  # noqa: BLE001 - 不让知识库故障炸掉调用方，但要留痕
        print(f"  [KB] 检索失败: {type(error).__name__}: {error}")
        return []


def related_memories_for(text: str, top_k: int = 3,
                         min_sim: float = 0.45) -> list:
    """§5.10 步骤 3「双向关联」的知识 → 记忆方向。

    走 B4 检索（向量精排），但 **`apply_heat=False`**：这是旁路关联查询，
    不是「用户想起了这条记忆」，绝不能升温。

    `min_sim` 是 2026-09-23 加的门槛：B4 自己的 `_MIN_SIM_THRESHOLD=0.15` 是为了
    「别把明显不相关的塞进检索结果」，可用作**关联**太松了 —— 实测把《半盏药》大纲
    和「install.bat 集成冷记忆监听器」那条记忆关联到了一起（sim 0.373）。
    关联列表的价值全在「真的相关」，噪声宁可不要。
    """
    try:
        from services.B4_dual_gate_retrieval import retrieve
        result = retrieve(query_text=text, max_items=top_k, apply_heat=False)
        out = []
        for it in result.get("items", []):
            sim = float(it.get("_sim") or 0.0)
            if sim < min_sim:
                continue
            out.append({
                "memory_id": it.get("memory_id"),
                "summary": (it.get("summary") or "")[:120],
                "sim": round(sim, 3),
                "layer": it.get("layer"),
            })
        return out
    except Exception as error:  # noqa: BLE001 - 关联失败不影响检索本身
        print(f"  [KB] related_memories 失败: {type(error).__name__}: {error}")
        return []


# ---------------------------------------------------------------- 清单

def list_kb_files() -> list:
    kb_dir = _get_kb_dir()
    files = []
    if os.path.exists(kb_dir):
        for fname in sorted(os.listdir(kb_dir)):
            fp = os.path.join(kb_dir, fname)
            if os.path.isfile(fp):
                try:
                    stat = os.stat(fp)
                    files.append({
                        "name": fname,
                        "path": fp,
                        "folder": "",
                        "size_kb": round(stat.st_size / 1024, 1),
                        "modified": stat.st_mtime,
                    })
                except OSError:
                    pass
    return files


def list_kb_tree() -> list:
    kb_dir = _get_kb_dir()
    tree = []
    if not os.path.exists(kb_dir):
        return tree
    seen_folders = set()
    for root, dirs, files in os.walk(kb_dir):
        rel = os.path.relpath(root, kb_dir)
        parent = "" if rel == "." else rel.replace("\\", "/")
        if parent and parent not in seen_folders:
            folder_name = parent.split("/")[-1]
            folder_parent = "/".join(parent.split("/")[:-1]) if "/" in parent else ""
            tree.append({
                "name": folder_name, "path": root, "folder": folder_parent,
                "is_folder": True, "size_kb": 0, "modified": 0,
            })
            seen_folders.add(parent)
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                stat = os.stat(fp)
                tree.append({
                    "name": f, "path": fp, "folder": parent,
                    "is_folder": False,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": stat.st_mtime,
                })
            except OSError:
                pass
    for root, dirs, files in os.walk(kb_dir):
        rel = os.path.relpath(root, kb_dir)
        parent = "" if rel == "." else rel.replace("\\", "/")
        if parent and not files and parent not in seen_folders:
            folder_name = parent.split("/")[-1]
            folder_parent = "/".join(parent.split("/")[:-1]) if "/" in parent else ""
            tree.append({
                "name": folder_name, "path": root, "folder": folder_parent,
                "is_folder": True, "size_kb": 0, "modified": 0,
            })
            seen_folders.add(parent)
    return tree
