import json, re, requests
from .base import LLMBackend

class OllamaBackend(LLMBackend):
    def __init__(self, model: str = "qwen2.5:3b", base_url: str = "http://localhost:11434", timeout: int = 120, max_retries: int = 2, num_predict: int = 4096, temperature: float = 0.1, num_ctx: int = 131072):
        self._model = model
        self._base_url = base_url.rstrip('/')
        self._url = f"{self._base_url}/api/generate"
        self._timeout = timeout
        self._max_retries = max_retries
        self._num_predict = num_predict
        self._temperature = temperature
        self._num_ctx = num_ctx

    @property
    def name(self) -> str: return f"Ollama/{self._model}"

    def check_health(self) -> dict:
        try:
            resp = requests.get(f"{self._base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                return {"healthy": True, "message": "已连接"}
            return {"healthy": False, "message": f"Ollama 返回异常 HTTP {resp.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"healthy": False, "message": "Ollama 服务未启动"}
        except requests.exceptions.Timeout:
            return {"healthy": False, "message": "Ollama 连接超时"}
        except Exception as e:
            return {"healthy": False, "message": str(e)[:80]}

    def infer(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> str:
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        options = {"temperature": self._temperature, "num_predict": self._num_predict, "num_ctx": self._num_ctx}
        if task_options:
            if "num_ctx" in task_options:
                options["num_ctx"] = task_options["num_ctx"]
            if "num_predict" in task_options:
                options["num_predict"] = task_options["num_predict"]
        payload = {"model": self._model, "prompt": full_prompt, "stream": False, "options": options}
        for attempt in range(self._max_retries + 1):
            try:
                resp = requests.post(self._url, json=payload, timeout=self._timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("error"):
                        err_msg = data.get("error", "")
                        print(f"  [Ollama] 模型错误: {err_msg}")
                        return f"[错误] Ollama 返回错误: {err_msg}"
                    result = data.get("response", "").strip()
                    if result:
                        return result
                    if data.get("done"):
                        reason = data.get("done_reason", "未知")
                        print(f"  [Ollama] 空回复 (done_reason={reason})")
                        return ""
                else:
                    err_text = resp.text[:200]
                    print(f"  [Ollama] HTTP {resp.status_code}: {err_text}")
                    if attempt == self._max_retries:
                        return f"[错误] Ollama 返回 HTTP {resp.status_code}"
            except requests.exceptions.ConnectionError:
                print("  [Ollama] 无法连接")
                return "[错误] Ollama 服务未启动，请运行 ollama serve 启动服务"
            except requests.exceptions.Timeout:
                print(f"  [Ollama] 超时 (timeout={self._timeout}s, attempt={attempt+1})")
                if attempt == self._max_retries:
                    return f"[错误] Ollama 请求超时，模型可能正在加载中，请重试"
            except Exception as e:
                print(f"  [Ollama] 异常: {e}")
                return f"[错误] Ollama 请求异常: {str(e)}"
        return ""

    def infer_json(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> dict:
        text = self.infer(prompt, system_prompt, task_options)
        if not text: return {}
        json_str = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE).strip()
        for line in json_str.split("\n"):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"): json_str = line; break
        try: return json.loads(json_str)
        except: pass
        m = re.search(r'\{[^{}]*\}', json_str)
        if m:
            try: return json.loads(m.group())
            except: pass
        print(f"  [Ollama] JSON解析失败")
        return {}
