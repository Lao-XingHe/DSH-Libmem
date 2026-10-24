import os, json, shutil, time
import jieba
from whoosh.index import create_in, open_dir, EmptyIndexError
from whoosh.fields import Schema, TEXT, ID, NUMERIC, STORED
from whoosh.qparser import MultifieldParser, OrGroup
from whoosh.analysis import Token, Tokenizer, LowercaseFilter

INDEX_NAME = "memory_index"

def _get_index_dir():
    from services.shared.config.loader import get_data_path
    path = get_data_path("index_dir", "./data/index")
    os.makedirs(path, exist_ok=True)
    return path

def _get_warm_dir():
    from services.shared.config.loader import get_data_path
    path = get_data_path("warm_dir", "./data/warm")
    os.makedirs(path, exist_ok=True)
    return path


class JiebaTokenizer(Tokenizer):
    """基于 jieba 的中文分词 Tokenizer"""
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


# 中文分词用纯 jieba tokenizer + LowercaseFilter
JIEBA_ANALYZER = JiebaTokenizer() | LowercaseFilter()

SCHEMA = Schema(
    id=ID(stored=True, unique=True),
    type=TEXT(stored=True, analyzer=JIEBA_ANALYZER),
    summary=TEXT(stored=True, analyzer=JIEBA_ANALYZER, field_boost=2.0),
    topics=TEXT(stored=True, analyzer=JIEBA_ANALYZER, field_boost=2.5),
    entities=TEXT(stored=True, analyzer=JIEBA_ANALYZER, field_boost=3.0),
    relations=TEXT(stored=True, analyzer=JIEBA_ANALYZER, field_boost=1.5),
    intent=TEXT(stored=True, field_boost=1.5),
    temporal=TEXT(stored=True),
    ref=ID(stored=True),
    callCount=NUMERIC(stored=True),
    source_date=ID(stored=True),
    score=NUMERIC(stored=True),
)

_CURRENT_INDEX = None

def get_index_info() -> dict:
    idx_dir = _get_index_dir()
    idx_path = os.path.join(idx_dir, INDEX_NAME)
    info = {"exists": os.path.exists(idx_path), "doc_count": 0}
    if os.path.exists(idx_path):
        try:
            idx = _get_or_create_index()
            with idx.searcher() as s:
                info["doc_count"] = s.doc_count()
        except Exception as e:
            info["error"] = str(e)[:200]
    return info

def _get_or_create_index():
    global _CURRENT_INDEX
    if _CURRENT_INDEX: return _CURRENT_INDEX
    idx_dir = _get_index_dir(); idx_path = os.path.join(idx_dir, INDEX_NAME)
    if not os.path.exists(idx_path): os.makedirs(idx_path, exist_ok=True); _CURRENT_INDEX = create_in(idx_path, SCHEMA)
    else:
        try:
            _CURRENT_INDEX = open_dir(idx_path)
        except Exception:
            shutil.rmtree(idx_path)
            os.makedirs(idx_path, exist_ok=True)
            _CURRENT_INDEX = create_in(idx_path, SCHEMA)
    return _CURRENT_INDEX

def reload_index():
    global _CURRENT_INDEX
    _CURRENT_INDEX = None
    idx_dir = _get_index_dir()
    idx_path = os.path.join(idx_dir, INDEX_NAME)
    warm_dir = _get_warm_dir()
    lock_path = os.path.join(idx_path, "MAIN_WRITELOCK")
    if os.path.exists(lock_path):
        try: os.remove(lock_path)
        except: pass
    if os.path.exists(idx_path):
        try:
            shutil.rmtree(idx_path)
        except: pass
    time.sleep(0.1)
    os.makedirs(idx_path, exist_ok=True)
    _CURRENT_INDEX = create_in(idx_path, SCHEMA)
    idx = _CURRENT_INDEX
    writer = idx.writer()
    doc_count = 0
    if os.path.exists(warm_dir):
        for fname in sorted(os.listdir(warm_dir)):
            if fname.endswith(".jsonl"):
                date_str = fname.replace(".jsonl", "")
                fp = os.path.join(warm_dir, fname)
                print(f"  [索引]  读取 {fname}...")
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        file_count = 0
                        for line in f:
                            try:
                                entry = json.loads(line.strip())
                                if isinstance(entry, dict) and entry.get("id"):
                                    writer.update_document(
                                        id=entry.get("id", ""),
                                        type=entry.get("type", ""),
                                        summary=entry.get("summary", ""),
                                        topics=" ".join(entry.get("topics", [])),
                                        entities=" ".join(entry.get("entities", [])),
                                        relations=" ".join(entry.get("relations", [])),
                                        intent=" ".join(entry.get("intent", [])),
                                        temporal=" ".join(entry.get("temporal", [])),
                                        ref=entry.get("ref", ""),
                                        callCount=entry.get("callCount", 0),
                                        source_date=entry.get("source_date", date_str),
                                        score=0,
                                    )
                                    file_count += 1
                            except Exception as e:
                                print(f"  [索引]    跳过行: {e}")
                        print(f"  [索引]   {fname}: 写入 {file_count} 条")
                        doc_count += file_count
                except Exception as e:
                    print(f"  [索引]  打开 {fname} 失败: {e}")
    else:
        print(f"  [索引]  温记忆目录不存在: {warm_dir}")
    writer.commit()
    _CURRENT_INDEX = idx
    return doc_count

def search_index(query: str, top_k: int = 10) -> list:
    try:
        idx = _get_or_create_index()
        with idx.searcher() as searcher:
            qp = MultifieldParser(
                ["entities", "topics", "summary", "relations", "intent", "type"],
                idx.schema,
                group=OrGroup,
                fieldboosts={
                    "entities": 3.0,
                    "topics": 2.5,
                    "summary": 2.0,
                    "relations": 1.5,
                    "intent": 1.5,
                    "type": 1.0,
                }
            )
            try: q = qp.parse(query)
            except: q = qp.parse("*")
            results = searcher.search(q, limit=top_k)
            if not results:
                words = query.split()
                if len(words) > 1:
                    qp2 = MultifieldParser(
                        ["entities", "topics", "summary"],
                        idx.schema,
                        group=OrGroup,
                        fieldboosts={"entities": 3.0, "topics": 2.5, "summary": 2.0},
                    )
                    try: q = qp2.parse(words[0])
                    except: q = qp2.parse("*")
                    results = searcher.search(q, limit=top_k)
            return [{
                "id": r["id"],
                "type": r["type"],
                "summary": r["summary"],
                "topics": [t for t in r.get("topics", "").split() if t],
                "entities": [t for t in r.get("entities", "").split() if t],
                "relations": [t for t in r.get("relations", "").split() if t],
                "intent": [t for t in r.get("intent", "").split() if t],
                "temporal": [t for t in r.get("temporal", "").split() if t],
                "ref": r["ref"],
                "callCount": r["callCount"],
                "source_date": r.get("source_date", ""),
                "score": round(r.score, 2),
            } for r in results]
    except EmptyIndexError: return []
    except: return []

def ensure_index_ready():
    global _CURRENT_INDEX
    _CURRENT_INDEX = None
    n = reload_index()
    print(f"  [索引] 就绪，共 {n} 条文档")
