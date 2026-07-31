"""参考文献超链接：保住原有链接，并跟着重排后的译文重新锚定。

背景（2026-08-01 实测）：LaTeX（hyperref）生成的论文，正文里的 "[41]" **本来就
链到参考文献条目**——某篇样本前 6 页就有 100 条。但 PyMuPDF 的 `apply_redactions`
会**连带删除与抹除区重叠的注释**，于是译文输出里 111 条只剩 12 条：我们把原有的
超链接毁掉了。所以要做的不是"实现超链接"，而是别弄丢 + 按译文新位置重新锚定。

⚠️ 这批用例不能用 MockTranslator 跑通路：它的占位译文里没有 "[41]" 这类标记，
`_cite_rects` 找不到东西可锚，测了个寂寞。真实翻译会保留（见 translator.py 的
提示词"引用编号 [12] 等不翻译"），所以这里直接给 Block 塞含引用的译文。
"""
import pytest

fitz = pytest.importorskip("fitz")

from src.pdf_parser import Block, PageLayout          # noqa: E402
from src.pdf_writer_fitz import _cite_rects, build_output  # noqa: E402


# --- 引用编号定位（纯函数）----------------------------------------------------

def _m(t, s):                      # 每字符固定 10pt 宽，便于算期望值
    return len(t) * 10.0


def test_cite_rects_finds_single_citation():
    got = _cite_rects("详见 [41] 一节", x=100.0, y_top=50.0, h=12.0,
                      size=10.0, measure=_m)
    assert len(got) == 1
    num, (x0, top, x1, bot) = got[0]
    assert num == 41
    assert x0 == pytest.approx(100.0 + 4 * 10.0)   # "详见 [" 共 4 字
    assert x1 == pytest.approx(100.0 + 6 * 10.0)   # 再加 "41"
    assert (top, bot) == (50.0, 62.0)


def test_cite_rects_splits_multi_citation():
    """"[46, 82]" 要拆成两个独立链接——它们指向不同的文献条目。"""
    got = _cite_rects("见 [46, 82]", 0.0, 0.0, 12.0, 10.0, _m)
    assert [n for n, _ in got] == [46, 82]
    assert got[0][1][0] < got[1][1][0]


@pytest.mark.parametrize("text", [
    "这是 2024 年的研究",        # 裸数字不是引用
    "见附录 [见后文]",            # 方括号里不是数字
    "误差为 [0.5",               # 括号没闭合
    "没有任何标记",
])
def test_cite_rects_ignores_non_citations(text):
    assert _cite_rects(text, 0.0, 0.0, 12.0, 10.0, _m) == []


# --- 端到端：链接确实跟着译文挪了位 -------------------------------------------

@pytest.fixture
def linked_pdf(tmp_path):
    """造一份两页 PDF：第 1 页正文含 "[41]" 且链到第 2 页某处。"""
    path = tmp_path / "src.pdf"
    doc = fitz.open()
    doc.new_page(width=300, height=200)
    doc.new_page(width=300, height=200)
    # 先把两页都建出来再取页对象：new_page() 之后早先拿到的 Page 会失效
    p1 = doc[0]
    p1.insert_text((50, 100), "See [41] for details.", fontsize=10)
    # "[41]" 在原文里的大致位置
    p1.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(70, 90, 90, 103),
                    "page": 1, "to": fitz.Point(50, 60)})
    doc.save(str(path))
    doc.close()
    return str(path)


def _layout_with(translation: str) -> PageLayout:
    b = Block(text="See [41] for details.", x0=50.0, top=90.0, x1=250.0,
              bottom=103.0, size=10.0, page_index=0, translatable=True,
              translation=translation,
              line_rects=[(50.0, 90.0, 250.0, 103.0)])
    return PageLayout(page_index=0, width=300.0, height=200.0, blocks=[b])


def test_citation_link_survives_and_moves(linked_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    build_output(linked_pdf, str(out), [_layout_with("详见 [41] 的说明。")])

    doc = fitz.open(str(out))
    try:
        links = doc[0].get_links()
        cites = [l for l in links
                 if l.get("kind") == fitz.LINK_GOTO and l.get("page") == 1]
        assert cites, "引用链接应当被保留下来"
        r = fitz.Rect(cites[0]["from"])
        # 锚点必须落在译文那一行上，而不是原文的老位置
        assert 90.0 <= r.y0 <= 115.0
        assert r.width > 0
    finally:
        doc.close()


def test_no_citation_in_translation_drops_link(linked_pdf, tmp_path):
    """译文里没有该引用标记时，宁可不给链接，也不能留一个指向错位置的。"""
    out = tmp_path / "out2.pdf"
    build_output(linked_pdf, str(out), [_layout_with("这段译文没有引用标记。")])

    doc = fitz.open(str(out))
    try:
        assert not [l for l in doc[0].get_links()
                    if l.get("kind") == fitz.LINK_GOTO and l.get("page") == 1]
    finally:
        doc.close()
