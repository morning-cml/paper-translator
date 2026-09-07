"""管线纯函数：跨栏配对（T12）、译文拆回、CJK 判定、全文语境、服务名推断。"""
import pytest

from src.pipeline import (_continues, _doc_context, _has_cjk,
                          _split_translation, _unit_text, service_label)


# ---- service_label：错误提示要点名具体服务 ----
@pytest.mark.parametrize("url,name", [
    ("https://api.deepseek.com", "DeepSeek"),
    ("https://api.openai.com/v1", "OpenAI"),
    ("https://api.moonshot.cn/v1", "Kimi（月之暗面）"),
    ("https://open.bigmodel.cn/api/paas/v4", "智谱 GLM"),
    ("http://127.0.0.1:11434/v1", "本地服务（Ollama？）"),
    ("http://localhost:11434/v1", "本地服务（Ollama？）"),
])
def test_service_label(url, name):
    assert service_label(url) == name


class FakeBlock:
    def __init__(self, text, x0=36, top=100, x1=290, bottom=200,
                 translatable=True, bold=False, size=10.0, page_index=0):
        self.text, self.x0, self.top, self.x1, self.bottom = text, x0, top, x1, bottom
        self.translatable, self.bold, self.size = translatable, bold, size
        self.page_index, self.from_ocr, self.translation = page_index, False, None


# ---- _has_cjk ----
@pytest.mark.parametrize("text,expected", [
    ("这是中文", True), ("mixed 中文 text", True),
    ("pure english", False), ("12345 (2025)", False), ("", False), (None, False),
])
def test_has_cjk(text, expected):
    assert _has_cjk(text) is expected


# ---- _continues：判断是否被腰斩 ----
def test_continues_true_when_unterminated_and_lowercase():
    a = FakeBlock("...completed contributions to software and")
    b = FakeBlock("methodology during a summer internship")
    assert _continues(a, b)


@pytest.mark.parametrize("ta,tb", [
    ("A full sentence ends here.", "next paragraph starts"),   # 前句已结束
    ("unterminated tail", "Uppercase start follows"),          # 后句是新句
    ("", "something"), ("something", ""),                      # 空
])
def test_continues_false(ta, tb):
    assert not _continues(FakeBlock(ta), FakeBlock(tb))


# ---- _unit_text：合并 ----
def test_unit_text_single():
    assert _unit_text([FakeBlock("only one")]) == "only one"


def test_unit_text_joins_with_space():
    out = _unit_text([FakeBlock("first half"), FakeBlock("second half")])
    assert out == "first half second half"


def test_unit_text_repairs_hyphenation():
    out = _unit_text([FakeBlock("method-"), FakeBlock("ology works")])
    assert out == "methodology works", "连字符断词应直接拼接"


# ---- _split_translation：按比例在句读处拆回 ----
def test_split_prefers_sentence_boundary():
    tr = "第一部分讲了方法与数据来源。第二部分给出实验结果与讨论内容"
    a, b = _split_translation(tr, 50, 50)
    assert a + b == tr, "拆分不得丢字"
    assert a.endswith("。"), "应优先在句号处切开"


def test_split_without_punctuation_still_lossless():
    tr = "无标点连续文本" * 10
    a, b = _split_translation(tr, 30, 70)
    assert a + b == tr
    assert a and b


def test_split_respects_length_ratio():
    tr = "甲" * 50 + "，" + "乙" * 50
    a, b = _split_translation(tr, 50, 50)
    assert a + b == tr
    assert 0.25 < len(a) / len(tr) < 0.75


def test_split_empty():
    assert _split_translation("", 10, 10) == ("", "")


# ---- _doc_context ----
class FakeLayout:
    def __init__(self, blocks):
        self.blocks = blocks
        self.width, self.height = 594.0, 756.0


def test_doc_context_picks_title_and_abstract():
    title = FakeBlock("A Study of Robots in Classrooms", size=18.0)
    abstract = FakeBlock("According to productive failure theory, " * 20, size=9.5)
    ctx = _doc_context([FakeLayout([title, abstract])])
    assert "A Study of Robots" in ctx
    assert "摘要节选" in ctx


