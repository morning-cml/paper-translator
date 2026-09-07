"""翻译客户端。

- DeepSeekTranslator：调用 DeepSeek 的 OpenAI 兼容 /chat/completions 接口。
- MockTranslator：离线占位翻译，用于无网络/无密钥时测试整条流水线。

对外主入口：translate_texts(texts, glossary, progress_cb)
    自动去重、按段落批量、并发请求、缓存、注入术语库。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

import requests

from .glossary import Glossary


class TranslatorError(Exception):
    pass


_MARKER = re.compile(r"\[\[(\d+)\]\]\s*(.*?)(?=\[\[\d+\]\]|\Z)", re.DOTALL)

# 行内公式占位符（与 pdf_parser.PLACEHOLDER_FMT 一致）
_PH = re.compile(r"⟦F\d+⟧")
_PH_NOTE = (
    "注意：文中形如 ⟦F1⟧、⟦F2⟧ 的标记是公式占位符，"
    "必须原样保留在译文的对应位置，不得翻译、改写、合并或遗漏。\n\n"
)


def _ph_ok(src: str, tgt: str) -> bool:
    """译文中的公式占位符必须与原文完全一致（集合与个数）。"""
    return sorted(_PH.findall(src)) == sorted(_PH.findall(tgt or ""))


class BaseTranslator:
    def __init__(self, batch_size: int = 12, max_workers: int = 4,
                 persist=None, cache_scope: str = ""):
        self.batch_size = max(1, batch_size)
        self.max_workers = max(1, max_workers)
        self._cache: dict[str, str] = {}
        self.ph_failures = 0    # 占位符校验失败（已回退原文）的段数
        self.failed_texts = 0   # 网络/服务失败降级（已回退原文）的段数（T2）
        self.cache_hits = 0     # 持久缓存命中段数（T1）
        self.quality_flags = 0  # 质量自检命中并触发重译的段数（B3）
        self.quality_fixed = 0  # 重译后通过自检的段数（B3）
        # T1 持久化缓存：persist 为 TransCache 实例；scope 参与键（模型/领域/上下文）
        self.persist = persist
        self.cache_scope = cache_scope
        # 强制重译：跳过缓存**读取**（无视旧译文），但仍**写入**——用新结果覆盖
        # 上次可能不对的旧缓存。对应"上次翻得不好，重译却因缓存还是坏的"。
        self.cache_refresh = False
        # B5 多语言：目标语提示词名（"简体中文"/"英语"…）与源/目标代码（质量长度带用）
        self.target_name = "简体中文"
        self.source_code = "auto"
        self.target_code = "zh"

    def _persist_key(self, text: str) -> str:
        from .transcache import TransCache
        return TransCache.make_key(self.cache_scope, text)

    # 子类若能一次性译出术语表则置 True（Mock 与纯本地实现不支持）
    supports_terms = False

    def translate_terms(self, terms: List[str]) -> dict:
        """把「本文术语表」一次性译出，返回 {英文小写: 译文}。默认不支持。"""
        return {}

    # 子类可实现「占位符丢失后的强化重试」；返回 None 表示不支持
    def _translate_strict(self, text: str, glossary_block: str) -> Optional[str]:
        return None

    # 子类可实现「质量自检未过后的定向重译」；返回 None 表示不支持
    def _translate_fix(self, text: str, problem: str,
                       glossary_block: str) -> Optional[str]:
        return None

    def _quality_pass(self, src: str, tgt: str, glossary) -> str:
        """B3：本地规则自检，命中则定向重译一次，取更好的那个。

        重译结果必须同时通过「占位符校验」与「质量自检」才采纳；否则保留
        首次译文（它通常仍可用，强行替换反而可能更差）。
        """
        from .quality import check, describe
        issues = check(src, tgt, self.source_code, self.target_code)
        if not issues:
            return tgt
        self.quality_flags += 1
        try:
            fixed = self._translate_fix(src, describe(issues),
                                        glossary.prompt_block([src]))
        except Exception:  # noqa: BLE001
            fixed = None
        if fixed and _ph_ok(src, fixed) \
                and not check(src, fixed, self.source_code, self.target_code):
            self.quality_fixed += 1
            return fixed
        return tgt

    # 子类实现：翻译一批段落，返回与输入等长的译文列表
    def _translate_batch(self, batch: List[str], glossary_block: str) -> List[str]:
        raise NotImplementedError

    def _cache_lookup(self, text: str) -> Optional[str]:
        """持久缓存命中值——但**占位符必须仍与原文一致**才采纳。

        缓存损坏（手工误改、旧版本写入、磁盘半写）时，返回的译文可能公式占位符
        对不上；直接用会让写回端把公式贴错位或丢失。这类命中一律作废、重新翻译，
        使缓存具备自愈能力（对应"重译同一篇因缓存反而生成不出来"的反馈）。
        """
        if self.persist is None or self.cache_refresh:
            return None
        hit = self.persist.get(self._persist_key(text))
        if hit is not None and _ph_ok(text, hit):
            return hit
        return None

    def pending_texts(self, texts: List[str]) -> List[str]:
        """去重并剔除内存/持久缓存命中后，真正需要请求的段（供成本预估）。"""
        unique = list(dict.fromkeys(texts))
        todo = []
        for t in unique:
            if t in self._cache:
                continue
            if self._cache_lookup(t) is not None:
                continue
            todo.append(t)
        return todo

    def translate_texts(
        self,
        texts: List[str],
        glossary: Glossary,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        cancelled = should_cancel or (lambda: False)
        # 去重（保留首次出现顺序）；先查内存缓存，再查持久缓存（T1，含占位符校验）
        unique = list(dict.fromkeys(texts))
        todo = []
        for t in unique:
            if t in self._cache:
                continue
            hit = self._cache_lookup(t)
            if hit is not None:
                self._cache[t] = hit
                self.cache_hits += 1
                continue
            todo.append(t)
        batches = [todo[i:i + self.batch_size] for i in range(0, len(todo), self.batch_size)]
        done = 0
        total = len(batches)

        def run(batch):
            # 取消后仍在队列里、尚未启动的批：直接原样返回，绝不再发起请求。
            # 这是"取消能立刻见效"的关键——否则所有批已 submit，线程池会把它们
            # 全部跑完才肯收摊（旧代码用 with 退出即 shutdown(wait=True)）。
            if cancelled():
                return list(batch)
            block = glossary.prompt_block(batch)
            return self._translate_batch(batch, block)

        if batches:
            ex = ThreadPoolExecutor(max_workers=self.max_workers)
            try:
                futures = {ex.submit(run, b): b for b in batches}
                for fut in as_completed(futures):
                    if cancelled():
                        break   # 立即停止收结果，剩余批交给 finally 里的取消
                    batch = futures[fut]
                    # T2 失败降级：单批彻底失败 → 本批各段回退原文并继续，
                    # 绝不让 90% 进度的任务整体报废；失败段不写缓存，重跑可补。
                    try:
                        results = fut.result()
                    except Exception:  # noqa: BLE001
                        for src in batch:
                            self._cache[src] = src
                        self.failed_texts += len(batch)
                        done += 1
                        if progress_cb:
                            progress_cb(done, total)
                        continue
                    for src, tgt in zip(batch, results):
                        if _ph_ok(src, tgt):
                            self._store(src, self._quality_pass(src, tgt, glossary))
                            continue
                        # 占位符丢失/变形 → 单段强化重试一次
                        retry = None
                        try:
                            retry = self._translate_strict(src, glossary.prompt_block([src]))
                        except Exception:  # noqa: BLE001
                            retry = None
                        if retry is not None and _ph_ok(src, retry):
                            self._store(src, self._quality_pass(src, retry, glossary))
                        else:
                            # 保底：保留原文，公式位置绝不丢失；不写持久缓存
                            self.ph_failures += 1
                            self._cache[src] = src
                    if self.persist is not None:
                        self.persist.flush()   # 每批落盘，中途崩溃不丢已译段
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
            finally:
                # wait=False + cancel_futures：不等在途请求跑完、丢弃未启动的批，
                # 使取消立刻返回（Python 3.9+）。正常跑完时所有批已 done，无副作用。
                ex.shutdown(wait=False, cancel_futures=True)
        if self.persist is not None:
            self.persist.flush()
        return [self._cache.get(t, t) for t in texts]

    def _store(self, src: str, tgt: str) -> None:
        self._cache[src] = tgt
        if self.persist is not None and tgt and tgt != src:
            self.persist.put(self._persist_key(src), tgt)


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一名资深学术翻译，专注于{domain}领域，负责将{src}准确翻译成{tgt}。"
    "要求：\n"
    "1. 译文专业、准确、通顺，使用学术书面语。\n"
    "2. 保留数学公式、变量符号、代码片段、以及行内缩写和专有名词"
    "（如 CNN、GPU、arXiv、URL、引用编号 [12] 等）不翻译。\n"
    "3. 不翻译作者姓名、机构名；参考文献中的原文文献标题保持原样。\n"
    "4. 不要添加解释、注释或原文，只输出译文本身。\n"
    "5. 必须严格遵守用户给出的术语对照表。\n"
    "6. 文中形如 ⟦F1⟧、⟦F2⟧ 的标记是公式占位符：必须原样保留在译文的对应位置，"
    "不得翻译、改写、合并或遗漏。"
)


class DeepSeekTranslator(BaseTranslator):
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com",
        temperature: float = 1.0,
        source_lang: str = "auto",
        target_lang: str = "zh",
        batch_size: int = 12,
        max_workers: int = 4,
        timeout: int = 120,
        max_retries: int = 3,
        proxy: str = "",
        use_system_proxy: bool = True,
        verify_ssl: bool = True,
        thinking: bool = False,
        persist=None,
        cache_scope: str = "",
        domain: str = "计算机科学",
        doc_context: str = "",
    ):
        super().__init__(batch_size=batch_size, max_workers=max_workers,
                         persist=persist, cache_scope=cache_scope)
        if not api_key:
            raise TranslatorError("未提供 DeepSeek API Key。")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify_ssl = verify_ssl
        self.thinking = thinking  # 开=质量优先（更准更慢），关=提速；应用默认开（见 config.py），此参数默认关仅为直接构造时的保守回退
        # B5 多语言：source_lang/target_lang 为语言代码（auto/en/zh/ja…）
        from . import languages as _L
        self.source_code = source_lang or "auto"
        self.target_code = target_lang or "zh"
        self.target_name = _L.target_prompt_name(self.target_code)
        self.system_prompt = SYSTEM_PROMPT.format(
            src=_L.source_prompt_name(self.source_code),
            tgt=self.target_name, domain=domain or "计算机科学")
        if doc_context:
            # T6 全文上下文：标题+摘要片段进 system prompt，各批口径一致
            self.system_prompt += (
                "\n\n本文档主题背景（仅供理解翻译口径，禁止输出到译文中）：\n"
                + doc_context[:400])
        # 独立 Session：可选择是否走系统/环境代理，或指定代理
        self.session = requests.Session()
        self.session.trust_env = use_system_proxy
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        # 连接池随并发扩大，避免高并发时连接排队
        pool = max(10, max_workers + 2)
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ---- 底层 API 调用 ----
    def _chat(self, messages) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
            # 关闭思考链（默认开启会先生成推理再翻译，非常慢）
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        last_err = None
        attempt = 0
        while attempt < self.max_retries:
            try:
                resp = self.session.post(url, headers=headers, json=payload,
                                         timeout=self.timeout, verify=self.verify_ssl)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                # 兼容非 DeepSeek 服务（Kimi/GLM/豆包/OpenAI/Ollama…）：
                # "thinking" 是 DeepSeek 风格参数，严格校验的服务会报 400
                # （OpenAI 明确回 "Unrecognized request argument supplied: thinking"），
                # 去掉该字段立刻重试一次。
                #
                # ⚠️ 这次重试**不计入重试次数**。计入的话，联通性预检（用的是
                # max_retries=1）会把仅有的一次机会花在注定被拒的首个请求上，
                # 剥掉字段后的重试根本发不出去 → 预检必然失败在"翻译请求失败。"
                # 上。而预检是翻译的强制闸门，等于整个工具对所有严格校验参数的
                # 服务（OpenAI 等）完全不可用。
                # 本分支至多触发一次：剥掉字段后条件不再成立，不会死循环。
                if resp.status_code == 400 and "thinking" in payload:
                    payload = {k: v for k, v in payload.items() if k != "thinking"}
                    continue
                # 明确的客户端错误不重试
                if resp.status_code in (401, 402, 403):
                    raise TranslatorError(
                        f"翻译服务返回 {resp.status_code}：{_short(resp.text)}"
                        "（请检查 API Key 是否正确、账户余额是否充足、模型是否已开通）"
                    )
                last_err = TranslatorError(f"HTTP {resp.status_code}: {_short(resp.text)}")
            except requests.exceptions.SSLError as e:
                last_err = TranslatorError(
                    "SSL 连接被中断，通常是 VPN/加速器/代理或安全软件拦截了 HTTPS。\n"
                    "可尝试：① 关闭 VPN/加速器后重试；② 在 config.json 设置 "
                    '"use_system_proxy": false 直连；③ 暂时关闭安全软件的 HTTPS/SSL 扫描。\n'
                    f"（原始错误：{e}）")
            except requests.RequestException as e:
                last_err = TranslatorError(f"网络请求失败：{e}")
            attempt += 1
            if attempt < self.max_retries:
                time.sleep(2 ** (attempt - 1))  # 指数退避；最后一次失败不再空等
        raise last_err or TranslatorError("翻译请求失败。")

    # ---- 批量翻译 ----
    def _translate_batch(self, batch: List[str], glossary_block: str) -> List[str]:
        if len(batch) == 1:
            return [self._translate_single(batch[0], glossary_block)]
        numbered = "\n\n".join(f"[[{i + 1}]] {t}" for i, t in enumerate(batch))
        instruction = (
            f"下面有 {len(batch)} 个段落，每段前有形如 [[序号]] 的编号。"
            f"请把每个段落翻译成{self.target_name}，并按相同编号输出，格式：\n"
            "[[1]] 译文\n[[2]] 译文\n"
            "务必保证编号数量与输入完全一致，且只输出译文。\n\n"
        )
        if any("⟦F" in t for t in batch):
            instruction += _PH_NOTE
        if glossary_block:
            instruction += glossary_block + "\n\n"
        instruction += "待翻译段落：\n" + numbered
        content = self._chat([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ])
        parsed = {int(i): t.strip() for i, t in _MARKER.findall(content)}
        if len(parsed) == len(batch) and all((i + 1) in parsed for i in range(len(batch))):
            return [parsed[i + 1] for i in range(len(batch))]
        # 编号错位 → 退化为逐段翻译，保证对齐
        return [self._translate_single(t, glossary_block) for t in batch]

    def _translate_single(self, text: str, glossary_block: str) -> str:
        instruction = ""
        if "⟦F" in text:
            instruction += _PH_NOTE
        if glossary_block:
            instruction += glossary_block + "\n\n"
        instruction += f"把下面这段文字翻译成{self.target_name}，只输出译文：\n\n" + text
        return self._chat([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ]).strip()

    # ---- 本文术语表：一次调用译出，全篇共用 ----
    supports_terms = True
    _TERMS_TAG = "\x00doc-glossary\x00"

    def translate_terms(self, terms: List[str]) -> dict:
        """把抽取出的本文术语一次性译出，返回 {英文小写: 译文}。

        全篇只发**一次**请求，结果进持久缓存（键含术语表本身），所以重跑同一篇
        文档零额外开销。译不出或对不上号的条目直接丢弃——术语表宁缺毋滥，
        塞一条错译进提示词会让全篇跟着错。
        """
        if not terms:
            return {}
        key = self._persist_key(self._TERMS_TAG + "\n".join(terms))
        if self.persist is not None and not self.cache_refresh:
            hit = self.persist.get(key)
            if hit:
                try:
                    cached = json.loads(hit)
                    if isinstance(cached, dict):
                        return cached
                except Exception:  # noqa: BLE001
                    pass          # 缓存坏了就重新译，不让它拖垮翻译
        numbered = "\n".join(f"[[{i + 1}]] {t}" for i, t in enumerate(terms))
        instruction = (
            f"下面是同一篇文档里反复出现的 {len(terms)} 个关键术语，每行前有 "
            f"[[序号]]。请给出每个术语在本文语境下最恰当的"
            f"{self.target_name}译名，按相同编号逐行输出，格式：\n"
            "[[1]] 译名\n[[2]] 译名\n"
            "只输出译名本身，不要解释、不要重复原文。"
            "已成惯例的缩写（CNN、GPU 等）若本就不必翻译，原样输出即可。\n\n"
            + numbered)
        content = self._chat([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ])
        parsed = {int(i): t.strip() for i, t in _MARKER.findall(content)}
        from .languages import is_translated
        out: dict = {}
        for i, term in enumerate(terms):
            zh = parsed.get(i + 1, "").strip()
            # 一行一条，多行说明八成是模型跑题了；且必须真的译成了目标语
            if not zh or "\n" in zh or len(zh) > 40:
                continue
            if not is_translated(term, zh, self.target_code):
                continue
            out[term.lower()] = zh
        if out and self.persist is not None:
            self.persist.put(key, json.dumps(out, ensure_ascii=False))
            self.persist.flush()
        return out

    def _translate_fix(self, text: str, problem: str,
                       glossary_block: str) -> Optional[str]:
        """质量自检未过后的定向重译：把具体问题告诉模型，让它重来一次。"""
        instruction = (
            "上一次翻译存在问题：" + problem + "。\n"
            f"请重新把下面这段文字完整、准确地翻译成{self.target_name}，只输出译文本身，"
            "不要任何说明或解释。\n\n")
        if "⟦F" in text:
            instruction = _PH_NOTE + instruction
        if glossary_block:
            instruction += glossary_block + "\n\n"
        instruction += text
        return self._chat([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ]).strip()

    def _translate_strict(self, text: str, glossary_block: str) -> Optional[str]:
        """占位符校验失败后的强化重试：逐个点名要求保留。"""
        phs = _PH.findall(text)
        instruction = (
            "下面文本包含 " + str(len(phs)) + " 个公式占位符：" + "、".join(phs) +
            f"。把文本翻译成{self.target_name}，译文必须一字不差地保留这些占位符，"
            "缺一个都算错误。只输出译文。\n\n"
        )
        if glossary_block:
            instruction += glossary_block + "\n\n"
        instruction += text
        return self._chat([
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": instruction},
        ]).strip()


def _short(s: str, n: int = 200) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n]


# ---------------------------------------------------------------------------
# 离线 Mock（用于测试）
# ---------------------------------------------------------------------------

_FILLER = "这是用于测试排版与渲染的中文占位译文内容以便验证图片保留与位置对齐效果"


class MockTranslator(BaseTranslator):
    """不联网的假翻译：套用术语库译法，并生成长度大致相当的中文，便于测试。"""

    def _translate_batch(self, batch: List[str], glossary_block: str) -> List[str]:
        # 从对照表提示块里解析出 en→zh，便于在占位译文中体现术语
        term_map = {}
        for line in glossary_block.splitlines():
            m = re.match(r"-\s*(.+?)\s*→\s*(.+)", line.strip())
            if m:
                term_map[m.group(1).lower()] = m.group(2)
        out = []
        for text in batch:
            phs = _PH.findall(text)
            plain = _PH.sub("", text)
            hits = [zh for en, zh in term_map.items() if re.search(
                r"(?<![a-z0-9])" + re.escape(en) + r"(?![a-z0-9])", plain.lower())]
            n_words = max(1, len(plain.split()))
            target_len = int(n_words * 1.6)
            body = (_FILLER * (target_len // len(_FILLER) + 1))[:target_len]
            prefix = ("【术语：" + "、".join(dict.fromkeys(hits)) + "】") if hits else ""
            # 占位符按原顺序均匀插回，模拟真实译文中公式的位置
            if phs:
                step = max(1, len(body) // (len(phs) + 1))
                chunks = []
                pos = 0
                for i, ph in enumerate(phs):
                    nxt = min(len(body), (i + 1) * step)
                    chunks.append(body[pos:nxt])
                    chunks.append(ph)
                    pos = nxt
                chunks.append(body[pos:])
                body = "".join(chunks)
            out.append(prefix + body)
        return out
