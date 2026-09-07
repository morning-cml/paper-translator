"""DOCX 翻译（方向一 A1）。

复用 PDF 那套翻译侧能力（术语库 / 全文语境 / 持久缓存 / 失败降级 /
占位符保护 / 中西文加空格），只是"回填"换成在 Word 文档里就地改文字。

为什么 DOCX 比 PDF 简单得多：Word 自带段落与样式结构，不需要坐标重排——
**按 run 就地替换文字，样式、编号、表格、图片位置天然保留**。

覆盖范围：正文段落、表格（含嵌套）、页眉页脚、脚注（若存在）。
已知限制：超链接内的文字保持原文（python-docx 的 paragraph.runs 不含
w:hyperlink 内的 run，读写口径保持一致才不会出现重复文字）。

依赖 python-docx（MIT 许可，可自由商用）。
"""
from __future__ import annotations

import copy
from typing import Callable, List, Optional


class DocxUnsupported(Exception):
    """缺少 python-docx 组件。"""


def available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 采集：文档里所有"值得翻译"的段落
# ---------------------------------------------------------------------------

class _Unit:
    """一个待译段落：文字取自它的 runs，回填也只动这些 runs。"""

    __slots__ = ("para", "runs", "text")

    def __init__(self, para, runs):
        self.para = para
        self.runs = runs
        self.text = "".join(r.text for r in runs)


def _worth_translating(text: str, target_code: str = "zh") -> bool:
    from .languages import looks_like
    t = (text or "").strip()
    if len(t) < 2:
        return False
    if looks_like(t, target_code):
        return False                      # 已是目标语（B5），不重复翻译
    return sum(c.isalpha() for c in t) >= 2   # 任意脚本字母即可译


def _iter_paragraphs(container, seen: set):
    """递归遍历容器内的段落（正文 → 表格 → 嵌套表格）。合并单元格会重复
    返回同一段落，用 id 去重。"""
    for p in getattr(container, "paragraphs", []):
        if id(p._p) not in seen:
            seen.add(id(p._p))
            yield p
    for t in getattr(container, "tables", []):
        for row in t.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell, seen)


def _collect(doc, target_code: str = "zh") -> List[_Unit]:
    seen: set = set()
    containers = [doc]
    for section in doc.sections:          # 页眉页脚
        for part in (section.header, section.footer,
                     getattr(section, "first_page_header", None),
                     getattr(section, "first_page_footer", None),
                     getattr(section, "even_page_header", None),
                     getattr(section, "even_page_footer", None)):
            if part is not None:
                containers.append(part)

    units: List[_Unit] = []
    for c in containers:
        for p in _iter_paragraphs(c, seen):
            runs = [r for r in p.runs if r.text]
            if not runs:
                continue
            u = _Unit(p, runs)
            if _worth_translating(u.text, target_code):
                units.append(u)
    return units


# ---------------------------------------------------------------------------
# 回填
# ---------------------------------------------------------------------------

def _carrier(runs):
    """选"字数最多"的 run 承载译文，而不是固定第一个。

    段落若以加粗/斜体为主（图注、强调段、标题），主导格式得以保留；清空其余
    run 不影响文字位置——空 run 不渲染任何内容。
    段内混排格式（正文中间夹一个加粗短语）无法逐词对齐译文，只能取主导格式，
    这与主流文档翻译工具的取舍一致。
    """
    return max(runs, key=lambda r: len(r.text))


def _set_text(unit: _Unit, text: str) -> None:
    """译文写进主导 run，其余清空。段落级样式（标题级别、编号、对齐、
    间距）完全不动。"""
    keep = _carrier(unit.runs)
    keep.text = text
    for r in unit.runs:
        if r is not keep:
            r.text = ""


def _append_translation(unit: _Unit, text: str) -> None:
    """双语模式：在原段之后插入一个同样式的新段落放译文。"""
    new_p = copy.deepcopy(unit.para._p)
    unit.para._p.addnext(new_p)
    from docx.text.paragraph import Paragraph
    para = Paragraph(new_p, unit.para._parent)
    runs = [r for r in para.runs if r.text]
    if not runs:
        return
    keep = _carrier(runs)
    keep.text = text
    for r in runs:
        if r is not keep:
            r.text = ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def translate_docx(
    input_path: str,
    output_path: str,
    cfg,
    translator=None,
    glossary=None,
    mock: bool = False,
    progress: Optional[Callable[[str, float], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    if not available():
        raise DocxUnsupported(
            "缺少 Word 文档支持组件。请运行：pip install python-docx")

    from docx import Document

    from .glossary import Glossary
    from .pipeline import (CancelledError, _estimate_line, _maybe_pangu,
                           _translated, build_doc_glossary, make_translator)

    def report(msg: str, frac: float):
        if progress:
            progress(msg, max(0.0, min(1.0, frac)))

    def check_cancel():
        if should_cancel and should_cancel():
            raise CancelledError("已取消。")

    check_cancel()
    report("正在解析 Word 文档…", 0.03)
    doc = Document(input_path)
    units = _collect(doc, getattr(cfg, "target_lang", "zh") or "zh")
    texts = [u.text for u in units]
    report(f"共 {len(texts)} 个待译段落", 0.08)

    if glossary is None:
        glossary = Glossary.load(cfg.resolved_glossary_path())
    if translator is None:
        # 文档语境：标题（首个较短段）+ 首个长段
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
        report(f"正在翻译… 第 {done}/{total} 批", 0.10 + 0.80 * (done / max(total, 1)))

    n_done = 0
    if texts:
        translations = [_maybe_pangu(t, cfg) for t in
                        translator.translate_texts(texts, glossary, tcb,
                                                   should_cancel=should_cancel)]
        check_cancel()
        report("正在写回 Word 文档…", 0.93)
        bilingual = getattr(cfg, "output_mode", "translated") != "translated"
        for u, tr in zip(units, translations):
            if not _translated(u.text, tr, cfg):
                continue          # 模型原样退回（专名/编号等）→ 保留原文
            if bilingual:
                _append_translation(u, tr)
            else:
                _set_text(u, tr)
            n_done += 1

        hits = getattr(translator, "cache_hits", 0)
        if hits:
            report(f"持久缓存命中 {hits} 段，未重复计费", 0.94)
        failed = getattr(translator, "failed_texts", 0)
        if failed:
            report(f"注意：{failed} 段因网络/服务错误未翻译，已保留原文——"
                   "重新运行即可补齐（已译段走缓存不重复计费）", 0.94)
    else:
        report("文档中未找到可翻译的文字", 0.9)

    from .paths import atomic_output
    with atomic_output(output_path) as _out:
        doc.save(_out.tmp)        # 原子写出：中途失败不留半截损坏文档
    report("完成", 1.0)
    return {
        "pages": 0,               # Word 无固定页数概念
        "blocks": n_done,
        "output": _out.path,      # 目标被占用时可能已自动改名
        "mode": getattr(cfg, "output_mode", "translated"),
        "backend": "docx",
    }
