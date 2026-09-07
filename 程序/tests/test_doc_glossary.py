"""本文术语表：全文自动抽取 + 一次性译出 + 全篇统一（对齐 BabelDOC）。

要解决的问题：静态 CSV 只有通用术语，每篇论文自己的核心说法是**逐批**送译的，
同一个词这批译成"生成性失败"、下一批译成"有效失败"。BabelDOC 论文把
terminology consistency 单列为指标（4.47 对 PDFMathTranslate 的 3.34），
差距正在这里。
"""
import re

import pytest

from src.config import load_config
from src.glossary import Glossary, auto_extract_terms
from src.pipeline import build_doc_glossary
from src.translator import DeepSeekTranslator
from src.transcache import TransCache

PARA = ("Productive failure (PF) encourages learners to attempt problems before "
        "instruction. In our study, the productive failure condition consistently "
        "outperformed direct instruction on conceptual knowledge and knowledge "
        "transfer. Students in the direct instruction group reported higher "
        "perceived competence but weaker knowledge transfer. ")


# ---------------- 抽取（纯本地，不联网） ----------------

def test_extracts_acronym_definitions():
    """"全称 (缩写)" 是论文里最可靠的关键术语信号。"""
    terms = auto_extract_terms([PARA * 3], limit=20, min_count=3)
    assert "productive failure" in terms


def test_rejects_bogus_acronym_parentheses():
    """首字母对不上就不是缩写定义——"(see Fig. 1)" 之流不得混进来。"""
    from src.glossary import _acronym_terms
    got = _acronym_terms("the learning outcome was stable (see Fig. 1) overall")
    assert got == []


def test_skips_terms_already_in_static_glossary():
    """静态库已有权威译法的词，再送去翻译纯属浪费 token。"""
    known = {"productive failure": "生成性失败"}
    terms = auto_extract_terms([PARA * 3], limit=20, min_count=3, known=known)
    assert "productive failure" not in terms


def test_function_words_never_start_or_end_a_term():
    from src.glossary import _STOP
    terms = auto_extract_terms([PARA * 4], limit=30, min_count=3)
    for t in terms:
        words = t.split()
        assert words[0] not in _STOP and words[-1] not in _STOP, t


def test_page_furniture_does_not_become_a_term():
    """页眉页脚每页重复一次，十几页下来轻松冲进高频榜。

    回归：实测某篇论文抽出过 "sci robot eadu"、"robot eadu september" 这种
    垃圾——全部来自逐页重复的期刊页眉。它们天然是短块，按长度筛掉即可。
    """
    header = "Sci. Robot. 10, eadu5257 (2025) 12 September 2026"   # 短块
    terms = auto_extract_terms([PARA * 4] + [header] * 15, limit=30, min_count=3)
    assert not any("eadu" in t or "robot sci" in t for t in terms)


def test_hyphenated_line_breaks_are_repaired():
    """PDF 断词留下的 "problem- solving" 要合回一个词，不能当两个。"""
    text = ("The problem- solving task was scored by two raters. "
            "Each problem- solving attempt was independently coded. "
            "We compared problem- solving performance across conditions. "
            "Later problem- solving trials showed the same pattern. ")
    terms = auto_extract_terms([text], limit=20, min_count=3)
    assert not any(t.endswith("-s") or "- " in t for t in terms)


def test_empty_input_is_safe():
    assert auto_extract_terms([], limit=10) == []
    assert auto_extract_terms(["short"], limit=10) == []


# ---------------- 合并 ----------------

def test_static_glossary_wins_over_auto():
    """手工维护的译法是权威，自动抽取的只补它没覆盖到的。"""
    g = Glossary({"neural network": "神经网络"})
    merged = g.merged_with({"neural network": "神经网路", "robot peer": "机器人同伴"})
    assert merged.entries["neural network"] == "神经网络"
    assert merged.entries["robot peer"] == "机器人同伴"


def test_merging_nothing_returns_same_object():
    g = Glossary({"a b": "甲乙"})
    assert g.merged_with({}) is g


# ---------------- 译出 + 接入流水线 ----------------

class _Resp:
    def __init__(self, body):
        self.status_code, self.text, self._b = 200, "ok", body

    def json(self):
        return {"choices": [{"message": {"content": self._b}}]}


