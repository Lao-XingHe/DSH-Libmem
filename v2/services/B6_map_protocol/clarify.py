# -*- coding: utf-8 -*-
"""反问 / 拒答 —— **产品与评测共用的唯一判定处**（V1.3.4 步骤 3）。

为什么要有这个模块（LIS 补充文档 §2.4 ① 的硬约束）：
    "判定**下沉到协议层**，`chat.py` 与评测脚本**调用同一个判定** —— 避免产品/评测分叉。"
本仓已经踩过一次这个坑（评测脚本全都直连 `retrieve()`、绕过 `chat.py`），
所以判词、判定函数、给模型的指令**都只写在这里**，两边 import 同一份。

## 三种"没答"要分清

| 情形 | 输出 | 含义 |
|---|---|---|
| **拒答** | `No information available` | 记忆里**确实没有**这件事（用户要的） |
| **反问** | `Need clarification: <一句问话>` | **不知道用户在问什么**（产品要的） |
| 作答 | 正常答案 | 记忆里有 |

**语言逻辑上反问也是"没答"**（没有对应内容可答，只能回问），
但它是**另一种行为**：拒答是放弃，反问是追问。所以：
  * 记分时两者**都算"正确地不答"**（口径 C，见 `PLAN-V1.3.4` §3.1）；
  * 但**反问率必须单列**，否则"追问"和"放弃"混成一个数就看不出来了。

## 实测依据（`PLAN-V1.3.4` §3.2）

* **反问路径已验证**：10 条模糊输入（照抄 LIS 文档实测到的真实用户原话
  "继续/可以/你好/好的/退出这个模式吧"…）→ **反问 8 条、拒答 2 条、编答案 0 条**；
  且问话内容**扣上下文**（"Continue with what — the job application topic, …"）。
* **拒答路径提示词单独做不到**：多拒陷阱题（+15 点）必然伴随多拒真题（+10~20 点），
  net 约 −0.017（非对抗）。所以这里**只装反问**，不装激进的拒答指令。
* `protocol.py` 的 `detect_ambiguity` 实测**触发 2.2% 且 11 条全是误伤**
  （"继续""你好""好的"这类**长度**触发，0 条真的指代不明）→ **它不是本模块的判定**，
  保留仅作统计；真正的判定是**模型读了上下文之后的自判**（本模块的指令）。
"""
from __future__ import annotations

import re

#: 反问判词。**必须带冒号**，机器按它判；模型被要求整行输出，前后不加别的。
CLARIFY_MARKER = 'Need clarification:'

#: 拒答判词 —— 沿用本仓既有约定（`full_9b_eval.refused()` 一直认它）。
REFUSE_SENTENCE = 'No information available'

_CLARIFY_RE = re.compile(r'need\s*clarification', re.I)
_REFUSE_RE = re.compile(r'no\s+information\s+available', re.I)


def is_clarify(text: str) -> bool:
    """这一句回答是不是**反问**。"""
    return bool(_CLARIFY_RE.search(text or ''))


def is_refusal(text: str) -> bool:
    """这一句回答是不是**拒答**（沿用既有约定：`No information available`/空/n-a）。"""
    t = (text or '').strip().lower()
    return bool(_REFUSE_RE.search(t)) or t in ('', 'n/a')


def clarify_question(text: str) -> str:
    """从回答里取出**要问用户的那句话**（判词之后的部分）。

    产品侧用它把问题回给用户；取不到就返回全文（总比丢掉好）。
    """
    m = _CLARIFY_RE.search(text or '')
    if not m:
        return (text or '').strip()
    rest = (text or '')[m.end():].lstrip(' :：-—')
    return rest.strip() or (text or '').strip()


#: 给模型的可答性指令 —— **产品（`chat.py`）与评测（`full_9b_eval.py` 的 v6+ 提示词）
#: 说的是同一件事**。放在这里是为了"改一处、两边同时生效"，避免又一次产品/评测分叉。
#:
#: ⚠️ 措辞是**实测调出来的**，别随手改（`PLAN-V1.3.4` §3.2 记了三个版本的横扫结果）：
#:   * 开头那句 framing **决定拒答倾向**，后面的情形规则只决定"分类对不对" ——
#:     v7 把情形边界整个重写、拒答率**一个数都没动**；改开头才动。
#:   * 所以这里的开头故意保留"**该答就答，别动不动拒答**"的调子 ——
#:     实测激进版会把真题误拒 +10~20 点，得不偿失。
CLARIFY_INSTRUCTION = f"""
【关于"记忆里没有"和"不知道你在问什么"】

先看上面的记忆，再决定属于哪一种：

1. **记忆里有**（哪怕是间接提到、或需要推一步）→ **正常作答**。这是绝大多数情况。
   不要因为"不是原话""不太确定"就拒答。

2. **记忆里确实没有这件事** → 直接输出：`{REFUSE_SENTENCE}`
   注意边界：是"**问的那个东西根本没出现过**"，不是"没按原话说"。
   如果话题相关但那个具体事实不在，也算这一种。

3. **不知道用户在问什么**（问题太模糊，或有"那个""它""继续"这类指代而看不出指谁）
   → **反问用户**，整行输出：`{CLARIFY_MARKER} <你要问用户的一句话>`
   问话要**具体**：能列出你猜的候选就列出来问（例如
   `{CLARIFY_MARKER} 你想继续的是哪件事——上次说的求职，还是体检那件？`）。
   除了这一行，什么都不要输出。
"""


def annotate(text: str) -> dict:
    """给一段回答打标 —— **产品侧记统计用**（对应 LIS 的 `unfilled request` 登记）。

    Returns: `{"clarify": bool, "refuse": bool, "question": str}`。
    """
    return {'clarify': is_clarify(text), 'refuse': is_refusal(text),
            'question': clarify_question(text) if is_clarify(text) else ''}
