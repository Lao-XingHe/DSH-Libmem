"""C3 人设与系统提示 — 从 V1 config/loader.py 中 personas 相关逻辑独立出来"""
import yaml, os, sys

def _get_base_dir():
    env_dir = os.environ.get("SHUFANG_DATA_DIR")
    if env_dir: return env_dir
    if getattr(sys, "frozen", False): return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PERSONAS_PATH = os.path.join(_get_base_dir(), "personas.yaml")

_DEFAULT_PERSONAS = {
    "default_persona": "shufang_zhushou",
    "memory_enhance": True,
    "presets": {
        "shufang_zhushou": {"name": "书房助手", "prompt": "你是一位温文尔雅的书房助手..."},
        "academic": {"name": "学术顾问", "prompt": "你是一位严谨的学术研究顾问..."},
        "code_master": {"name": "代码导师", "prompt": "你是一位经验丰富的编程导师..."},
        "creative": {"name": "创意伙伴", "prompt": "你是一位充满灵感的创意伙伴..."},
    },
    "custom_personas": [],
}

def load_personas():
    if not os.path.exists(_PERSONAS_PATH):
        os.makedirs(os.path.dirname(_PERSONAS_PATH), exist_ok=True)
        yaml.dump(_DEFAULT_PERSONAS, open(_PERSONAS_PATH, "w", encoding="utf-8"), allow_unicode=True, default_flow_style=False)
        return _DEFAULT_PERSONAS.copy()
    return yaml.safe_load(open(_PERSONAS_PATH, "r", encoding="utf-8")) or _DEFAULT_PERSONAS.copy()

def resolve_persona(personas, persona_key=None, persona_custom=None):
    if persona_custom: return persona_custom
    key = persona_key or personas.get("default_persona", "")
    if not key: return ""
    presets = personas.get("presets", {})
    if key in presets: return presets[key].get("prompt", "")
    for c in personas.get("custom_personas", []):
        if c.get("key") == key: return c.get("prompt", "")
    return ""
