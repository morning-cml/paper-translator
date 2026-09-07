"""版面保真量化评测（BIoU / UTB）与公式回贴的文字层洁净度。

这两件事绑在一起测是有原因的：公式回贴一旦把整页文字捎带进 XObject，
BIoU 会被幽灵文字彻底带偏（实测公式最多的那页从 0.44 掉到 0.04），
指标就再也卡不住真正的版面退化了。
"""
import pytest

from src.config import load_config
from src.metrics import (MATCH_THRESHOLD, biou_from_boxes, evaluate,
                         page_boxes, untranslated_blocks)
from src.pipeline import translate_pdf

fitz = pytest.importorskip("fitz")


# ---------------- BIoU 纯函数 ----------------

def _boxes(*rects):
    return {0: [{"box": r, "text": "x" * 20} for r in rects]}


def test_identical_layout_scores_one():
    b = _boxes((0.1, 0.1, 0.5, 0.2), (0.1, 0.3, 0.9, 0.6))
    assert biou_from_boxes(b, b)["biou"] == pytest.approx(1.0)


def test_disjoint_layout_scores_zero():
    a = _boxes((0.0, 0.0, 0.2, 0.2))
    b = _boxes((0.7, 0.7, 0.9, 0.9))
    assert biou_from_boxes(a, b)["biou"] == pytest.approx(0.0)


def test_missing_page_counts_as_zero():
    a = _boxes((0.1, 0.1, 0.5, 0.5))
    assert biou_from_boxes(a, {})["biou"] == pytest.approx(0.0)


def test_partial_overlap_is_between():
    a = _boxes((0.0, 0.0, 0.4, 0.4))
    b = _boxes((0.2, 0.0, 0.6, 0.4))
    r = biou_from_boxes(a, b)
    assert 0.0 < r["biou"] < 1.0
    assert r["matched"] == 0.0, f"IoU 应低于匹配阈值 {MATCH_THRESHOLD}"


def test_utb_counts_only_long_blocks():
    out = {0: [{"box": (0, 0, 1, 1), "text": "这是一段完整的中文译文内容足够长"},
               {"box": (0, 0, 1, 1), "text": "This block was never translated."},
               {"box": (0, 0, 1, 1), "text": "Fig. 1"}]}     # 太短，不计
    r = untranslated_blocks(out, "zh")
    assert r["blocks"] == 2 and r["untranslated"] == 1


# ---------------- 补集分解（公式取景用） ----------------

def test_complement_covers_everything_but_the_kept_rects():
    from src.pdf_writer_fitz import _complement_rects
    page = fitz.Rect(0, 0, 100, 100)
    keep = [(40.0, 40.0, 60.0, 60.0)]
    comp = _complement_rects(page, keep)
    # 补集不得与保护区相交
    k = fitz.Rect(*keep[0])
    assert all(not fitz.Rect(*c).intersects(k) for c in comp)
    # 补集面积 + 保护区面积 = 整页（补集互不重叠，可直接相加）
    area = sum((c[2] - c[0]) * (c[3] - c[1]) for c in comp)
    assert area == pytest.approx(100 * 100 - 20 * 20, abs=1.0)


def test_complement_handles_overlapping_keeps():
    from src.pdf_writer_fitz import _complement_rects
    page = fitz.Rect(0, 0, 100, 100)
    keep = [(10.0, 10.0, 50.0, 50.0), (30.0, 30.0, 70.0, 70.0)]
    comp = _complement_rects(page, keep)
    for k in keep:
        kr = fitz.Rect(*k)
        assert all(not fitz.Rect(*c).intersects(kr) for c in comp)


def test_complement_of_nothing_is_whole_page():
    from src.pdf_writer_fitz import _complement_rects
    page = fitz.Rect(0, 0, 100, 100)
    assert _complement_rects(page, []) == [(0.0, 0.0, 100.0, 100.0)]


# ---------------- 端到端：公式页的文字层不得被污染 ----------------

@pytest.fixture(scope="module")
def formula_output(paper_layouts, paper_path, tmp_path_factory):
    """真实论文（含 38 个行内公式）走一遍 Mock 翻译。"""
    out = tmp_path_factory.mktemp("metrics") / "out.pdf"
    translate_pdf(paper_path, str(out),
                  load_config(render_backend="pymupdf",
                              output_mode="translated"), mock=True)
    return str(out)


