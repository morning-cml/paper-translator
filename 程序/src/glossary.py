"""术语库：加载英中术语对照，并为待译文本挑选相关术语注入提示词。

CSV 格式（含表头）：en,zh,note
匹配采用「按单词边界、忽略大小写」的方式，优先匹配较长的短语，
以保证例如 "convolutional neural network" 先于 "neural network" 命中。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Tuple


class Glossary:
    def __init__(self, entries: Dict[str, str]):
        # entries: 小写英文 -> 中文
        self.entries = entries
        # 按短语长度降序，便于长短语优先匹配
        self._sorted_terms = sorted(entries.keys(), key=len, reverse=True)
        # 预编译匹配正则（整词/短语边界）
        if self._sorted_terms:
            pattern = "|".join(re.escape(t) for t in self._sorted_terms)
            # (?<![A-Za-z0-9]) ... (?![A-Za-z0-9]) 保证边界，避免 "index" 命中 "indexed"
            self._regex = re.compile(
                r"(?<![A-Za-z0-9])(" + pattern + r")(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
        else:
            self._regex = None

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        path = Path(path)
        entries: Dict[str, str] = {}
        if not path.exists():
            print(f"[glossary] 术语库不存在：{path}，将不使用术语库。")
            return cls(entries)
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                en = (row.get("en") or "").strip()
                zh = (row.get("zh") or "").strip()
                if en and zh:
                    entries[en.lower()] = zh
        print(f"[glossary] 已加载术语 {len(entries)} 条：{path}")
        return cls(entries)

    def relevant_terms(self, text: str) -> List[Tuple[str, str]]:
        """返回文本中出现的术语列表 [(原文形态, 中文)]，去重、保序。"""
        if not self._regex:
            return []
        seen = set()
        found: List[Tuple[str, str]] = []
        for m in self._regex.finditer(text):
            surface = m.group(0)
            zh = self.entries.get(surface.lower())
            key = surface.lower()
            if zh and key not in seen:
                seen.add(key)
                found.append((surface, zh))
        return found

    def prompt_block(self, texts: List[str], limit: int = 40) -> str:
        """汇总多段文本中出现的术语，生成注入提示词的对照表；无则返回空串。"""
        collected: Dict[str, str] = {}
        for t in texts:
            for surface, zh in self.relevant_terms(t):
                collected.setdefault(surface.lower(), zh)
                if len(collected) >= limit:
                    break
        if not collected:
            return ""
        lines = [f"- {en} → {zh}" for en, zh in collected.items()]
        return "术语对照表（翻译时必须严格遵守以下译法）：\n" + "\n".join(lines)

    def merged_with(self, extra: Dict[str, str]) -> "Glossary":
        """并入一批术语，返回新实例。**静态库优先**——手工维护的译法是权威，
        自动抽取的只补它没覆盖到的部分。"""
        if not extra:
            return self
        merged = {k.lower(): v for k, v in extra.items() if k and v}
        merged.update(self.entries)
        return Glossary(merged)

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# 全文术语自动抽取（对齐 BabelDOC 的 auto-extract glossary）
# ---------------------------------------------------------------------------
# 为什么需要：静态 CSV 只有 183 条通用计算机术语，而每篇论文都有自己的一套
# 核心说法（"productive failure""perceived competence"）。它们在文中反复出现，
# 却是**逐批**送给模型翻的——同一个词这批译成"生成性失败"、下一批译成"有效
# 失败"，读者看到的是同一篇文章里三种叫法。BabelDOC 论文把 terminology
# consistency 单列为一项指标，人工评分 4.47 对 PDFMathTranslate 的 3.34，
# 差距就出在这里。
#
# 做法分两步，抽取这一步**纯本地、不花一个 token**：
#   ① 缩写定义式 "productive failure (PF)" —— 论文里最可靠的关键术语信号，
#      且能顺带确认首字母，几乎没有误报；
#   ② 高频实词 n-gram（2~3 词）—— 覆盖那些没给缩写的核心说法。
# 抽出来的候选再交给 translator 一次性译出（见 BaseTranslator.translate_terms），
# 那一步才花 token，且结果进持久缓存，重跑同一篇不再计费。

# 英文功能词：不能出现在术语的首尾（"of the model" 显然不是术语）
_STOP = frozenset("""
a an the this that these those and or but if then than so because as of in on at
to for from by with without within into onto over under between among during
is are was were be been being do does did have has had will would can could may
might must shall should we they it its our their his her he she you i not no
such other others more most less least many much few several various different
we our us also however thus therefore moreover furthermore based using used use
show shows shown found find figure table section results result method methods
study studies data paper work approach proposed present presented new both each
per via when where which who whom while all any some one two three first second
""".split())

_ACRO_DEF = re.compile(
    r"\b([A-Za-z][A-Za-z\-]*(?:\s+[A-Za-z][A-Za-z\-]*){0,4})\s*\(\s*([A-Z][A-Za-z0-9]{1,6})s?\s*\)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']+")
# n-gram 只统计"正文长块"：页眉页脚/页码/图号都是短块，逐页重复会冲进高频榜
_BODY_MIN_CHARS = 80
# 断词残片：连字符后只剩一两个字母（"problem-s"），真实英文词不长这样
_FRAGMENT_RE = re.compile(r"-[A-Za-z]{1,2}$")


def _acronym_terms(text: str) -> List[str]:
    """抽 "全称 (缩写)" 里的全称。用首字母核对，避开 "(see Fig. 1)" 这类。"""
    out = []
    for phrase, acro in _ACRO_DEF.findall(text):
        words = phrase.split()
        k = len(acro)
        if not 1 < k <= len(words):
            continue
        tail = words[-k:]
        if [w[0].lower() for w in tail] != [c.lower() for c in acro]:
            continue                      # 首字母对不上 → 不是缩写定义
        term = " ".join(tail).lower().strip("-")
        if len(term) >= 6 and not any(w in _STOP for w in term.split()):
            out.append(term)
    return out


def _normalize(text: str) -> str:
    """抽取前的清洗：把 PDF 断词留下的 "problem- solving" 合回一个词。

    正文里 "-" 后面紧跟空格几乎只有两种来源：断词换行，或破折号（前面也会有
    空格）。只合并"前面不是空格"的那种，破折号不受影响。
    """
    return re.sub(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])", "-", text)


def _ngram_terms(text: str, min_count: int) -> Dict[str, int]:
    """高频实词 2~3 元组。首尾不许是功能词，整体不许全是功能词。"""
    words = [w.lower() for w in _WORD_RE.findall(text)]
    counts: Dict[str, int] = {}
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in _STOP or gram[-1] in _STOP:
                continue
            if any(len(w) < 3 for w in gram):
                continue
            # "problem-s"：PDF 把 "problem-solving" 断在词中间又漏了空格，
            # 留下"连字符 + 单个字母"结尾的残片。真实英文词不长这样。
            if any(_FRAGMENT_RE.search(w) for w in gram):
                continue
            key = " ".join(gram)
            counts[key] = counts.get(key, 0) + 1
    return {k: c for k, c in counts.items() if c >= min_count}


def auto_extract_terms(texts: List[str], limit: int = 30,
                       min_count: int = 4,
                       known: Dict[str, str] | None = None) -> List[str]:
    """从全文抽出"本文关键术语"候选列表。纯本地，不联网、不计费。

    known：已有静态术语库（小写英文 → 中文），命中的直接跳过——它已经有权威
    译法了，再送去翻译纯属浪费。
    """
    if not texts:
        return []
    known = {k.lower() for k in (known or {})}
    blob = _normalize("\n".join(texts))
    # n-gram 只看**正文长块**：页眉页脚（"Sci. Robot. 10, eadu5257 (2025)"）每页
    # 重复一次，十几页下来轻松冲进高频榜，抽出 "sci robot eadu" 这种垃圾。
    # 它们天然是短块，按长度筛掉最省事；缩写定义式不受影响，仍扫全文。
    body = _normalize("\n".join(t for t in texts if len(t) >= _BODY_MIN_CHARS))
    scored: Dict[str, float] = {}
    # 缩写定义式权重最高：论文作者亲手点名的关键概念
    for t in _acronym_terms(blob):
        scored[t] = scored.get(t, 0.0) + 100.0
    for gram, c in _ngram_terms(body, min_count).items():
        # 词数多 + 出现多 = 更像术语；短的高频组合多半是普通搭配
        scored[gram] = scored.get(gram, 0.0) + c * (1 + 0.5 * gram.count(" "))
    # 去掉被更高分的长术语完全包含的短术语（"failure theory" ⊂ "productive failure theory"）
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    picked: List[str] = []
    for term, _ in ranked:
        if term in known:
            continue
        if any(term in longer and term != longer for longer in picked):
            continue
        picked.append(term)
        if len(picked) >= limit:
            break
    return picked
