"""llama.cpp router 后端（OpenAI 兼容），统一本地模型服务的唯一出口。

取代 ``OllamaBackend``：DS工程启动器 拉起的 llama router 在
``127.0.0.1:8089`` 上以 OpenAI 兼容协议暴露多个本地模型
（``Qwen3.5-4B-Q4_K_M`` / ``Qwen3.5-9B-Q4_K_M`` / 35B-A3B 等），
所以本地小模型、大模型兜底都走这一个服务，不再需要 Ollama。

两个必须注意的点
----------------

1. **默认关闭思维链。** Qwen3.5 系列默认先输出一大段
   ``reasoning_content``；实测 ``max_tokens=512`` 会被思维链整个吃光，
   ``content`` 返回空串且 ``finish_reason="length"``。
   同一请求加上 ``chat_template_kwargs={"enable_thinking": false}`` 后
   217ms 返回 ``finish_reason="stop"``。提炼这类任务要的是稳定 JSON，
   所以这里默认关闭，并且遇到不认识该参数的服务端会**自动降级重试一次**。

2. **不需要 API key。** 但部分网关要求 Authorization 头存在，所以仍带一个
   占位值；llama-server 会忽略它。

路径解析沿用 ``ollama_backend`` 的形状（``infer`` / ``infer_json`` /
``check_health`` / ``name``），调用方无需改动。
"""

import json
import re

import requests

from .base import LLMBackend

#: 启动器里 llama router 的默认地址（models.ini 由 DS工程启动器 使用）。
DEFAULT_BASE_URL = "http://127.0.0.1:8089/v1"

#: router 上默认的小模型（gguf 文件名去掉 .gguf）。
DEFAULT_MODEL = "Qwen3.5-4B-Q4_K_M"


class LlamaServerBackend(LLMBackend):
    """OpenAI 兼容的 llama.cpp router 客户端。"""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 120,
        max_retries: int = 2,
        num_predict: int = 4096,
        temperature: float = 0.1,
        num_ctx: int = 131072,
        enable_thinking: bool = False,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._url = f"{self._base_url}/chat/completions"
        self._timeout = timeout
        self._max_retries = max_retries
        self._num_predict = num_predict
        self._temperature = temperature
        self._num_ctx = num_ctx
        self._enable_thinking = enable_thinking

    @property
    def name(self) -> str:
        return f"LlamaServer/{self._model}"

    def check_health(self) -> dict:
        try:
            resp = requests.get(f"{self._base_url}/models", timeout=5)
            if resp.status_code != 200:
                return {"healthy": False, "message": f"llama router 返回异常 HTTP {resp.status_code}"}
            ids = [m.get("id") for m in resp.json().get("data", [])]
            if ids and self._model not in ids:
                return {"healthy": False, "message": f"router 在跑，但没有模型 {self._model}（可用: {', '.join(ids)}）"}
            return {"healthy": True, "message": "已连接"}
        except requests.exceptions.ConnectionError:
            return {"healthy": False, "message": "llama router 未启动（应由 DS工程启动器 拉起 8089）"}
        except requests.exceptions.Timeout:
            return {"healthy": False, "message": "llama router 连接超时"}
        except Exception as e:  # noqa: BLE001
            return {"healthy": False, "message": str(e)[:80]}

    def _build_body(self, messages: list, task_options: dict = None, disable_thinking: bool = True) -> dict:
        max_tokens = self._num_predict
        temperature = self._temperature
        if task_options:
            # 沿用 Ollama 后端的任务级覆盖语义（task_config.TASK_CONFIG）
            if "num_predict" in task_options:
                max_tokens = task_options["num_predict"]
            if "temperature" in task_options:
                temperature = task_options["temperature"]
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if disable_thinking and not self._enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def infer(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # 服务端若不认识 chat_template_kwargs，第一次会 400；降级去掉该参数重试。
        variants = [True, False] if not self._enable_thinking else [False]
        for disable_thinking in variants:
            body = self._build_body(messages, task_options, disable_thinking)
            for attempt in range(self._max_retries + 1):
                try:
                    resp = requests.post(
                        self._url,
                        headers={
                            "Authorization": "Bearer local",
                            "Content-Type": "application/json",
                        },
                        json=body,
                        timeout=self._timeout,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("error"):
                            err_msg = data.get("error")
                            print(f"  [LlamaServer] 模型错误: {err_msg}")
                            return f"[错误] llama router 返回错误: {err_msg}"
                        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
                        result = (msg.get("content") or "").strip()
                        if result:
                            return result
                        reasoning = (msg.get("reasoning_content") or "").strip()
                        finish = (data.get("choices") or [{}])[0].get("finish_reason")
                        print(
                            f"  [LlamaServer] 空回复 (finish_reason={finish}"
                            f"{', 只有思维链' if reasoning else ''})"
                        )
                        return ""
                    if resp.status_code == 400 and disable_thinking:
                        # 该服务端不支持 chat_template_kwargs —— 去掉参数再来一遍
                        print(f"  [LlamaServer] 不支持 chat_template_kwargs，降级重试: {resp.text[:120]}")
                        break
                    print(f"  [LlamaServer] HTTP {resp.status_code}: {resp.text[:200]}")
                    if attempt == self._max_retries:
                        return f"[错误] llama router 返回 HTTP {resp.status_code}"
                except requests.exceptions.ConnectionError:
                    print(f"  [LlamaServer] 无法连接 {self._base_url}")
                    return "[错误] llama router 未启动，请先运行 DS工程启动器.bat（8089）"
                except requests.exceptions.Timeout:
                    print(f"  [LlamaServer] 超时 (timeout={self._timeout}s, attempt={attempt + 1})")
                    if attempt == self._max_retries:
                        return "[错误] llama router 请求超时，模型可能正在加载中，请重试"
                except Exception as e:  # noqa: BLE001
                    print(f"  [LlamaServer] 异常: {e}")
                    return f"[错误] llama router 请求异常: {str(e)}"
        return ""

    def infer_json(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> dict:
        text = self.infer(prompt, system_prompt, task_options)
        if not text or text.startswith("[错误]"):
            return {}
        json_str = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
        for line in json_str.split("\n"):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                json_str = line
                break
        try:
            return json.loads(json_str)
        except Exception:  # noqa: BLE001
            pass
        m = re.search(r"\{[^{}]*\}", json_str)
        if m:
            try:
                return json.loads(m.group())
            except Exception:  # noqa: BLE001
                pass
        m = re.search(r"\{[\s\S]*\}", json_str)
        if m:
            try:
                return json.loads(m.group())
            except Exception:  # noqa: BLE001
                pass
        print("  [LlamaServer] JSON解析失败")
        return {}
