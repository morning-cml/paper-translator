"""Markdown / TXT / SRT 翻译（A4）。

核心不变量：**结构与不可译片段必须逐字幸存**——代码块、链接地址、时间轴、
序号一旦被改动，文件就废了。
"""
import re

import pytest

from src.config import load_config
from src.pipeline import translate_document
from src.text_translator import (_parse_markdown, _parse_srt, _parse_txt,
                                 _protect)

MD = """---
title: Sample Paper
---

# Robot Failure Study

We compared three instructional methods across classrooms.
The robot failure condition produced the largest gain.

## Methods

- Random assignment to conditions
- Pretest and posttest measures

See [the project page](https://example.com/robots) and `run_study.py` for details.

```python
def train(model):
    return model.fit()  # this must never be translated
```

| Condition | Outcome |
|-----------|---------|
| Robot failure | High improvement |

> Observing failure helps learning.

Inline math $E = mc^2$ stays intact.
"""

SRT = """1
00:00:01,000 --> 00:00:04,000
Welcome to the lecture on robot learning.

2
00:00:04,500 --> 00:00:08,000
Today we discuss productive failure.
"""

TXT = """Introduction to the Study

We evaluated three conditions with 135 students.
The results were significant.

Visit https://example.com/data for the dataset.
"""


def _mock(tmp_path, name, content, **cfg_kw):
    src = tmp_path / name
    src.write_text(content, encoding="utf-8")
    out = tmp_path / ("out" + src.suffix)
    res = translate_document(str(src), str(out), load_config(**cfg_kw), mock=True)
    return out.read_text(encoding="utf-8"), res


def _cjk(s):
    return any("一" <= c <= "鿿" for c in s)


# ---------------- 行内保护 ----------------
@pytest.mark.parametrize("text,keep", [
    ("see `code_here()` now", "`code_here()`"),
    ("link [x](https://a.b/c) here", "](https://a.b/c)"),
    ("bare https://a.b/c?d=1 url", "https://a.b/c?d=1"),
    ("math $x^2 + y$ inline", "$x^2 + y$"),
    ("tag <br/> here", "<br/>"),
    ("note [^1] here", "[^1]"),
])
def test_protect_masks_and_restores(text, keep):
    masked, holes = _protect(text)
    assert keep not in masked, "不可译片段应被替换为占位符"
    assert "⟦F" in masked
    restored = masked
    for i, orig in holes.items():
        restored = restored.replace(f"⟦F{i}⟧", orig)
    assert restored == text, "还原必须无损"


@pytest.mark.parametrize("text", [
    "see [the dataset](https://example.com/data) now",
    "an ![image alt](img/a.png) here",
    "a [ref style][key] link",
    'titled [x](https://a.b "Title") end',
])
def test_link_brackets_both_sides_protected(text):
    """左右方括号都必须进占位符。

    只保护右半 "](url)" 时，模型会把落单的 "[" 删掉，产出
    "数据集](url)" 这种坏链接——真实翻译中实际发生过，此处锁死。
    """
    masked, holes = _protect(text)
    assert "[" not in masked and "]" not in masked, \
        f"结构方括号未被保护：{masked!r}"
    # 显示文字仍应可译（未被整体吞进占位符）
    assert any(c.isalpha() for c in re.sub(r"⟦F\d+⟧", "", masked))
    restored = masked
    for i, orig in holes.items():
        restored = restored.replace(f"⟦F{i}⟧", orig)
    assert restored == text


def test_link_survives_translation_dropping_brackets(tmp_path):
    """模型只译显示文字、占位符原样返回时，链接必须完整重建。"""
    from src.text_translator import Seg, _mk_text_seg
    seg = _mk_text_seg("see [the dataset](https://example.com/data) now")
    assert seg.kind == "text"
    # 模拟译文：显示文字换成中文，占位符保留
    seg.translation = seg.body.replace("the dataset", "数据集")
    out = seg.render()
    assert "[数据集](https://example.com/data)" in out


