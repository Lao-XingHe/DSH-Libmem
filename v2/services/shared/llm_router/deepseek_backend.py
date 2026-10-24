import json, re, requests
from .base import LLMBackend

class DeepSeekBackend(LLMBackend):
    def __init__(self, model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com",
                 api_key: str = "", timeout: int = 120, max_retries: int = 1,
                 enable_reasoning: bool = False, reasoning_effort: str = "high"):
        self._model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._enable_reasoning = enable_reasoning
        self._reasoning_effort = reasoning_effort

    @property
    def name(self) -> str:
        return f"DeepSeek/{self._model}"

    def check_health(self) -> dict:
        if not self._api_key:
            return {"healthy": False, "message": "未配置 API Key"}
        try:
            resp = requests.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                timeout=10)
            if resp.status_code == 200:
                return {"healthy": True, "message": "已连接"}
            elif resp.status_code == 401:
                return {"healthy": False, "message": "API Key 无效 (401)"}
            elif resp.status_code == 403:
                return {"healthy": False, "message": "API 权限不足 (403)"}
            elif resp.status_code == 404:
                return {"healthy": False, "message": f"模型 {self._model} 不存在 (404)"}
            return {"healthy": False, "message": f"API 返回 HTTP {resp.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"healthy": False, "message": "无法连接 API 地址"}
        except requests.exceptions.Timeout:
            return {"healthy": False, "message": "API 连接超时"}
        except Exception as e:
            return {"healthy": False, "message": str(e)[:80]}

    def _build_body(self, messages: list) -> dict:
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": 4096,
            "stream": False,
        }
        if self._enable_reasoning:
            body["extra_body"] = {"thinking": {"type": "enabled"}}
            body["reasoning_effort"] = self._reasoning_effort
        else:
            body["temperature"] = 0.7
        return body

    def infer(self, prompt: str, system_prompt: str = None, task_options: dict = None):
        # task_options: 暂未使用，接口统一
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(self._max_retries + 1):
            try:
                resp = requests.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._build_body(messages),
                    timeout=self._timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content", "").strip()
                    reasoning = msg.get("reasoning_content", "").strip()
                    if self._enable_reasoning:
                        return {"content": content, "reasoning_content": reasoning}
                    return content
                else:
                    print(f"  [DeepSeek] HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.exceptions.Timeout:
                print(f"  [DeepSeek] 超时")
            except Exception as e:
                print(f"  [DeepSeek] 异常: {e}")
        return "" if not self._enable_reasoning else {"content": "", "reasoning_content": ""}

    def infer_json(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> dict:
        # task_options: 暂未使用，接口统一
        result = self.infer(prompt, system_prompt, task_options)
        text = result if isinstance(result, str) else result.get("content", "")
        if not text:
            return {}
        json_str = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(json_str)
        except:
            pass
        m = re.search(r'\{[\s\S]*\}', json_str)
        if m:
            try:
                return json.loads(m.group())
            except:
                pass
        print(f"  [DeepSeek] JSON解析失败")
        return {}