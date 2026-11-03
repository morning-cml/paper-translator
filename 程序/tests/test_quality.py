"""译文质量自检（B3）：检出能力 + 零误报 + 重译接线。"""
import pytest

from src.glossary import Glossary
from src.quality import Issue, check, describe
from src.translator import BaseTranslator

SRC = ("We compared three instructional methods across 135 students in the "
       "eighth grade mathematics lesson to evaluate whether observing robot "
       "failure improves learning outcomes significantly in the classroom.")
GOOD = ("我们在八年级数学课上比较了三种教学方法，涉及 135 名学生，"
        "以评估观察机器人失败是否能显著改善课堂学习成果。")


def codes(src, tgt):
    return {i.code for i in check(src, tgt)}


# ---- 正常译文零误报（阈值按真实语料标定）----
# 每条译文配**它自己的**原文。此前这里只有两种原文轮着用、断言末尾还挂着
# `or True`——无论 check 返回什么都通过，"零误报"其实从没被测过。
@pytest.mark.parametrize("src,tgt", [
    (SRC, GOOD),
    ("Use of this article is subject to the Terms of service",
     "使用本文需遵守服务条款"),                                # 实测最短比 0.20
    ("Fig. 8. Students' classroom activity engagement in different conditions.",
     "图 8. 不同条件下学生的课堂活动参与。"),
    ("Submitted 18 November 2024 Accepted 12 August 2025",
     "投稿日期：2024年11月18日  接收日期：2025年8月12日"),
    # 以下开头都是正文本身，不是模型的客套话
    ("The following are the three main contributions of this paper to the "
     "field of human-computer interaction research.",
     "以下是本文对人机交互研究领域的三项主要贡献："),
    ("This is because participants had been exposed to similar robots and "
     "related task settings before the experiment.",
     "这是因为参与者在实验之前已经接触过类似的机器人和相关的任务设置。"),
    ("Of course, this conclusion still needs to be verified with a larger "
     "sample while considering other confounding factors.",
     "当然，这一结论仍需在更大规模的样本中加以验证，并考虑其他混杂因素。"),
])
def test_no_false_positive_on_normal(src, tgt):
    assert check(src, tgt) == []


def test_english_target_here_is_not_meta():
    """zh→en 时 "Here is …" 可以是正文。"""
    src = "这是一项关于机器人的研究，我们在课堂中测试了学生的学习效果和参与程度。"
    tgt = "Here is a study in which we tested how students learn with a robot peer in class."
    assert "meta_prefix" not in {i.code for i in check(src, tgt, "zh", "en")}


def test_good_translation_passes():
    assert check(SRC, GOOD) == []


def test_short_source_not_length_checked():
    """短原文的长度比噪声大，不做长度判定，避免误报。"""
    assert "too_short" not in codes("Results", "结")


# ---- 各类坏译文必须检出 ----
def test_detects_truncation():
    assert "too_short" in codes(SRC, "我们比较了三种")


def test_detects_verbosity():
    # 原文 ~170 字符；译文需超过 1.6 倍才算异常（阈值按真实语料留足余量）
    assert "too_long" in codes(SRC, "我们比较了三种教学方法并且详细说明其背景。" * 20)


@pytest.mark.parametrize("num,forms", [
    ("20", ("二十", "二〇")), ("15", ("十五",)), ("10", ("十",)),
    ("35", ("三十五",)), ("200", ("二百", "两百")),
])
def test_cn_number_forms(num, forms):
    from src.quality import _cn_forms
    got = _cn_forms(num)
    for f in forms:
        assert f in got, f"{num} 应接受中文写法 {f}，实际生成 {got}"


@pytest.mark.parametrize("prefix", [
    "以下是翻译：", "好的，", "以下是该段落的中文译文：", "这是翻译结果：",
    "当然！以下是", "译文：", "Here is the translation: ",
])
def test_detects_meta_prefix(prefix):
    assert "meta_prefix" in codes(SRC, prefix + GOOD)


def test_detects_repetition():
    assert "repetition" in codes(SRC, "我们比较了三种教学方法涉及学生" * 6)


def test_detects_empty():
    assert codes(SRC, "   ") == {"empty"}


# ---- 数字检查：只查"不该被改写"的数字 ----
def test_detects_missing_significant_number():
    bad = "我们在八年级数学课上比较了三种教学方法，以评估观察机器人失败的效果。"
    assert "missing_numbers" in codes(SRC, bad)   # 丢了 135


@pytest.mark.parametrize("src,tgt", [
    # 个位数被正确译成中文数字——真实语料里的常见情形，绝不能误报
    ("Objectives and experiment design of study 2", "实验二的目标与实验设计"),
    ("During the 2-week adaptation phase", "在为期两周的适应阶段"),
    ("Study 1 was conducted first", "实验一率先开展"),
    # 大数字以中文写法出现也应接受
    ("A total of 20 classes joined", "共有二十个班级参加"),
])
def test_small_numbers_and_cn_forms_not_flagged(src, tgt):
    assert "missing_numbers" not in codes(src, tgt)


def test_decimal_must_be_kept():
    src = "The mean age was 13.6 years across all participating classes today"
    assert "missing_numbers" in codes(src, "所有参与班级的平均年龄为十几岁。")


def test_placeholder_excluded_from_checks():
    src = "The effect ⟦F1⟧ was significant across all conditions in this study"
    tgt = "在本研究的所有条件下，效应 ⟦F1⟧ 均显著。"
    assert check(src, tgt) == []


def test_describe_joins_details():
    text = describe([Issue("a", "问题甲"), Issue("b", "问题乙")])
    assert "问题甲" in text and "问题乙" in text


# ---- 与翻译流程接线 ----
class _Stub(BaseTranslator):
    """首次返回坏译文，重译返回好译文。"""

    def __init__(self, fix_result, **kw):
        super().__init__(**kw)
        self.fix_calls = 0
        self.fix_result = fix_result

    def _translate_batch(self, batch, glossary_block):
        return ["我们比较了三种" for _ in batch]      # 截断

    def _translate_fix(self, text, problem, glossary_block):
        self.fix_calls += 1
        self.last_problem = problem
        return self.fix_result


def test_bad_translation_triggers_one_retry_and_is_replaced():
    t = _Stub(GOOD, batch_size=1)
    out = t.translate_texts([SRC], Glossary({}))
    assert t.fix_calls == 1, "应触发且仅触发一次定向重译"
    assert out[0] == GOOD, "重译通过自检后应采纳"
    assert t.quality_flags == 1 and t.quality_fixed == 1
    assert "过短" in t.last_problem, "应把具体问题告知模型"


def test_retry_still_bad_keeps_first_translation():
    t = _Stub("仍然很短", batch_size=1)
    out = t.translate_texts([SRC], Glossary({}))
    assert t.fix_calls == 1
    assert out[0] == "我们比较了三种", "重译仍不合格时保留首次译文"
    assert t.quality_flags == 1 and t.quality_fixed == 0


def test_good_translation_never_retries():
    class Good(_Stub):
        def _translate_batch(self, batch, glossary_block):
            return [GOOD for _ in batch]
    t = Good(GOOD, batch_size=1)
    t.translate_texts([SRC], Glossary({}))
    assert t.fix_calls == 0 and t.quality_flags == 0, "正常译文不得产生额外开销"