# ---------------- Markdown ----------------
def test_markdown_code_block_untouched(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    assert "def train(model):" in out
    assert "return model.fit()  # this must never be translated" in out
    assert "```python" in out


def test_markdown_structure_preserved(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    for marker in ("---\ntitle: Sample Paper", "# ", "## ", "- ", "> ",
                   "|-----------|"):
        assert marker in out, f"结构标记 {marker!r} 丢失"


def test_markdown_link_target_and_inline_code_survive(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    assert "https://example.com/robots" in out
    assert "`run_study.py`" in out
    assert "$E = mc^2$" in out


def test_markdown_prose_translated(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    body = [l for l in out.split("\n")
            if l.startswith("# ") or l.startswith("## ")]
    assert body and all(_cjk(l) for l in body), "标题应被翻译"
    assert _cjk(out.split("```")[0]), "正文应被翻译"


def test_markdown_front_matter_verbatim(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    assert out.startswith("---\ntitle: Sample Paper\n---\n"), "front matter 不得改动"


def test_markdown_table_cells_translated(tmp_path):
    out, _ = _mock(tmp_path, "a.md", MD)
    row = [l for l in out.split("\n") if l.startswith("|") and "---" not in l]
    assert len(row) >= 2
    assert all(l.count("|") >= 3 for l in row), "表格竖线结构应保持"
    assert _cjk("".join(row)), "表格单元格应被翻译"


def test_markdown_paragraph_merged_into_one_unit(tmp_path):
    """连续正文行应合并成一个翻译单元（整段翻译质量更好）。"""
    segs = _parse_markdown("Line one continues\nline two of same paragraph.\n")
    texts = [s for s in segs if s.kind == "text"]
    assert len(texts) == 1
    assert "Line one continues line two" in texts[0].body


# ---------------- SRT ----------------
def test_srt_timestamps_and_indices_intact(tmp_path):
    out, _ = _mock(tmp_path, "a.srt", SRT)
    assert "00:00:01,000 --> 00:00:04,000" in out
    assert "00:00:04,500 --> 00:00:08,000" in out
    lines = [l.strip() for l in out.split("\n")]
    assert "1" in lines and "2" in lines, "字幕序号应保留"


def test_srt_text_translated(tmp_path):
    out, _ = _mock(tmp_path, "a.srt", SRT)
    assert _cjk(out)
    assert "-->" in out


def test_srt_parse_structure():
    segs = _parse_srt(SRT)
    assert sum(1 for s in segs if s.kind == "text") == 2, "两条字幕两个单元"


# ---------------- TXT ----------------
def test_txt_translated_and_blank_lines_kept(tmp_path):
    out, _ = _mock(tmp_path, "a.txt", TXT)
    assert _cjk(out)
    assert "\n\n" in out, "空行结构应保留"
    assert "https://example.com/data" in out, "URL 不得翻译"


def test_txt_paragraph_grouping():
    segs = _parse_txt("a one\nb two\n\nc three\n")
    assert sum(1 for s in segs if s.kind == "text") == 2


# ---------------- 通用 ----------------
def test_srt_roundtrip_byte_identical():
    """SRT 每行都有意义，不做段落合并 → 必须逐字节还原。"""
    assert "".join(s.render() for s in _parse_srt(SRT)) == SRT


@pytest.mark.parametrize("name,content", [("a.md", MD), ("a.txt", TXT)])
def test_roundtrip_loses_nothing_but_soft_wraps(name, content):
    """MD/TXT 会把连续正文行合并成一段（有意为之：整段翻译质量更好），
    故不要求逐字节相同，但**内容零丢失、结构标记完整**。"""
    from pathlib import Path

    from src.text_translator import PARSERS
    out = "".join(s.render() for s in PARSERS[Path(name).suffix](content))
    assert out.split() == content.split(), "除软换行外不得增删任何内容"
    for line in content.split("\n"):
        s = line.strip()
        if s.startswith(("#", "-", ">", "|", "```", "---")):
            assert s in out, f"结构行 {s!r} 丢失"


def test_bilingual_keeps_original(tmp_path):
    out, _ = _mock(tmp_path, "a.txt", TXT, output_mode="bilingual")
    assert "The results were significant." in out, "双语模式应保留原文"
    assert _cjk(out)


def test_backend_name_reported(tmp_path):
    _, res = _mock(tmp_path, "a.md", MD)
    assert res["backend"] == "md" and res["blocks"] > 3


def test_already_chinese_not_retranslated(tmp_path):
    out, res = _mock(tmp_path, "a.txt", "这已经是中文了，不需要翻译。\n")
    assert res["blocks"] == 0
    assert out.strip() == "这已经是中文了，不需要翻译。"


# ---------------- 行尾风格 ----------------
def _mock_bytes(tmp_path, name, raw: bytes) -> bytes:
    """按**字节**写入与读回：行尾必须逐字节校验，text 模式会自动改写而看不见。"""
    src = tmp_path / name
    src.write_bytes(raw)
    out = tmp_path / ("out" + src.suffix)
    translate_document(str(src), str(out), load_config(), mock=True)
    return out.read_bytes()


@pytest.mark.parametrize("name,raw", [
    ("a.md", b"# Title\r\nsome body text goes here\r\n\r\n- item one\r\n"),
    ("a.txt", b"first paragraph text\r\n\r\nsecond paragraph text\r\n"),
    ("a.srt", b"1\r\n00:00:01,000 --> 00:00:02,000\r\nHello world\r\n\r\n"),
])
def test_crlf_source_keeps_crlf_everywhere(tmp_path, name, raw):
    """CRLF 源文件不得译成**混合行尾**。

    回归 2026-09-07：解析器按 "\n" 切行，行尾那个 \r 落在行内、被 strip()
    吃掉；原样输出的结构行仍是 \r\n，经解析重组的译文行却只剩 \n，同一个
    文件里两种行尾混着。Windows 是本工具的主力平台，.srt/.txt/.md 常态就是
    CRLF，而 SRT 播放器与老编辑器对混合行尾并不宽容。
    """
    got = _mock_bytes(tmp_path, name, raw)
    assert got.count(b"\n") == got.count(b"\r\n"), \
        f"出现裸 LF（混合行尾）：{got!r}"


@pytest.mark.parametrize("name,raw", [
    ("a.md", b"# Title\nplain LF body line\n"),
    ("a.txt", b"first paragraph\n\nsecond paragraph\n"),
])
def test_lf_source_stays_lf(tmp_path, name, raw):
    """反向也要守住：LF 源文件不能被"顺手"改成 CRLF。"""
    assert b"\r" not in _mock_bytes(tmp_path, name, raw)