def _stub_client(monkeypatch, calls, reply=None, **kw):
    tr = DeepSeekTranslator(api_key="k", **kw)

    def handler(url, headers=None, json=None, timeout=None, verify=None):
        body = json["messages"][-1]["content"]
        calls.append(body)
        if reply is not None:
            return _Resp(reply)
        return _Resp("\n".join(
            f"[[{m.group(1)}]] 译{m.group(1)}"
            for m in re.finditer(r"\[\[(\d+)\]\] (.+)", body)))

    monkeypatch.setattr(tr.session, "post", handler)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tr


def test_terms_cost_exactly_one_request(monkeypatch):
    calls = []
    tr = _stub_client(monkeypatch, calls)
    got = tr.translate_terms(["robot peer", "productive failure", "prior knowledge"])
    assert len(calls) == 1, "全篇只该发一次请求"
    assert set(got) == {"robot peer", "productive failure", "prior knowledge"}


def test_terms_are_cached_across_runs(monkeypatch, tmp_path):
    """重跑同一篇文档不该再为术语表付一次费。"""
    cache = TransCache(tmp_path / "c.json")
    calls = []
    terms = ["robot peer", "productive failure"]
    for _ in range(3):
        tr = _stub_client(monkeypatch, calls, persist=cache, cache_scope="s")
        assert tr.translate_terms(terms) == {"robot peer": "译1",
                                             "productive failure": "译2"}
    assert len(calls) == 1, f"应只请求一次，实际 {len(calls)} 次"


def test_untranslated_echo_is_discarded(monkeypatch):
    """模型原样回显 = 没译出来。术语表宁缺毋滥——错译会让全篇跟着错。"""
    calls = []
    tr = _stub_client(monkeypatch, calls,
                      reply="[[1]] robot peer\n[[2]] 生成性失败")
    got = tr.translate_terms(["robot peer", "productive failure"])
    assert got == {"productive failure": "生成性失败"}


def test_rambling_answer_is_discarded(monkeypatch):
    """模型跑题写解释时，那一条丢掉即可，不得整表作废。"""
    calls = []
    tr = _stub_client(monkeypatch, calls,
                      reply="[[1]] 机器人同伴\n[[2]] " + "长篇解释" * 20)
    got = tr.translate_terms(["robot peer", "productive failure"])
    assert got == {"robot peer": "机器人同伴"}


def test_pipeline_injects_terms_into_batch_prompt(monkeypatch):
    calls = []
    tr = _stub_client(monkeypatch, calls)
    monkeypatch.setattr(
        "src.glossary.auto_extract_terms",
        lambda *a, **k: ["productive failure", "direct instruction"])
    g = build_doc_glossary(tr, [PARA * 3], Glossary({}), load_config(api_key="k"))
    block = g.prompt_block(["Compared with direct instruction, productive failure won."])
    assert "productive failure" in block and "direct instruction" in block


def test_auto_glossary_can_be_switched_off(monkeypatch):
    calls = []
    tr = _stub_client(monkeypatch, calls)
    g0 = Glossary({})
    g = build_doc_glossary(tr, [PARA * 3], g0,
                           load_config(api_key="k", auto_glossary=False))
    assert g is g0 and not calls


def test_mock_translator_never_pays_for_terms():
    """离线模式是用来看排版的，不该因为术语表跑去联网。"""
    from src.translator import MockTranslator
    g0 = Glossary({})
    assert build_doc_glossary(MockTranslator(), [PARA * 3], g0,
                              load_config()) is g0


def test_model_failure_falls_back_to_static_glossary(monkeypatch):
    """术语表是增强项，任何一步失败都不该连累主流程。"""
    tr = DeepSeekTranslator(api_key="k")

    def boom(*a, **k):
        raise RuntimeError("service down")

    monkeypatch.setattr(tr.session, "post", boom)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    g0 = Glossary({"neural network": "神经网络"})
    assert build_doc_glossary(tr, [PARA * 3], g0, load_config(api_key="k")) is g0


def test_extracted_terms_can_be_exported_as_csv(monkeypatch, tmp_path):
    """导出成同格式 CSV，用户挑拣后可并进静态库，下次连那次请求都省了。"""
    import csv
    calls = []
    tr = _stub_client(monkeypatch, calls)
    out = tmp_path / "auto.csv"
    build_doc_glossary(tr, [PARA * 3], Glossary({}),
                       load_config(api_key="k", save_glossary_path=str(out)))
    assert out.exists()
    with out.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows and set(rows[0]) == {"en", "zh", "note"}
    assert all(r["zh"] for r in rows)