def test_formula_pages_do_not_leak_whole_page_text(paper_path, formula_output):
    """回归 2026-09-07：`show_pdf_page(clip=...)` 把**整页**塞进 XObject 再裁切。

    视觉上只露出公式那一小块，但文字层里整页文字都在，且被缩放平移到公式那个
    小框上——一页 N 个行内公式就多 N 份整页副本。实测第 7 页（11 个公式）：
    pdfminer 系提取器读出 13059 个词，原文只有 1171 个，坐标从 -53 铺到 676
    （页宽 594）。MuPDF 系尊重裁切、看不到，所以肉眼无恙；但 pdfminer 系工具
    （含本项目自己的解析器与这套评测）看到的是一片垃圾。
    """
    import pdfplumber
    with pdfplumber.open(paper_path) as src, pdfplumber.open(formula_output) as out:
        for i, (a, b) in enumerate(zip(src.pages, out.pages)):
            n_src, n_out = len(a.extract_words()), len(b.extract_words())
            assert n_out <= 3 * max(n_src, 50), (
                f"第 {i + 1} 页译文词数 {n_out} 远超原文 {n_src}，"
                "十有八九是公式 XObject 把整页文字捎带进来了")


def test_inline_formulas_are_actually_pasted(paper_layouts, formula_output):
    """公式取景源建好后必须**照样贴得出来**——别为了洁净把公式弄没了。

    回归 2026-09-07：取景副本文档若边建边用，PyMuPDF 的 graftmap 会在源文档
    被追加页面后失效并抛 `source object number out of range`，而 `_draw_page`
    里那句 `except Exception: pass` 会把它静静吞掉：实测第 1 页 38 个公式正常，
    第 4/6/7 页的公式**全部无声消失**，页面上只剩空白。
    """
    # paper_layouts 只是解析结果（未翻译），这里要的就是"哪些页有可译块带公式"
    want = {L.page_index: sum(len(b.formulas) for b in L.blocks if b.translatable)
            for L in paper_layouts}
    doc = fitz.open(formula_output)
    try:
        pages_with_formulas = [p for p, n in want.items() if n > 0]
        assert pages_with_formulas, "基准论文应当含行内公式"
        for p in pages_with_formulas:
            # 每个回贴公式是一个 XObject；页面本身的图像也算，故只要求"明显多于 1"
            assert len(doc[p].get_xobjects()) > 1, (
                f"第 {p + 1} 页应有公式回贴，实际 XObject 数为 "
                f"{len(doc[p].get_xobjects())}——公式被静默丢弃了")
    finally:
        doc.close()


def test_evaluate_reports_expected_shape(paper_path, formula_output):
    res = evaluate(paper_path, formula_output, "zh", pages=3)
    assert set(res) >= {"pages", "blocks", "biou", "matched", "utb"}
    assert 0.0 <= res["biou"] <= 1.0
    assert res["pages"] == 3


def test_layout_fidelity_floor(paper_path, formula_output):
    """版面保真的**回归下限**。

    这条不是"追求高分"，是**防退化**：真实论文 + Mock 译文（长度固定，可复现）
    下 BIoU 实测 0.515、UTB 6.4%。留出余量卡在 0.42 / 12%，掉下去就说明某次
    排版或回填改动把版面弄坏了——以前这种整体性劣化只能靠导出 PNG 用眼睛看。
    """
    res = evaluate(paper_path, formula_output, "zh")
    assert res["biou"] >= 0.42, f"版面保真退化：BIoU {res['biou']:.3f}"
    assert res["utb"]["ratio"] <= 0.12, (
        f"未翻译块占比升到 {res['utb']['ratio']:.1%}——出现整块漏译")


def test_biou_and_utb_must_be_read_together(paper_path):
    """守住方法论：完全不翻译时 BIoU 是满分，只有 UTB 能抓住它。"""
    a = page_boxes(paper_path, pages=2)
    res = biou_from_boxes(a, a)
    assert res["biou"] == pytest.approx(1.0), "原文对自己应当满分"
    assert untranslated_blocks(a, "zh")["ratio"] > 0.5, "但 UTB 必须报警"
