"""翻译层：持久缓存（T1）、失败降级（T2）、占位符保护、成本预估入口。"""
import json

import pytest

from src.glossary import Glossary
from src.transcache import TransCache
from src.translator import BaseTranslator


class StubTranslator(BaseTranslator):
    """可控桩：记录调用次数，可指定第 N 批抛异常。"""

    def __init__(self, fail_batches=(), bad_placeholder=False, **kw):
        super().__init__(**kw)
        self.calls = 0
        self.fail_batches = set(fail_batches)
        self.bad_placeholder = bad_placeholder
        self.strict_calls = 0

    def _translate_batch(self, batch, glossary_block):
        self.calls += 1
        if self.calls in self.fail_batches:
            raise RuntimeError("simulated network failure")
        if self.bad_placeholder:
            return [t.replace("⟦F1⟧", "") for t in batch]   # 故意丢占位符
        return ["译" + t for t in batch]

    def _translate_strict(self, text, glossary_block):
        self.strict_calls += 1
        return "译" + text          # 强化重试保住占位符


@pytest.fixture
def gloss():
    return Glossary({})


@pytest.fixture
def texts():
    return [f"para {i}" for i in range(10)]


def test_cache_persists_and_second_run_costs_nothing(tmp_path, gloss, texts):
    cache_file = tmp_path / "c.json"
    t1 = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                        cache_scope="m|d|ctx")
    out1 = t1.translate_texts(texts, gloss)
    assert out1 == ["译" + t for t in texts]
    assert cache_file.exists()

    t2 = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                        cache_scope="m|d|ctx")
    out2 = t2.translate_texts(texts, gloss)
    assert out2 == out1
    assert t2.calls == 0, "第二次应全部命中缓存，零请求"
    assert t2.cache_hits == len(texts)


def test_cache_scope_isolates(tmp_path, gloss, texts):
    """换模型/领域/上下文 → 缓存键变化，不得串用旧译文。"""
    cache_file = tmp_path / "c.json"
    StubTranslator(batch_size=4, persist=TransCache(cache_file),
                   cache_scope="A").translate_texts(texts, gloss)
    t = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                       cache_scope="B")
    t.translate_texts(texts, gloss)
    assert t.calls > 0 and t.cache_hits == 0


def test_failed_batch_degrades_and_rerun_completes(tmp_path, gloss, texts):
    cache_file = tmp_path / "c.json"
    t1 = StubTranslator(batch_size=4, fail_batches={1},
                        persist=TransCache(cache_file), cache_scope="s")
    out = t1.translate_texts(texts, gloss)
    fallback = [o for o, s in zip(out, texts) if o == s]
    assert len(fallback) == 4, "失败批应回退原文而非中断任务"
    assert t1.failed_texts == 4

    # 重跑：成功段走缓存，失败段补齐
    t2 = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                        cache_scope="s")
    out2 = t2.translate_texts(texts, gloss)
    assert out2 == ["译" + t for t in texts]
    assert t2.cache_hits == 6, "只有成功过的 6 段该命中缓存"


def test_failed_text_not_cached(tmp_path, gloss, texts):
    cache_file = tmp_path / "c.json"
    StubTranslator(batch_size=4, fail_batches={1},
                   persist=TransCache(cache_file),
                   cache_scope="s").translate_texts(texts, gloss)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(data) == 6, "失败段不得写入缓存（否则永远补不回来）"


def test_placeholder_loss_triggers_strict_retry(gloss):
    src = ["公式 ⟦F1⟧ 在此"]
    t = StubTranslator(batch_size=1, bad_placeholder=True)
    out = t.translate_texts(src, gloss)
    assert t.strict_calls == 1, "占位符丢失应触发一次强化重试"
    assert "⟦F1⟧" in out[0]


def test_pending_texts_reflects_cache(tmp_path, gloss, texts):
    cache_file = tmp_path / "c.json"
    t = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                       cache_scope="s")
    assert len(t.pending_texts(texts)) == len(texts)
    t.translate_texts(texts, gloss)
    t2 = StubTranslator(batch_size=4, persist=TransCache(cache_file),
                        cache_scope="s")
    assert t2.pending_texts(texts) == [], "全命中时预估应显示零请求"


# ---------------- 缓存自愈：坏缓存丢弃重译 / 强制刷新 ----------------
def test_corrupt_cached_placeholder_is_refetched(tmp_path, gloss):
    """缓存里的译文若占位符对不上（缓存损坏/旧版本写入），必须作废并重译，
    否则写回端会把公式贴错位——对应"重译同一篇因缓存反而生成不出来"。"""
    cache_file = tmp_path / "c.json"
    src = ["公式 ⟦F1⟧ 在此说明"]
    StubTranslator(persist=TransCache(cache_file),
                   cache_scope="s").translate_texts(src, gloss)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    key = next(iter(data))
    data[key] = data[key].replace("⟦F1⟧", "")     # 人为弄坏：占位符丢失
    cache_file.write_text(json.dumps(data), encoding="utf-8")

    t2 = StubTranslator(persist=TransCache(cache_file), cache_scope="s")
    out = t2.translate_texts(src, gloss)
    assert t2.calls == 1, "坏缓存应被丢弃并重新翻译，而非直接采用"
    assert t2.cache_hits == 0
    assert "⟦F1⟧" in out[0], "重译后公式占位符恢复"


