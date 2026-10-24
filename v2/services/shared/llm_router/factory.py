from .base import LLMBackend
from .deepseek_backend import DeepSeekBackend
from .llama_cpp_backend import LlamaCppBackend
from .llama_server_backend import (
    LlamaServerBackend,
    DEFAULT_BASE_URL as LLAMA_SERVER_BASE_URL,
    DEFAULT_MODEL as LLAMA_SERVER_MODEL,
)
from .task_config import get_task_options, get_preferred_model, get_fallback_model

# 本地引擎只有 llama 家族（2026-09-22 统一模型服务，Ollama 已从运行时移除）：
#   llama_server = 走启动器拉起的 llama router(8089)，OpenAI 兼容，默认
#   llama_cpp    = 进程内加载 gguf（备用，需 model_path）
LOCAL_ENGINE_TYPES = ("llama_server", "llama_cpp")

def _make_deepseek(cfg: dict) -> DeepSeekBackend:
    return DeepSeekBackend(
        model=cfg.get("model", "deepseek-chat"),
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        api_key=cfg.get("api_key", ""),
        timeout=cfg.get("timeout", 120),
        max_retries=cfg.get("max_retries", 1),
        enable_reasoning=cfg.get("enable_reasoning", False),
        reasoning_effort=cfg.get("reasoning_effort", "high"),
    )

def create_llm_backend(config: dict) -> "SimpleLLMProxy":
    llm_config = config.get("llm", {})
    local_engine = llm_config.get("local_engine", "llama_server")
    small_cfg = llm_config.get("small", {})
    if local_engine == "llama_cpp":
        small = LlamaCppBackend(model_path=small_cfg.get("model_path", ""), n_ctx=small_cfg.get("n_ctx", 4096), n_gpu_layers=small_cfg.get("n_gpu_layers", 0), n_threads=small_cfg.get("n_threads", None), timeout=small_cfg.get("timeout", 120), max_retries=small_cfg.get("max_retries", 2), num_predict=small_cfg.get("num_predict", 4096), temperature=small_cfg.get("temperature", 0.1))
    else:
        # 默认分支（含未知取值）一律走 llama router，绝不再退回 Ollama
        small = LlamaServerBackend(model=small_cfg.get("model", LLAMA_SERVER_MODEL), base_url=small_cfg.get("base_url", LLAMA_SERVER_BASE_URL), timeout=small_cfg.get("timeout", 120), max_retries=small_cfg.get("max_retries", 2), num_predict=small_cfg.get("num_predict", 4096), temperature=small_cfg.get("temperature", 0.1), num_ctx=small_cfg.get("num_ctx", 131072), enable_thinking=small_cfg.get("enable_thinking", False))
    large_cfg = llm_config.get("large", {})
    backend_type = large_cfg.get("provider", "deepseek")
    if backend_type == "deepseek":
        large = _make_deepseek(large_cfg)
    elif backend_type in ("llama_server", "ollama"):
        # "ollama" 是保号兼容：老配置里写了它，也按 llama router 解释
        large = LlamaServerBackend(model=large_cfg.get("model", LLAMA_SERVER_MODEL), base_url=large_cfg.get("base_url", LLAMA_SERVER_BASE_URL), timeout=large_cfg.get("timeout", 120), max_retries=large_cfg.get("max_retries", 1), num_predict=large_cfg.get("num_predict", 4096), temperature=large_cfg.get("temperature", 0.1), num_ctx=large_cfg.get("num_ctx", 131072), enable_thinking=large_cfg.get("enable_thinking", False))
    else:
        large = _make_deepseek(large_cfg)
    return SimpleLLMProxy(small, large, large_config=large_cfg)

class SimpleLLMProxy:
    def __init__(self, small: LLMBackend, large: LLMBackend, large_config: dict = None):
        self.small = small
        self.large = large
        self.large_config = large_config or {}

    def infer_small(self, prompt: str, system_prompt: str = None, task_type: str = "default") -> str:
        task_opts = get_task_options(task_type)
        try:
            result = self.small.infer(prompt, system_prompt, task_opts)
            if result and not result.startswith("[错误]"):
                return result
            if result:
                print(f"  [Task] {task_type} 失败: {result[:80]}")
        except Exception as e:
            print(f"  [Task] {task_type} 异常: {e}")
        if get_fallback_model() != get_preferred_model():
            print(f"  [Task] {task_type} 尝试 fallback 模型 {get_fallback_model()}...")
            saved = self.small._model
            self.small._model = get_fallback_model()
            try:
                result = self.small.infer(prompt, system_prompt, task_opts)
                if result and not result.startswith("[错误]"):
                    return result
            except Exception as e:
                print(f"  [Task] {task_type} fallback 也异常: {e}")
            finally:
                self.small._model = saved
        return ""

    def infer_small_json(self, prompt: str, system_prompt: str = None, task_type: str = "default") -> dict:
        task_opts = get_task_options(task_type)
        result = self.small.infer_json(prompt, system_prompt, task_opts)
        if not result or not result.get("summary") or not result.get("topics"):
            if get_fallback_model() != get_preferred_model():
                print(f"  [Task] {task_type} JSON异常，尝试 fallback 模型 {get_fallback_model()}...")
                saved = self.small._model
                self.small._model = get_fallback_model()
                try:
                    result = self.small.infer_json(prompt, system_prompt, task_opts)
                except Exception as e:
                    print(f"  [Task] {task_type} fallback 异常: {e}")
                    result = {}
                finally:
                    self.small._model = saved
        return result or {}

    def infer_large(self, prompt: str, system_prompt: str = None, override_model: str = None):
        if override_model:
            cfg = dict(self.large_config)
            cfg["model"] = override_model
            cfg["enable_reasoning"] = True
            temp = _make_deepseek(cfg)
            return temp.infer(prompt, system_prompt)
        return self.large.infer(prompt, system_prompt)

    def chat_large(self, messages: list, system_prompt: str = None) -> str:
        full = []
        if system_prompt:
            full.append({"role": "system", "content": system_prompt})
        full.extend(messages)
        prompt = "\n\n".join(f"{m['role']}: {m['content']}" for m in full)
        return self.large.infer(prompt)