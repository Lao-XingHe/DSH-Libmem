from abc import ABC, abstractmethod

class LLMBackend(ABC):
    @abstractmethod
    def infer(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> str: pass

    @abstractmethod
    def infer_json(self, prompt: str, system_prompt: str = None, task_options: dict = None) -> dict: pass

    @property
    @abstractmethod
    def name(self) -> str: pass

    def check_health(self) -> dict:
        return {"healthy": None, "message": ""}
