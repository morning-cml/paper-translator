"""译文质量自检（B3）：纯本地规则，不额外调用模型。

只做**高置信度**的异常检测——阈值全部依据真实整篇译文语料标定
（116 段真实译文对，见 docs/未来路线图.md B3 条），确保正常译文零误报：

| 检查 | 判据 | 语料实测 |
|---|---|---|
| 截断 | 译/原 字符比 < 0.12 | 实测最小 0.20，留足余量 |
| 啰嗦 | 比 > 1.6（且原文够长） | 实测最大 0.88 |
| 数字错漏 | **≥10 或带小数**的数字未出现在译文 | 小数字会被正确译成"二/两"，故豁免 |
| 元话语 | 以"以下是/译文：/好的，"等开头 | 实测 0 命中 |
| 重复退化 | 同一 12 字片段重复 ≥4 次 | 正常文本不会 |

命中即触发**一次**定向重译（成本可控：绝大多数段落不会命中）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import List

_PH = re.compile(r"⟦F\d+⟧")
_NUM = re.compile(r"\d+(?:\.\d+)?")
# 只认"套话 + 指明这是译文"的组合。"以下是 / 这是 / 当然，/ Here is" 单独出现
# 都可能是正文本身——"The following are…"→"以下是…"、"This is because…"→
# "这是因为…"、zh→en 的 "Here is a study…"——旧规则把它们一概判为元话语，
# 每段白发一次重译请求，而重译照样这么开头、修正必被拒，纯属浪费。
_META = re.compile(
    r"^\s*(?:(?:以下|下面|这)是.{0,15}?(?:译文|翻译)|翻译如下|译文如下|"
    r"翻译[:：]|译文[:：]|好的[，,！!]|当然[，,！!]\s*(?:以下|下面|这)是|"
    r"Translation\s*[:：]|Here(?:'s| is) (?:the |your |my )?"
    r"(?:\w+ ){0,2}translation)", re.I)

# 中文数字写法（模型把 "2" 译成"二/两"是正确的，不算错漏）
_CN_DIGITS = {
    "0": "〇零", "1": "一壹", "2": "二两贰", "3": "三叁", "4": "四肆",
    "5": "五伍", "6": "六陆", "7": "七柒", "8": "八捌", "9": "九玖",
}

MIN_RATIO = 0.12
MAX_RATIO = 1.6
REPEAT_WINDOW = 12
REPEAT_TIMES = 4


@dataclass(frozen=True)
class Issue:
    code: str      # 机器可读
    detail: str    # 给模型看的中文说明


def _strip(s: str) -> str:
    return _PH.sub("", s or "").strip()


_CN_UNIT = "零一二三四五六七八九"


def _cn_forms(num: str) -> set:
    """生成该数字**可能的中文写法**集合，用于宽松匹配。

    逐位（"20"→"二〇"，年份/编号常见）与十进位（"20"→"二十"，计数常见）
    都要覆盖——只认一种会误判正确译文为"数字丢失"。
    """
    forms = {"".join(_CN_DIGITS.get(c, (c,))[0] for c in num)}
    if "." in num:
        return forms
    try:
        v = int(num)
    except ValueError:
        return forms
    if 10 <= v <= 99:                      # 十/二十/二十五
        tens, ones = divmod(v, 10)
        s = ("十" if tens == 1 else _CN_UNIT[tens] + "十")
        forms.add(s + (_CN_UNIT[ones] if ones else ""))
    elif 100 <= v <= 999 and v % 100 == 0:  # 一百/两百
        forms.add(_CN_UNIT[v // 100] + "百")
        forms.add("两百" if v == 200 else "")
    forms.discard("")
    return forms


def _missing_numbers(src: str, tgt: str, cn_forms=None) -> List[str]:
    """只查"不该被改写"的数字：≥10 的整数或带小数点的数。

    1~9 的小数字常被正确译为中文数字（study 2 → 实验二、2-week → 两周），
    纳入检查会大量误报——真实语料里 4 处"缺失"全是这种情况。
    cn_forms：中文数字写法生成器（仅目标为中文时传入）。
    """
    cn_forms = cn_forms or (lambda n: set())
    missing = []
    for n in _NUM.findall(src):
        if "." not in n and len(n.lstrip("0")) <= 1:
            continue                       # 个位数：豁免
        if n in tgt:
            continue
        if any(f in tgt for f in cn_forms(n)):   # 宽松：允许中文数字写法
            continue
        missing.append(n)
    return missing


def _max_repeat(text: str) -> int:
    if len(text) < REPEAT_WINDOW * 2:
        return 0
    c = Counter(text[i:i + REPEAT_WINDOW]
                for i in range(len(text) - REPEAT_WINDOW + 1))
    return max(c.values())


def check(src: str, tgt: str, source_code: str = "en",
          target_code: str = "zh") -> List[Issue]:
    """返回译文的质量问题列表；空列表 = 通过。

    长度带按语言对取（B5）：en→zh 用标定的 0.12–1.6；zh→en 等膨胀方向用更宽
    的带；数字/元话语/重复检查与语言无关。目标非中文时数字检查退化为纯匹配。
    """
    from .languages import BY_CODE, length_band
    issues: List[Issue] = []
    cs, ct = _strip(src), _strip(tgt)
    if not ct:
        return [Issue("empty", "译文为空")]

    band = length_band(source_code, target_code)
    if band:
        lo, hi = band
        if len(cs) > 80 and len(ct) / len(cs) < lo:
            issues.append(Issue(
                "too_short",
                f"译文明显过短（仅为原文长度的 {len(ct) / len(cs):.0%}），"
                "像是漏译或被截断，请完整翻译全部内容"))
        if len(cs) > 40 and len(ct) / len(cs) > hi:
            issues.append(Issue(
                "too_long",
                f"译文明显过长（达原文长度的 {len(ct) / len(cs):.0%}），"
                "可能混入了解释或重复，请只输出对应译文"))

    # 数字保留检查只在目标为中文时用"中文数字写法"宽松匹配；其他目标纯匹配
    tgt_lang = BY_CODE.get(target_code)
    cn_forms = _cn_forms if (tgt_lang and tgt_lang.code == "zh") else (lambda n: set())
    missing = _missing_numbers(cs, ct, cn_forms)
    if missing:
        issues.append(Issue(
            "missing_numbers",
            "以下数字在译文中丢失了，必须原样保留："
            + "、".join(missing[:8])))

    if _META.match(ct):
        issues.append(Issue(
            "meta_prefix", "译文开头混入了「以下是译文」之类的多余说明，"
                           "请直接输出译文本身"))

    if _max_repeat(ct) >= REPEAT_TIMES:
        issues.append(Issue(
            "repetition", "译文出现大段重复内容，请重新翻译一遍，不要重复"))

    return issues


def describe(issues: List[Issue]) -> str:
    return "；".join(i.detail for i in issues)