def test_doc_context_empty_is_safe():
    assert _doc_context([]) == ""
    assert _doc_context([FakeLayout([])]) == ""


# ---- 跨栏缝合与行内公式占位符不能共存 ----

def _blk(text, formulas=(), x0=36.0, top=400.0):
    from src.pdf_parser import Block
    return Block(text=text, x0=x0, top=top, x1=x0 + 244.0, bottom=top + 60.0,
                 size=10.0, page_index=0, formulas=list(formulas),
                 line_rects=[(x0, top, x0 + 244.0, top + 12.0)])


def _page(blocks):
    from src.pdf_parser import PageLayout
    return PageLayout(page_index=0, width=595.0, height=780.0, blocks=blocks)


_TAIL = ("The proposed estimator {}converges under mild assumptions "
         "whenever the sample size grows without bound and")
_HEAD = ("the regularization parameter {}is chosen appropriately according "
         "to the theory outlined in the previous section.")


def test_blocks_without_formulas_are_still_stitched():
    """T12 本身不受影响：无公式的腰斩段照常缝合成一个翻译单元。"""
    from src.pipeline import _make_units
    a, b = _blk(_TAIL.format(""), x0=36.0), _blk(_HEAD.format(""), x0=310.0, top=100.0)
    assert len(_make_units([_page([a, b])], [a, b])) == 1


def test_blocks_with_formulas_are_never_stitched():
    """含 ⟦Fn⟧ 的块不得参与缝合——否则公式会贴错或凭空消失。

    回归 2026-09-07：⟦Fn⟧ 的编号是**块内局部**的，两块拼成一个翻译单元后
    编号会撞车（a 的 ⟦F1⟧ 与 b 的 ⟦F1⟧ 同时出现）；而 `_split_translation`
    只按字符比例找句读处切，根本不认占位符归属。于是译文里的占位符可能落到
    另一块名下 —— 那一块要么按自己的 formulas 贴出**错误的公式图**，要么查
    不到该编号而**静默丢掉公式**（原文已抹除，公式就此消失）。实测在仓库自带
    的基准论文上就真丢了 2 个行内公式。
    """
    from src.pdf_parser import FormulaSpan
    from src.pipeline import _make_units
    a = _blk(_TAIL.format("⟦F1⟧ "), [FormulaSpan(1, 100, 400, 130, 412)])
    b = _blk(_HEAD.format("⟦F1⟧ "), [FormulaSpan(1, 380, 100, 410, 112)],
             x0=310.0, top=100.0)
    units = _make_units([_page([a, b])], [a, b])
    assert len(units) == 2, "含公式的块必须各自成单元"
    assert all(len(u) == 1 for u in units)


# ---- 页码范围（对齐同类工具的 --pages "1-3,5,8-" 语法） ----

@pytest.mark.parametrize("spec,expect", [
    ("5", [5]),
    ("3-7", [3, 4, 5, 6, 7]),
    ("8-", [8, 9, 10]),                       # 开口到末尾
    ("-3", [1, 2, 3]),                        # 开口自第一页
    ("1-2,5,9-", [1, 2, 5, 9, 10]),
    ("3-1", [1, 2, 3]),                       # 写反了也认
    ("2,2,2", [2]),                           # 重复取并集
    ("1-999", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),   # 越界截到末页
    ("1，3", [1, 3]),                          # 中文逗号
    ("-", list(range(1, 11))),                # 两头都开口 = 整篇（与 8- / -3 一致）
])
def test_parse_page_range(spec, expect):
    from src.pipeline import parse_page_range
    assert sorted(p + 1 for p in parse_page_range(spec, 10)) == expect


@pytest.mark.parametrize("spec", ["abc", "0-3", "20-30", "1-x"])
def test_parse_page_range_rejects_nonsense(spec):
    """看不懂或一页都选不中时必须**明确报错**，不能悄悄当成"整篇翻"。"""
    from src.pipeline import PageRangeError, parse_page_range
    with pytest.raises(PageRangeError):
        parse_page_range(spec, 10)


def test_empty_spec_is_not_an_error():
    from src.pipeline import parse_page_range
    assert parse_page_range("", 10) == set()
