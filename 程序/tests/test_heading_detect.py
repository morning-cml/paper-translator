"""标题识别：字体名判粗 + 章节编号断块。

这批用例是**纯函数**级的，不依赖任何 PDF——真实论文回归（test_parser_real.py）
挂在一份不入库的论文上，文件一旦不在工作区就整批静默跳过；标题识别是核心
不变量，必须有一份在 CI 里必然执行的守卫。

背景（2026-07-31 实测）：ACM acmart 模板（CHI / CSCW / UIST 等大量会议在用）
的粗体字体名是 `LinLibertineTB`，旧正则 `bold|black|heavy|semibold|-b\b|-bd\b`
一个都匹配不上 → 全文零个粗体块 → 章节标题不与正文断开、被当成正文一起翻译，
输出里整页只剩一个字号，标题层级彻底消失。
"""
import pytest

from src.pdf_parser import _HEADING_NUM, _word_bold


def _w(fontname: str) -> dict:
    return {"fontname": fontname, "text": "X"}


# --- 应判为粗体 ---------------------------------------------------------------
@pytest.mark.parametrize("fontname", [
    "LinLibertineTB",            # ACM acmart 正文粗体（本次的起因）
    "LinBiolinumTB",             # acmart 标题粗体
    "LinLibertineTBI",           # 粗斜体
    "ABCDEF+LinLibertineTB",     # 带子集前缀
    "Times-Bold",
    "TimesNewRomanPS-BoldMT",
    "Arial-BoldMT",
    "MinionPro-Semibold",
    "SourceSansPro-Black",
    "Helvetica-Heavy",
    "CMBX10",                    # Computer Modern Bold Extended（TeX 默认粗体）
    "CMBX12",
    "SFBX1200",
    "NimbusRomNo9L-Medi",        # TeX 里 Medium 即正文粗体
    "Foo-B",
    "Foo-Bd",
])
def test_bold_fonts_detected(fontname):
    assert _word_bold(_w(fontname)), f"{fontname} 应判为粗体"


# --- 不应判为粗体 -------------------------------------------------------------
@pytest.mark.parametrize("fontname", [
    "LinLibertineT",             # 正文常规——与 …TB 只差一个字母，最易误判
    "LinLibertineTI",            # 斜体，不是粗体
    "LinBiolinumT",
    "Times-Roman",
    "Helvetica",
    "CMR10",                     # Computer Modern Roman（常规）
    "ABCDEF+LinLibertineT",
    "",
])
def test_regular_fonts_not_bold(fontname):
    assert not _word_bold(_w(fontname)), f"{fontname} 不应判为粗体"


def test_lowercase_tb_is_not_bold():
    """后缀规则必须大小写敏感：小写 tb 可能只是普通字体名里的巧合。"""
    assert not _word_bold(_w("Whitby"))
    assert not _word_bold(_w("Montbeliard"))


def test_missing_fontname_is_safe():
    assert not _word_bold({})


# --- 章节编号（用于把同字号的相邻两级标题断开）--------------------------------
@pytest.mark.parametrize("text", [
    "2 RELATED WORK",
    "2.1 HCI Research for Music Therapy",
    "3.2.1 Interview Procedure",
    "  4 Method",
    "IV. Discussion",
    "A. Setup",
])
def test_heading_numbers_matched(text):
    assert _HEADING_NUM.match(text), f"{text!r} 应识别为带编号的标题行"


@pytest.mark.parametrize("text", [
    "The research on music therapy within the HCI community",
    "2020 was a turning point for the field",   # 以年份开头的正文
    "3.14159 is pi",                            # 小数开头
    "",
    "2",                                        # 只有编号、没有标题文字
    "2.",
])
def test_non_headings_not_matched(text):
    assert not _HEADING_NUM.match(text), f"{text!r} 不应识别为标题行"