def test_refresh_cache_ignores_and_overwrites(tmp_path, gloss, texts):
    """cache_refresh=True：无视旧缓存重译，但把新结果写回覆盖旧的。"""
    cache_file = tmp_path / "c.json"
    StubTranslator(persist=TransCache(cache_file),
                   cache_scope="s").translate_texts(texts, gloss)   # 先建缓存
    t2 = StubTranslator(persist=TransCache(cache_file), cache_scope="s")
    t2.cache_refresh = True
    out = t2.translate_texts(texts, gloss)
    assert t2.cache_hits == 0 and t2.calls > 0, "刷新模式应无视缓存重译"
    assert out == ["译" + t for t in texts]


# ---------------- 取消：立即短路，不跑完所有批 ----------------
def test_cancel_short_circuits_pending_batches(gloss, texts):
    """取消后，尚未开始的批必须**直接返回原文**、绝不再发起请求。

    旧代码把所有批一次性 submit、退出时 shutdown(wait=True)，导致取消要等全部
    在途+排队请求跑完才停——用户按了"感觉没反应"。"""
    t = StubTranslator(batch_size=1, max_workers=4)
    out = t.translate_texts(texts, gloss, should_cancel=lambda: True)
    assert t.calls == 0, "全程取消状态下不该真正翻译任何一批"
    assert out == texts, "未翻译的段回退原文"


def test_cancel_midway_leaves_rest_untranslated(gloss):
    """跑到一半点取消：已开始的可能完成，但剩余批不再翻译。"""
    flip = {"on": False}

    class Flip(StubTranslator):
        def _translate_batch(self, batch, gb):
            flip["on"] = True          # 第一批一跑就请求取消
            return super()._translate_batch(batch, gb)

    t = Flip(batch_size=1, max_workers=1)
    out = t.translate_texts([f"p{i}" for i in range(20)], gloss,
                            should_cancel=lambda: flip["on"])
    assert t.calls < 20, "取消后不应把 20 批全部翻完"


# ---------------------------------------------------------------------------
# _chat 重试循环：兼容严格校验参数的 OpenAI 兼容服务
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, code, body=""):
        self.status_code, self.text = code, body

    def json(self):
        return {"choices": [{"message": {"content": "你好"}}]}


def _client(monkeypatch, handler, **kw):
    """造一个 DeepSeekTranslator，把它的 session.post 换成受控桩。"""
    from src.translator import DeepSeekTranslator
    tr = DeepSeekTranslator(api_key="k", timeout=5, **kw)
    monkeypatch.setattr(tr.session, "post", handler)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tr


def test_strips_thinking_without_spending_a_retry(monkeypatch):
    """OpenAI 等服务对未知参数 thinking 直接回 400，剥掉后必须还能重试。

    回归 2026-09-07：这次重试原先**计入** max_retries，而联通性预检用的正是
    max_retries=1 —— 仅有的一次机会花在注定被拒的首个请求上，剥掉字段后的
    重试根本发不出去，预检必然失败在"翻译请求失败。"。预检是翻译的强制闸门，
    等于整个工具对这类服务完全不可用。
    """
    seen = []

    def handler(url, headers=None, json=None, timeout=None, verify=None):
        seen.append("thinking" in json)
        if "thinking" in json:
            return _Resp(400, "Unrecognized request argument supplied: thinking")
        return _Resp(200)

    tr = _client(monkeypatch, handler, max_retries=1)
    assert tr._chat([{"role": "user", "content": "hi"}]) == "你好"
    assert seen == [True, False], "剥掉 thinking 后应真的再发一次"


def test_real_400_still_reports_reason_and_terminates(monkeypatch):
    """不是 thinking 的锅时：不得死循环，且要报出服务返回的真实原因。"""
    from src.translator import TranslatorError
    seen = []

    def handler(url, headers=None, json=None, timeout=None, verify=None):
        seen.append(1)
        return _Resp(400, "model not found")

    tr = _client(monkeypatch, handler, max_retries=2)
    with pytest.raises(TranslatorError, match="model not found"):
        tr._chat([{"role": "user", "content": "hi"}])
    assert len(seen) == 3, "1 次剥离重试 + max_retries 次正常尝试"


def test_auth_error_is_not_retried(monkeypatch):
    from src.translator import TranslatorError
    seen = []

    def handler(url, headers=None, json=None, timeout=None, verify=None):
        seen.append(1)
        return _Resp(401, "bad key")

    tr = _client(monkeypatch, handler, max_retries=3)
    with pytest.raises(TranslatorError, match="401"):
        tr._chat([{"role": "user", "content": "hi"}])
    assert len(seen) == 1, "Key/额度/权限问题重试多少次都一样，应立刻报错"


