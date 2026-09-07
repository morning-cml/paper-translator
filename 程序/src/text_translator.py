"""纯文本类文档翻译（方向一 A4）：Markdown / TXT / SRT。

设计要点——**只翻该翻的，结构一字不动**：

| 格式 | 保留原样 | 翻译 |
|---|---|---|
| Markdown | 代码块/行内代码/链接地址/图片路径/数学公式/HTML 标签/front matter | 正文、标题、列表项、引用、表格单元格、链接显示文字 |
| TXT | 空行结构、URL | 段落文字 |
| SRT | 序号、时间轴 | 字幕文字 |

行内不可译片段（代码、URL、公式…）一律替换成 **⟦Fn⟧ 占位符**——直接复用
PDF 那套公式占位符机制，于是翻译端既有的"提示词保护 + 译后校验 + 强化重试"
自动生效，无需新增任何保护逻辑。

段落处理：Markdown/TXT 把连续正文行合并为一个翻译单元（整段翻译质量远好于
逐行），输出时合并为一行——Markdown 的软换行语义不变。
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PLACEHOLDER_FMT = "⟦F{}⟧"

# 链接/图片：**左右两侧的结构符都要保护，中间的显示文字留给翻译**。
# 只保护右半 "](url)" 是不够的——模型会把孤零零的 "[" 当噪声删掉，
# 产出 "数据集](url)" 这种坏链接（实测踩过）。
_LINK_RE = re.compile(
    r"(!?\[)"                                   # 左：[ 或 ![
    r"([^\]\n]*?)"                              # 中：显示文字（可译）
    r"(\](?:\([^)\s]*(?:\s+\"[^\"]*\")?\)|\[[^\]\n]*\]|))")  # 右：](url) / [ref] / 单独 ]

# ---- 其余行内需保护的片段（按优先级从长到短匹配）----
_INLINE_PATTERNS = [
    re.compile(r"\$\$.+?\$\$", re.S),        # 块级数学
    re.compile(r"`[^`\n]+`"),                # 行内代码
    re.compile(r"<[^>\n]{1,120}>"),          # HTML 标签 / 自动链接
    re.compile(r"https?://\S+"),             # 裸 URL
    re.compile(r"\$[^$\n]{1,80}\$"),         # 行内数学
    re.compile(r"\{[#.][^}\n]*\}"),          # 属性块 {#id .class}
]

# ---- 结构前缀（保留，其后内容可译）----
_PREFIX_RE = re.compile(
    r"^(\s*(?:"
    r"#{1,6}\s+"                 # 标题
    r"|>[ \t]?"                  # 引用
    r"|[-*+]\s+(?:\[[ xX]\]\s+)?"  # 无序列表（含任务列表）
    r"|\d+[.)]\s+"               # 有序列表
    r"))(.*)$")

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+")
_ATX_TRAIL_RE = re.compile(r"\s+#+\s*$")
_SRT_TIME_RE = re.compile(r"^\s*[\d:,.]+\s*-->\s*[\d:,.]+")


@dataclass
class Seg:
    """一个输出片段：raw 原样输出；text 需翻译。"""
    kind: str                 # "raw" | "text"
    body: str                 # raw: 原样内容；text: 待译内容（已插占位符）
    prefix: str = ""          # 结构前缀（如 "## "、"- "），原样保留
    suffix: str = ""          # 结构后缀（如表格的 " |"）
    holes: Dict[int, str] = field(default_factory=dict)   # 占位符还原表
    translation: Optional[str] = None

    def render(self) -> str:
        if self.kind == "raw":
            return self.body
        out = self.translation if self.translation else self.body
        for idx, original in self.holes.items():
            out = out.replace(PLACEHOLDER_FMT.format(idx), original)
        return self.prefix + out + self.suffix


def _protect(text: str) -> Tuple[str, Dict[int, str]]:
    """把行内不可译片段换成 ⟦Fn⟧，返回 (masked, 还原表)。

    脚注 `[^1]` 因左括号后紧跟 `^`，会被链接规则整体收进左侧占位符，
    结构同样不受损。
    """
    holes: Dict[int, str] = {}
    n = 0

    def take(s: str) -> str:
        nonlocal n
        n += 1
        holes[n] = s
        return PLACEHOLDER_FMT.format(n)

    # 1) 链接/图片：左右结构符各占一个占位符，中间显示文字保持可译
    def link_repl(m):
        open_, label, close = m.group(1), m.group(2), m.group(3)
        if label.startswith("^"):            # 脚注 [^1]：整体保护
            return take(m.group(0))
        return take(open_) + label + take(close)

    text = _LINK_RE.sub(link_repl, text)

    # 2) 其余片段整体保护
    for pat in _INLINE_PATTERNS:
        text = pat.sub(lambda m: take(m.group(0)), text)
    return text, holes


# 目标语上下文：翻译时由 translate_text_file 设定，供解析期判"是否已是目标语"
_target_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "target_code", default="zh")


def _worth_translating(text: str) -> bool:
    from .languages import looks_like
    t = re.sub(r"⟦F\d+⟧", "", text or "").strip()
    if len(t) < 2:
        return False
    if looks_like(t, _target_ctx.get()):
        return False                       # 已是目标语（B5：按目标语判定，非写死中文）
    return sum(c.isalpha() for c in t) >= 2   # 含任意脚本字母即可译（源可为中/日/俄…）


def _split_lines(text: str) -> Tuple[List[str], bool]:
    """按行切分，并剥掉"末尾换行"产生的伪空行。

    `"a\\n".split("\\n")` == `["a", ""]`，那个空串不是真实的一行——直接当行
    处理会给每个文件平白多出一个换行（往返测试即因此暴露过该 bug）。
    """
    trailing = text.endswith("\n")
    lines = text.split("\n")
    if trailing:
        lines = lines[:-1]
    return lines, trailing


def _finalize(segs: List[Seg], trailing_nl: bool) -> List[Seg]:
    """逐行渲染时每行都带了 \\n；若原文末尾本无换行则去掉最后那个。"""
    if not trailing_nl and segs:
        last = segs[-1]
        if last.kind == "raw" and last.body.endswith("\n"):
            last.body = last.body[:-1]
    return segs


def _mk_text_seg(content: str, prefix: str = "", suffix: str = "") -> Seg:
    masked, holes = _protect(content)
    kind = "text" if _worth_translating(masked) else "raw"
    if kind == "raw":
        return Seg("raw", prefix + content + suffix)
    return Seg("text", masked, prefix=prefix, suffix=suffix, holes=holes)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _parse_markdown(text: str) -> List[Seg]:
    lines, trailing_nl = _split_lines(text)
    segs: List[Seg] = []
    buf: List[str] = []          # 待合并的正文行
    i, n = 0, len(lines)
    in_fence = False
    fence_mark = ""

    def flush():
        if buf:
            segs.append(_mk_text_seg(" ".join(s.strip() for s in buf)))
            segs.append(Seg("raw", "\n"))
            buf.clear()

    # YAML front matter
    if lines and lines[0].strip() in ("---", "+++"):
        mark = lines[0].strip()
        for j in range(1, n):
            if lines[j].strip() == mark:
                segs.append(Seg("raw", "\n".join(lines[:j + 1]) + "\n"))
                i = j + 1
                break

    while i < n:
        line = lines[i]
        stripped = line.strip()

        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            if not in_fence:
                in_fence, fence_mark = True, fence.group(1)
            elif stripped.startswith(fence_mark):
                in_fence = False
            segs.append(Seg("raw", line + "\n"))
            i += 1
            continue
        if in_fence:
            segs.append(Seg("raw", line + "\n"))
            i += 1
            continue

        # 空行 / 分隔线 / 缩进代码 / 引用定义 / 表格分隔行 → 原样
        if (not stripped or _TABLE_SEP_RE.match(line) or _REF_DEF_RE.match(line)
                or re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line)
                or (line.startswith(("    ", "\t")) and not buf)):
            flush()
            segs.append(Seg("raw", line + "\n"))
            i += 1
            continue

        # 表格数据行：逐单元格翻译，竖线结构原样
        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush()
            parts = line.split("|")
            for k, cell in enumerate(parts):
                if k:
                    segs.append(Seg("raw", "|"))
                if cell.strip():
                    lead = cell[:len(cell) - len(cell.lstrip())]
                    trail = cell[len(cell.rstrip()):]
                    segs.append(_mk_text_seg(cell.strip(), prefix=lead,
                                             suffix=trail))
                else:
                    segs.append(Seg("raw", cell))
            segs.append(Seg("raw", "\n"))
            i += 1
            continue

        m = _PREFIX_RE.match(line)
        if m:
            flush()
            prefix, content = m.group(1), m.group(2)
            trail = ""
            t = _ATX_TRAIL_RE.search(content)   # 闭合式标题 "## 标题 ##"
            if t and prefix.lstrip().startswith("#"):
                trail, content = t.group(0), content[:t.start()]
            segs.append(_mk_text_seg(content, prefix=prefix, suffix=trail))
            segs.append(Seg("raw", "\n"))
            i += 1
            continue

        buf.append(line)          # 普通正文行，累积成段
        i += 1

    flush()
    return _finalize(segs, trailing_nl)


# ---------------------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------------------

def _parse_txt(text: str) -> List[Seg]:
    segs: List[Seg] = []
    buf: List[str] = []

    def flush():
        if buf:
            segs.append(_mk_text_seg(" ".join(s.strip() for s in buf)))
            segs.append(Seg("raw", "\n"))
            buf.clear()

    lines, trailing_nl = _split_lines(text)
    for line in lines:
        if line.strip():
            buf.append(line)
        else:
            flush()
            segs.append(Seg("raw", line + "\n"))
    flush()
    return _finalize(segs, trailing_nl)


# ---------------------------------------------------------------------------
# SRT 字幕
# ---------------------------------------------------------------------------

def _parse_srt(text: str) -> List[Seg]:
    """只翻字幕文字；序号与时间轴原样保留。"""
    segs: List[Seg] = []
    buf: List[str] = []

    def flush():
        if buf:
            segs.append(_mk_text_seg(" ".join(s.strip() for s in buf)))
            segs.append(Seg("raw", "\n"))
            buf.clear()

    lines, trailing_nl = _split_lines(text)
    for line in lines:
        stripped = line.strip()
        if not stripped or _SRT_TIME_RE.match(line) or stripped.isdigit():
            flush()
            segs.append(Seg("raw", line + "\n"))
        else:
            buf.append(line)
    flush()
    return _finalize(segs, trailing_nl)


PARSERS = {".md": _parse_markdown, ".markdown": _parse_markdown,
           ".txt": _parse_txt, ".srt": _parse_srt}


def _read(path: str) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def translate_text_file(
    input_path: str,
    output_path: str,
    cfg,
    translator=None,
    glossary=None,
    mock: bool = False,
    progress: Optional[Callable[[str, float], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    from .glossary import Glossary
    from .pipeline import (CancelledError, _estimate_line, _maybe_pangu,
                           _translated, build_doc_glossary, make_translator)

    def report(msg: str, frac: float):
        if progress:
            progress(msg, max(0.0, min(1.0, frac)))

    def check_cancel():
        if should_cancel and should_cancel():
            raise CancelledError("已取消。")

    ext = Path(input_path).suffix.lower()
    parser = PARSERS.get(ext, _parse_txt)

    check_cancel()
    report("正在解析文档…", 0.03)
    _target_ctx.set(getattr(cfg, "target_lang", "zh") or "zh")  # 供解析期判目标语
    text = _read(input_path)
    # 行尾风格随源文件：解析器内部一律按 LF 走，写回时再还原成原样。不做这一步
    # 的话，CRLF 源文件（Windows 上的 .srt/.txt/.md 常态）译出来是**混合行尾**
    # ——原样输出的结构行仍是 CRLF，而经解析重组的译文行只剩 LF（那个 CR 在
    # strip() 时被吃掉了）。SRT 播放器与老编辑器对此并不宽容。
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    segs = parser(text)
    units = [s for s in segs if s.kind == "text"]
    texts = [s.body for s in units]
    report(f"共 {len(texts)} 个待译片段", 0.08)

    if glossary is None:
        glossary = Glossary.load(cfg.resolved_glossary_path())
    if translator is None:
        head = texts[0][:150] if texts else ""
        body = next((t for t in texts if len(t) > 300), "")
        ctx = head + (("\n摘要节选：" + body[:250]) if body else "")
        translator = make_translator(cfg, mock=mock, doc_context=ctx)
    glossary = build_doc_glossary(translator, texts, glossary, cfg, report)
    if texts and not mock:
        try:
            report(_estimate_line(translator, texts, cfg), 0.09)
        except Exception:  # noqa: BLE001
            pass

    def tcb(done: int, total: int):
        check_cancel()
        report(f"正在翻译… 第 {done}/{total} 批",
               0.10 + 0.80 * (done / max(total, 1)))

    n_done = 0
    if texts:
        results = [_maybe_pangu(t, cfg) for t in
                   translator.translate_texts(texts, glossary, tcb,
                                              should_cancel=should_cancel)]
        check_cancel()
        bilingual = getattr(cfg, "output_mode", "translated") != "translated"
        for seg, tr in zip(units, results):
            if not _translated(seg.body, tr, cfg):
                continue                       # 模型原样退回 → 保留原文
            seg.translation = (seg.body + "\n" + tr) if bilingual else tr
            n_done += 1

        hits = getattr(translator, "cache_hits", 0)
        if hits:
            report(f"持久缓存命中 {hits} 段，未重复计费", 0.94)
        failed = getattr(translator, "failed_texts", 0)
        if failed:
            report(f"注意：{failed} 段因网络/服务错误未翻译，已保留原文——"
                   "重新运行即可补齐", 0.94)
        flags = getattr(translator, "quality_flags", 0)
        if flags:
            report(f"译文自检：{flags} 段发现异常，"
                   f"重译修正 {getattr(translator, 'quality_fixed', 0)} 段", 0.94)
    else:
        report("文档中未找到可翻译的文字", 0.9)

    report("正在写回文档…", 0.95)
    out = "".join(s.render() for s in segs)
    if crlf:
        out = out.replace("\n", "\r\n")   # 还原源文件的行尾风格
    from .paths import atomic_output
    with atomic_output(output_path) as _out:
        Path(_out.tmp).write_text(out, encoding="utf-8", newline="")
    report("完成", 1.0)
    return {"pages": 0, "blocks": n_done, "output": _out.path,
            "mode": getattr(cfg, "output_mode", "translated"),
            "backend": ext.lstrip(".") or "text"}
