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


# --- 拼版模式下书签与链接必须跟着搬 -------------------------------------------

@pytest.fixture
def booked_pdf(tmp_path):
    """三页 PDF：第 1 页两条链接（一条指向第 3 页，一条页内），带 3 条书签。"""
    path = tmp_path / "booked.pdf"
    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=300, height=200)
    doc[0].insert_text((50, 100), "See [41] for details.", fontsize=10)
    doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(70, 90, 90, 103),
                        "page": 2, "to": fitz.Point(50, 60)})
    doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(10, 10, 40, 20),
                        "uri": "https://example.com"})
    doc.set_toc([[1, "Intro", 1], [1, "Method", 2], [2, "Detail", 3]])
    doc.save(str(path))
    doc.close()
    return str(path)


def _layouts_for(n: int):
    from src.pdf_parser import PageLayout
    b = Block(text="See [41] for details.", x0=50.0, top=90.0, x1=250.0,
              bottom=103.0, size=10.0, page_index=0, translatable=True,
              translation="详见 [41] 的说明。", line_rects=[(50.0, 90.0, 250.0, 103.0)])
    return [PageLayout(page_index=i, width=300.0, height=200.0,
                       blocks=[b] if i == 0 else []) for i in range(n)]


@pytest.mark.parametrize("mode", ["translated", "bilingual", "sidebyside", "updown"])
def test_outline_survives_every_output_mode(booked_pdf, tmp_path, mode):
    """回归 2026-09-07：目录（书签）原先只在纯译文模式下幸存。

    对照模式全军覆没——`show_pdf_page` 是图形操作、书签不跟随，`insert_pdf`
    逐页插入也不带 TOC。实测 15 页论文：原文 35 条书签，bilingual /
    sidebyside / updown 三种模式**一条不剩**。同类项目（PDFMathTranslate）
    把「保留目录与注释」写在功能表第一行。
    """
    out = tmp_path / f"o_{mode}.pdf"
    build_output(booked_pdf, str(out), _layouts_for(3), mode)
    doc = fitz.open(str(out))
    try:
        titles = [t[1] for t in doc.get_toc()]
        assert titles == ["Intro", "Method", "Detail"], f"{mode} 模式书签丢失"
    finally:
        doc.close()


@pytest.mark.parametrize("mode", ["bilingual", "sidebyside", "updown"])
def test_cross_page_links_survive_layout_modes(booked_pdf, tmp_path, mode):
    """跨页跳转（正文引用 → 参考文献页）在对照模式下必须还在。

    回归 2026-09-07：`bilingual` 逐页 `insert_pdf`，目标页不在本次插入范围内
    的链接被 PyMuPDF 丢弃；`sidebyside`/`updown` 用 `show_pdf_page` 连页内
    链接都不剩。实测 15 页论文 251 条链接 → 48 / 0 / 0，而其中 160 条正是
    正文引用指向参考文献页的跳转。
    """
    out = tmp_path / f"x_{mode}.pdf"
    build_output(booked_pdf, str(out), _layouts_for(3), mode)
    doc = fitz.open(str(out))
    try:
        goto = [l for p in doc for l in p.get_links()
                if l.get("kind") == fitz.LINK_GOTO and l.get("page", -1) >= 0]
        assert goto, f"{mode} 模式跨页跳转链接全部丢失"
        uri = [l for p in doc for l in p.get_links()
               if l.get("kind") == fitz.LINK_URI]
        assert uri, f"{mode} 模式外部链接丢失"
        for l in goto:
            assert 0 <= l["page"] < len(doc), "跳转目标页越界"
    finally:
        doc.close()


def test_sidebyside_translated_half_links_are_shifted(booked_pdf, tmp_path):
    """左右对照：右半（译文）的链接必须整体右移一个页宽，不能压在左半上。"""
    out = tmp_path / "sbs.pdf"
    build_output(booked_pdf, str(out), _layouts_for(3), "sidebyside")
    doc = fitz.open(str(out))
    try:
        half = doc[0].rect.width / 2
        xs = [fitz.Rect(l["from"]).x0 for l in doc[0].get_links()]
        assert any(x < half for x in xs), "左半（原文）应有链接"
        assert any(x >= half for x in xs), "右半（译文）应有链接——没有说明没搬过去"
    finally:
        doc.close()


def test_updown_translated_half_links_are_shifted(booked_pdf, tmp_path):
    """上下对照：下半（译文）的链接必须整体下移一个页高。"""
    out = tmp_path / "ud.pdf"
    build_output(booked_pdf, str(out), _layouts_for(3), "updown")
    doc = fitz.open(str(out))
    try:
        half = doc[0].rect.height / 2
        ys = [fitz.Rect(l["from"]).y0 for l in doc[0].get_links()]
        assert any(y < half for y in ys), "上半（原文）应有链接"
        assert any(y >= half for y in ys), "下半（译文）应有链接"
    finally:
        doc.close()


def test_bilingual_keeps_reader_on_their_own_side(booked_pdf, tmp_path):
    """双语前后页：页序为 原0,译0,原1,译1…

    原文页上的跳转要落到目标页的**原文页**（偶数），译文页上的落到**译文页**
    （奇数）——读者点一下不该被甩到另一路去。
    """
    out = tmp_path / "bi.pdf"
    build_output(booked_pdf, str(out), _layouts_for(3), "bilingual")
    doc = fitz.open(str(out))
    try:
        assert len(doc) == 6
        for p in doc:
            for l in p.get_links():
                if l.get("kind") != fitz.LINK_GOTO or l.get("page", -1) < 0:
                    continue
                assert l["page"] % 2 == p.number % 2, (
                    f"第 {p.number} 页的跳转落到了另一侧（目标 {l['page']}）")
    finally:
        doc.close()