def test_no_pointless_sleep_after_final_attempt(monkeypatch):
    """最后一次尝试失败后直接抛错，不再空等一轮退避。"""
    import requests
    from src.translator import DeepSeekTranslator, TranslatorError
    slept = []

    def handler(*a, **k):
        raise requests.RequestException("boom")

    tr = DeepSeekTranslator(api_key="k", timeout=5, max_retries=3)
    monkeypatch.setattr(tr.session, "post", handler)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    with pytest.raises(TranslatorError):
        tr._chat([{"role": "user", "content": "hi"}])
    assert slept == [1, 2], "3 次尝试之间只需 2 次退避"


# ---------------------------------------------------------------------------
# 收尾（强化重试 / 质量重译）在工作线程里并行
# ---------------------------------------------------------------------------

def test_quality_fixes_run_in_parallel(gloss):
    """回归：定向重译原先在主线程收结果时**串行**发请求，命中多的文档被拖成
    单线程。两段各自触发重译时，两次重译必须能同时在途——用 Barrier 卡住：
    串行执行的话第一个会等满超时、重译失败。"""
    import threading
    gate = threading.Barrier(2, timeout=5)

    class Fixer(BaseTranslator):
        def _translate_batch(self, batch, gb):
            return ["译"] * len(batch)            # 过短 → 必触发质量重译

        def _translate_fix(self, text, problem, gb):
            gate.wait()                           # 两次重译同时到场才放行
            return "我们比较了多个课堂中的三种教学方法，以仔细评估学习成果。" * 2

    src = ["We compared three instructional methods across many classrooms "
           f"to evaluate learning outcomes in study number {i} carefully."
           for i in range(2)]
    t = Fixer(batch_size=1, max_workers=2)
    out = t.translate_texts(src, gloss)
    assert t.quality_flags == 2 and t.quality_fixed == 2, "两次重译应并行完成"
    assert all(o.startswith("我们比较了") for o in out)


def test_ph_failure_falls_back_and_is_counted(gloss):
    class Lossy(StubTranslator):
        def _translate_strict(self, text, glossary_block):
            self.strict_calls += 1
            return "还是丢了占位符"

    src = ["公式 ⟦F1⟧ 在此"]
    t = Lossy(batch_size=1, bad_placeholder=True)
    assert t.translate_texts(src, gloss) == src, "占位符对不上 → 保留原文"
    assert t.ph_failures == 1 and t.strict_calls == 1


# ---------------------------------------------------------------------------
# _chat：异常响应与限流
# ---------------------------------------------------------------------------

class _NullResp(_Resp):
    def json(self):
        return {"choices": [{"message": {"content": None}}]}


def test_null_content_is_a_clear_error(monkeypatch):
    """200 但正文为 null / 缺 choices：旧代码抛裸 KeyError/AttributeError，
    联通性预检里用户只看到一句 "'choices'"。"""
    from src.translator import TranslatorError

    def handler(*a, **k):
        return _NullResp(200, '{"choices":[{"message":{"content":null}}]}')

    tr = _client(monkeypatch, handler, max_retries=2)
    with pytest.raises(TranslatorError, match="空内容"):
        tr._chat([{"role": "user", "content": "hi"}])


def test_429_honors_retry_after(monkeypatch):
    import requests  # noqa: F401
    from src.translator import DeepSeekTranslator
    slept, calls = [], []

    class R429(_Resp):
        headers = {"Retry-After": "7"}

    def handler(*a, **k):
        calls.append(1)
        return R429(429, "rate limited") if len(calls) == 1 else _Resp(200)

    tr = DeepSeekTranslator(api_key="k", timeout=5, max_retries=3)
    monkeypatch.setattr(tr.session, "post", handler)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    assert tr._chat([{"role": "user", "content": "hi"}]) == "你好"
    assert slept == [7.0], "限流应按 Retry-After 等待，而不是 1 秒后再撞一次"


def test_local_service_needs_no_key():
    """回归：Ollama 等本地服务不需要 Key。联通性预检给了占位能过，真正翻译
    时 make_translator 却报"未提供 API Key"——界面上写着"无需 Key"却跑不起来。"""
    from src.config import Config
    from src.pipeline import make_translator
    cfg = Config(api_key="", base_url="http://127.0.0.1:11434/v1",
                 model="qwen2.5:7b", use_cache=False)
    tr = make_translator(cfg)
    assert tr.base_url == "http://127.0.0.1:11434/v1"


def test_remote_service_still_requires_key():
    from src.config import Config
    from src.pipeline import make_translator
    from src.translator import TranslatorError
    with pytest.raises(TranslatorError, match="API Key"):
        make_translator(Config(api_key="", use_cache=False))
