"""水平对齐：从几何反推 + 排版引擎按对齐落位。

背景（2026-07-31）：排版引擎原先只有「从框左边开始排」一种模式，而居中块的
框就是它自己的墨迹范围。中文译文比英文短约三分之一，于是每个居中标题都向左
偏 (原文宽 − 译文宽)/2——偏移同向且固定，整篇看下来就是歪的。

这批用例守的是**误判**：几何上和居中长得很像、但绝不能当居中处理的三种版式
（悬挂缩进列表 / 左对齐多行标题 / 对称缩进引用块）都在下面。它们都是从真实
论文里抓出来的真实反例，不是假想。
"""
import pytest

from src.layout import layout_block
from src.pdf_parser import Block, PageLayout, _mark_alignment

L, R = 50.0, 300.0          # 栏边界，宽 250 → tol = max(2, 0.02*250) = 5.0
TOL = 5.0


def _blk(x0, x1, *, lines=None, size=9.0, bold=False, top=0.0):
    lines = lines or [(x0, top, x1, top + size * 1.2)]
    return Block(text="X" * 20, x0=x0, top=top, x1=x1,
                 bottom=lines[-1][3], size=size, page_index=0,
                 line_rects=[tuple(t) for t in lines], bold=bold)


def _page(target: Block) -> Block:
    """把待测块放进一页里，另配两个多行正文块把栏边界撑出来。"""
    body = [
        _blk(L, R, lines=[(L, 100, R, 110), (L, 112, R, 122)]),
        _blk(L, R, lines=[(L, 130, R, 140), (L, 142, R, 152)]),
    ]
    page = PageLayout(page_index=0, width=350.0, height=800.0,
                      blocks=body + [target])
    _mark_alignment(page, None)
    return target


# --- 应判为居中 ---------------------------------------------------------------

def test_centered_two_line_title():
    """真实用例：ACM acmart 大标题。两行中点重合、左右参差相等。"""
    b = _page(_blk(100, 250, lines=[(100, 0, 250, 12), (130, 14, 220, 26)],
                   size=17.2, bold=True))
    assert b.align == "center"


def test_centered_single_line_caption():
    """真实用例：'Figure 6: Screenshot on Design Results'，左右间隙相等。"""
    b = _page(_blk(120, 230, bold=True))
    assert b.align == "center"


def test_centered_block_records_column_bounds():
    """居中要相对**栏**算，所以栏边界必须记在块上供回填时用。"""
    b = _page(_blk(120, 230, bold=True))
    assert (b.col_x0, b.col_x1) == (L, R)


# --- 绝不能判为居中（真实反例）------------------------------------------------

def test_hanging_indent_list_is_not_centered():
    """悬挂缩进的编号列表：只有左边参差，右边被两端对齐拉齐。

    中点差恰好落在容差内，只看「中点重合 + 起点分散」会误判（实测误判过）。
    """
    b = _page(_blk(50, 300, lines=[(50, 0, 300, 12), (60, 14, 300, 26)]))
    assert b.align == "left"


def test_left_aligned_two_line_heading_is_not_centered():
    """左对齐的两行标题：只有右边参差。"""
    b = _page(_blk(50, 290, lines=[(50, 0, 290, 12), (50, 14, 180, 26)]))
    assert b.align == "left"


def test_symmetric_indent_quote_is_not_centered():
    """acmart 的 \\begin{quote}：左右各缩进 ~24pt，中点当然也重合。

    按居中处理会把整段引用逐行居中、还撑到满栏宽——比原来的左偏更难看。
    """
    b = _page(_blk(75, 275, lines=[(75, 0, 275, 12), (75, 14, 275, 26),
                                   (75, 28, 275, 40)]))
    assert b.align == "left"


def test_long_block_never_centered():
    """4 行以上按段落论处：论文里没有居中的长段落，而误判代价很大。"""
    lines = [(100, i * 14, 250, i * 14 + 12) if i % 2 else
             (130, i * 14, 220, i * 14 + 12) for i in range(6)]
    b = _page(_blk(100, 250, lines=lines))
    assert b.align == "left"


def test_ordinary_body_paragraph_is_left():
    b = _page(_blk(L, R, lines=[(L, 0, R, 12), (L, 14, R, 26),
                                (L, 28, 180, 40)]))
    assert b.align == "left"


def test_narrow_column_falls_back_to_left():
    """栏太窄时几何不可靠，一律左对齐，不冒险。"""
    page = PageLayout(page_index=0, width=350.0, height=800.0,
                      blocks=[_blk(10, 40), _blk(15, 35)])
    _mark_alignment(page, None)
    assert all(b.align == "left" for b in page.blocks)


# --- 排版引擎按对齐落位 -------------------------------------------------------

def _measure(t: str, s: float) -> float:
    return len(t) * s * 0.5


def _first_x(align: str) -> float:
    laid = layout_block("abcd", {}, (0.0, 0.0, 100.0, 100.0), 10.0,
                        _measure, align=align)
    return laid.items[0].x


def test_flow_left_starts_at_box_left():
    assert _first_x("left") == pytest.approx(0.0)


def test_flow_center_centers_within_box():
    # "abcd" @10pt = 20pt 宽，框宽 100 → 左边留 40
    assert _first_x("center") == pytest.approx(40.0)


def test_center_disables_justification():
    """两端对齐与居中互斥：同时开会把居中行撑成满行，居中当场失效。"""
    # 这行排完占 90pt、余 10pt，正好落在两端对齐的触发区间内；
    # 后面还有一个放不下的长词，保证它不是段末行（段末行本就不参与对齐）。
    text = "aaa bbb ccc ddd ee ffffffffff"
    box = (0.0, 0.0, 100.0, 200.0)
    left = layout_block(text, {}, box, 10.0, _measure, align="left")
    center = layout_block(text, {}, box, 10.0, _measure, align="center")
    # 左对齐首行会被两端对齐撑开，末项右端接近框右边；居中则不会。
    left_end = max(it.x + it.w for it in left.items if it.y_top < 12)
    center_end = max(it.x + it.w for it in center.items if it.y_top < 12)
    assert left_end > center_end
