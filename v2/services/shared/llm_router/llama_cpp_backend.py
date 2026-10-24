import json, re
from .base import LLMBackend

class LlamaCppBackend(LLMBackend):
    def __init__(self, model_path: str = "", n_ctx: int = 4096, n_gpu_layers: int = 0, n_threads: int = None, timeout: int = 120, max_retries: int = 2, num_predict: int = 4096, temperature: float = 0.1):
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._n_threads = n_threads
        self._timeout = timeout
        self._max_retries = max_retries
        self._num_predict = num_predict
        self._temperature = temperature
        self._llm = None
        self._loaded = False
        self._load_error = None

    @property
    def name(self) -> str:
        if self._model_path:
            import os; return f"内置引擎/{os.path.basename(self._model_path)}"
        return "内置引擎/未加载"

    @property
    def is_loaded(self) -> bool: return self._loaded

    @property
    def load_error(self) -> str: return self._load_error or ""

    def check_health(self) -> dict:
        if self._loaded:
            return {"healthy": True, "message": "模型已加载"}
        if not self._model_path:
            return {"healthy": False, "message": "未指定模型文件路径"}
        if not self._load_error:
            return {"healthy": None, "message": "模型尚未加载（首次调用时自动加载）"}
        return {"healthy": False, "message": self._load_error}

    def _ensure_loaded(self):
        if self._loaded: return True
        if not self._model_path: self._load_error = "未指定模型文件路径"; return False
        try:
            from llama_cpp import Llama
            import os
            if not os.path.exists(self._model_path): self._load_error = f"模型文件不存在: {self._model_path}"; return False
            kwargs = {"model_path": self._model_path, "n_ctx": self._n_ctx, "verbose": False}
            if self._n_gpu_layers >= 0: kwargs["n_gpu_layers"] = self._n_gpu_layers
            if self._n_threads is not None: kwargs["n_threads"] = self._n_threads
            self._llm = Llama(**kwargs); self._loaded = True; return True
        except ImportError: self._load_error = "未安装 llama-cpp-python，请运行: pip install llama-cpp-python"; return False
        except Exception as e: self._load_error = f"加载失败: {e}"; return False

    def infer(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> str:
        # task_options: 暂未使用，接口统一
        if not self._ensure_loaded(): return f"[错误] {self._load_error}"
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        for attempt in range(self._max_retries + 1):
            try:
                result = self._llm(full_prompt, max_tokens=self._num_predict, temperature=self._temperature)
                text = result.get("choices", [{}])[0].get("text", "").strip()
                if text: return text
                return ""
            except Exception as e: print(f"  [内置引擎] 异常: {e}")
        return ""

    def infer_json(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> dict:
        # task_options: 暂未使用，接口统一
        text = self.infer(prompt, system_prompt, task_options)
        if not text: return {}
        json_str = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE).strip()
        for line in json_str.split("\n"):
            if line.strip().startswith("{") and line.strip().endswith("}"): json_str = line.strip(); break
        try: return json.loads(json_str)
        except:
            m = re.search(r'\{[\s\S]*\}', json_str)
            if m:
                try: return json.loads(m.group())
                except: pass
        return {}
