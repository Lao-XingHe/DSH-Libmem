"""小模型输出协议：**一张表**既生成 prompt、也校验输出。

为什么要有协议
--------------
之前 prompt 是手写的字符串，字段规则散在 prompt 和代码里各一份 —— 改一处忘一处，
而且模型多填/少填/填错类型都没人拦（`if "summary" in result` 就算过）。
现在把"该填什么"抽成 `v2/extract_protocol.yaml` 一张表：

    表 → build_prompt() 生成 prompt
    表 → validate() 校验输出（必填/类型/条数/长度/泛词/枚举）

两条纪律
--------
* 协议文件缺失或坏了 → 回退到旧 prompt（绝不让提炼停摆）；
* 校验失败不丢整条：能救的字段留下，问题写进 `errors` 由调用方打日志。
"""
from __future__ import annotations

import os
import re

_PROTOCOL_PATH = os.environ.get("LIBMEM_EXTRACT_PROTOCOL") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extract_protocol.yaml")

_DEFAULT_PROTOCOL = {
    "version": 1,
    "forbid_generic_terms": [],
    "fields": {
        "task_type": {"type": "enum", "required": True, "values_from": "task_types.yaml"},
        "summary": {"type": "string", "required": True, "max_chars": 120},
        "topics": {"type": "array", "items": "string", "max_items": 3},
        "entities": {"type": "array", "items": "string", "max_items": 4},
        "relations": {"type": "array", "items": "string", "max_items": 3},
        "temporal": {"type": "array", "items": "string", "max_items": 2},
        "drafted": {"type": "boolean"},
    },
}

_cached: dict | None = None


def load_protocol() -> dict:
    global _cached
    if _cached is not None:
        return _cached
    try:
        import yaml
        data = yaml.safe_load(open(_PROTOCOL_PATH, encoding="utf-8")) or {}
        fields = data.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields 为空")
        _cached = {"version": data.get("version", 1),
                   "forbid_generic_terms": [str(x).lower() for x in (data.get("forbid_generic_terms") or [])],
                   "fields": fields}
    except Exception:
        _cached = _DEFAULT_PROTOCOL
    return _cached


def _type_hint(name: str, spec: dict, allowed_values: list[str]) -> str:
    t = spec.get("type", "string")
    if t == "enum":
        vals = "、".join(allowed_values) if allowed_values else "（见 task_types.yaml）"
        return f'必须是这些之一：{vals}'
    if t == "array":
        n = spec.get("max_items", 3)
        hint = f"数组，最多 {n} 个元素"
        if spec.get("forbid_generic"):
            hint += "；禁止泛词（如 AI/项目/东西/工具）"
        return hint
    if t == "boolean":
        return "true / false"
    if t == "string":
        mc = spec.get("max_chars")
        return f"字符串{('，最长 %d 字' % mc) if mc else ''}"
    return t


def build_prompt(user: str, assistant: str, task_values: list[str] | None = None,
                 task_judges: str = "") -> str:
    """按协议表生成提炼 prompt。"""
    proto = load_protocol()
    allowed = list(task_values or [])
    lines = ["从以下对话记录中提取关键信息，按下面的表逐项填写，生成一条 JSON。", "",
             "输出格式：一个 JSON 对象，只包含下面列出的字段，不要额外字段。", ""]
    for name, spec in proto["fields"].items():
        req = "（必填）" if spec.get("required") else "（可选）"
        lines.append(f"- {name}{req}：{_type_hint(name, spec, allowed)}")
        if spec.get("description"):
            lines.append(f"    {spec['description']}")
    if task_judges:
        lines += ["", "task_type 判据：", task_judges]
    lines += ["", "对话记录：", f"用户：{user}", f"助手：{assistant}", "",
              "规则：不确定的字段留空数组或省略；不要编造对话里没有的信息。",
              "只输出 JSON，不要解释、不要代码块标记。"]
    return "\n".join(lines)


def validate(result) -> tuple[dict, list[str]]:
    """按协议表校验/清洗模型输出。返回 (cleaned, errors)。"""
    proto = load_protocol()
    errors: list[str] = []
    if not isinstance(result, dict):
        return {}, ["输出不是 JSON 对象"]
    cleaned: dict = {}
    forbid = set(proto.get("forbid_generic_terms") or [])
    for name, spec in proto["fields"].items():
        t = spec.get("type", "string")
        val = result.get(name)
        if val is None:
            if spec.get("required"):
                errors.append(f"{name}: 缺必填字段")
            continue
        if t == "enum":
            s = str(val).strip()
            cleaned[name] = s
        elif t == "string":
            s = str(val).strip()
            mc = spec.get("max_chars")
            if mc and len(s) > mc:
                # 尽量截在标点处，别把词/句子砍半
                cut = s[:mc]
                for sep in ("。", "；", "，", "、", " "):
                    pos = cut.rfind(sep)
                    if pos >= mc * 0.6:
                        cut = cut[:pos]
                        break
                s = cut
                errors.append(f"{name}: 超长已截断到 {len(s)} 字")
            cleaned[name] = s
        elif t == "boolean":
            cleaned[name] = bool(val)
        elif t == "array":
            if not isinstance(val, list):
                val = [val]
            items = []
            for x in val:
                s = str(x).strip()
                if not s:
                    continue
                if spec.get("forbid_generic") and s.lower() in forbid:
                    errors.append(f"{name}: 去掉泛词 {s}")
                    continue
                if s.lower() in forbid and spec.get("forbid_generic"):
                    continue
                items.append(s)
            mx = spec.get("max_items")
            if mx and len(items) > mx:
                errors.append(f"{name}: 超出 {mx} 个，已截断")
                items = items[:mx]
            cleaned[name] = items
    unknown = set(result) - set(proto["fields"])
    if unknown:
        errors.append("丢弃未知字段: " + ",".join(sorted(unknown)))
    return cleaned, errors
