import os, yaml

TASK_CONFIG = {
    "keyword_extract": {"num_ctx": 4096, "num_predict": 100},
    "warm_extract": {"num_ctx": 4096, "num_predict": 800},
    "semantic_match": {"num_ctx": 8192, "num_predict": 200},
    "relevance_filter": {"num_ctx": 4096, "num_predict": 600},
    "persona_generate": {"num_ctx": 4096, "num_predict": 600},
    "long_doc_chunk": {"num_ctx": 32768, "num_predict": 500},
    "chat": {"num_ctx": 131072, "num_predict": 4096},
    "default": {"num_ctx": 4096, "num_predict": 200},
}

def _load_config():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "config.yaml")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

def get_task_options(task_type: str = "default") -> dict:
    return dict(TASK_CONFIG.get(task_type, TASK_CONFIG["default"]))

def get_preferred_model() -> str:
    cfg = _load_config()
    small = cfg.get("llm", {}).get("small", {})
    return small.get("model", "qwen2.5:3b")

def get_fallback_model() -> str:
    cfg = _load_config()
    return cfg.get("local", {}).get("fallback_model", "qwen2.5:3b")
