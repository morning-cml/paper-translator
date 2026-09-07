"""翻译流水线：解析 → 翻译 → 回填。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Tuple

from .config import Config
from .glossary import Glossary
from .pdf_parser import parse_pdf
from .pdf_writer import build_output
from .textfix import pangu
from .translator import BaseTranslator, DeepSeekTranslator, MockTranslator, TranslatorError


def pick_backend(cfg: Config) -> str:
    """选择回填后端：auto 优先 PyMuPDF，未安装则 reportlab；可强制指定。"""
    choice = (getattr(cfg, "render_backend", "auto") or "auto").lower()
    if choice == "reportlab":
        return "reportlab"
    try:
        from . import pdf_writer_fitz
        if pdf_writer_fitz.available():
            return "pymupdf"
    except Exception:  # noqa: BLE001
        pass
    if choice == "pymupdf":
        raise TranslatorError(
            "已指定 PyMuPDF 后端，但当前环境未安装 pymupdf。"
            "请运行：pip install pymupdf fonttools")
    return "reportlab"

ProgressCB = Callable[[str, float], None]
CancelCB = Callable[[], bool]


class CancelledError(Exception):
    """用户取消翻译。"""


def _has_cjk(text: Optional[str]) -> bool:
    return bool(text) and any(
        "㐀" <= c <= "鿿" or "豈" <= c <= "﫿" for c in text)


def _translated(src: str, tgt: Optional[str], cfg: Config) -> bool:
    """B5：模型是否真的产出了目标语译文（取代旧的仅判 CJK）。"""
    from .languages import is_translated
    return is_translated(src or "", tgt or "",
                         getattr(cfg, "target_lang", "zh") or "zh")


def _maybe_pangu(text: str, cfg: Config) -> str:
    """仅当目标语为中文时套用中西文加空格。"""
    from .languages import uses_pangu
    return pangu(text) if uses_pangu(getattr(cfg, "target_lang", "zh")) else text


# ---------------------------------------------------------------------------
# T12 跨栏段落重排：被栏边/页边腰斩的段落配成同一翻译单元
# ---------------------------------------------------------------------------

_TERMINAL = set("。．.!?！？;；:：…")


def _is_body(b) -> bool:
    """正文块才参与跨栏缝合：标题/OCR/表格单元格都不是连续行文。

    **含行内公式的块一律不缝合**：⟦Fn⟧ 的编号是**块内局部**的，两块拼成一个
    翻译单元后编号会撞车（a 的 ⟦F1⟧ 与 b 的 ⟦F1⟧ 同时出现）；而 `_split_translation`
    只按字符比例找句读处切，根本不认占位符归属。结果是译文里的占位符可能落到
    另一块名下——那一块要么按自己的 formulas 贴出**错误的公式图**，要么查不到
    该编号而**静默丢掉公式**（原文已被抹除，公式就此消失）。
    缝合只是让被腰斩的句子翻得更连贯，代价不值得；这类段落各自单独翻译即可。
    """
    return (b.translatable and not getattr(b, "bold", False)
            and not getattr(b, "from_ocr", False)
            and not getattr(b, "cell_rect", None)
            and not getattr(b, "formulas", None)
            and len(b.text or "") > 60)


def _continues(a, b) -> bool:
    """a 尾不带终止标点 且 b 以小写字母开头 → 视为同一段被腰斩。"""
    ta = (a.text or "").rstrip("'\"’”)]） ")
    tb = (b.text or "").lstrip()
    if not ta or not tb:
        return False
    return ta[-1] not in _TERMINAL and tb[0].islower()


def _make_units(layouts, blocks) -> list:
    """把 blocks 组成翻译单元（多数单块；被腰斩的相邻块配成一对）。"""
    in_set = set(map(id, blocks))
    pair_next: dict = {}
    consumed: set = set()

    def cols(L):
        mid = L.width / 2
        body = [b for b in L.blocks if id(b) in in_set and _is_body(b)
                and b.top < 0.93 * L.height]   # 排除页脚（不以句号结尾易误配）
        left = [b for b in body if b.x1 <= mid + 10 and b.x0 < mid - 40]
        right = [b for b in body if b.x0 >= mid - 10]
        return left, right

    for L in layouts:
        left, right = cols(L)
        if left and right:   # 同页：左栏尾 → 右栏首
            a = max(left, key=lambda x: x.bottom)
            b = min(right, key=lambda x: x.top)
            if id(a) not in consumed and id(b) not in consumed and _continues(a, b):
                pair_next[id(a)] = b
                consumed.update((id(a), id(b)))
    for L, Ln in zip(layouts, layouts[1:]):   # 跨页：本页尾 → 次页首
        lcols = cols(L)
        tail_pool = lcols[1] or lcols[0]
        ncols = cols(Ln)
        head_pool = ncols[0] or ncols[1]
        if not tail_pool or not head_pool:
            continue
        a = max(tail_pool, key=lambda x: x.bottom)
        b = min(head_pool, key=lambda x: x.top)
        if id(a) not in consumed and id(b) not in consumed and _continues(a, b):
            pair_next[id(a)] = b
            consumed.update((id(a), id(b)))

    units, skip = [], set()
    for b in blocks:
        if id(b) in skip:
            continue
        nxt = pair_next.get(id(b))
        if nxt is not None:
            units.append([b, nxt])
            skip.add(id(nxt))
        else:
            units.append([b])
    return units


def _unit_text(unit) -> str:
    if len(unit) == 1:
        return unit[0].text
    a, b = unit[0].text, unit[1].text
    if a.endswith("-") and len(a) > 1 and a[-2].isalpha():
        return a[:-1] + b          # 连字符断词：直接拼接
    return a + " " + b


def _split_translation(tr: str, len_a: int, len_b: int):
    """按源长度比例在最近的句读处把译文拆回两块。"""
    if not tr:
        return tr, ""
    idx = max(1, min(len(tr) - 1, round(len(tr) * len_a / max(len_a + len_b, 1))))
    span = max(6, len(tr) // 4)
    lo, hi = max(1, idx - span), min(len(tr) - 1, idx + span)
    best = None
    for prefer in ("。；！？!?;", "，、,"):
        cands = [i + 1 for i in range(lo, hi) if tr[i] in prefer]
        if cands:
            best = min(cands, key=lambda i: abs(i - idx))
            break
    cut = best if best is not None else idx
    return tr[:cut], tr[cut:]


def make_translator(cfg: Config, mock: bool = False,
                    doc_context: str = "") -> BaseTranslator:
    if mock:
        return MockTranslator(batch_size=cfg.batch_size, max_workers=cfg.max_workers)
    persist = None
    scope = ""
    if getattr(cfg, "use_cache", True):
        import hashlib

        from .paths import user_path
        from .transcache import TransCache
        ctx_h = hashlib.sha1(doc_context.encode("utf-8")).hexdigest()[:10]
        # 语言对进 scope：换目标语不得命中旧译文缓存
        langs = f"{getattr(cfg, 'source_lang', 'auto')}>{getattr(cfg, 'target_lang', 'zh')}"
        scope = f"{cfg.model}|{langs}|{getattr(cfg, 'domain', '')}|{ctx_h}"
        # 缓存写用户目录：打包后临时解包目录会被清空，缓存必须持久保留
        persist = TransCache(user_path("cache", "translations.json"))
    tr = DeepSeekTranslator(
        api_key=cfg.api_key,
        model=cfg.model,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        source_lang=cfg.source_lang,
        target_lang=cfg.target_lang,
        batch_size=cfg.batch_size,
        max_workers=cfg.max_workers,
        proxy=cfg.proxy,
        use_system_proxy=cfg.use_system_proxy,
        verify_ssl=cfg.verify_ssl,
        thinking=cfg.thinking,
        persist=persist,
        cache_scope=scope,
        domain=getattr(cfg, "domain", "计算机科学"),
        doc_context=doc_context,
    )
    # 强制重译：无视旧缓存重新翻译，但把新结果写回覆盖旧的（修复坏缓存）
    tr.cache_refresh = bool(getattr(cfg, "refresh_cache", False))
    return tr


def _doc_context(layouts) -> str:
    """T6：取 p1 最大字号可译块（标题）+ 首个长段（摘要）作全文语境。"""
    if not layouts:
        return ""
    first = [b for b in layouts[0].blocks if b.translatable and b.text]
    if not first:
        return ""
    title = max(first, key=lambda b: b.size).text[:150]
    abstract = next((b.text for b in first if len(b.text) > 300), "")
    ctx = title
    if abstract:
        ctx += "\n摘要节选：" + abstract[:250]
    return ctx


def build_doc_glossary(translator, texts, glossary: Glossary, cfg: Config,
                       report: Optional[ProgressCB] = None,
                       frac: float = 0.09) -> Glossary:
    """全文术语自动抽取 → 一次性译出 → 并入本次使用的术语库。

    对齐 BabelDOC 的 auto-extract glossary。要解决的问题很具体：静态 CSV 只有
    通用术语，而每篇论文都有自己的核心说法；它们是**逐批**送译的，同一个词
    这批译成"生成性失败"、下一批译成"有效失败"，读者在同一篇文章里看到三种
    叫法。BabelDOC 把 terminology consistency 单列为指标，人工评分 4.47 对
    PDFMathTranslate 的 3.34，差距正在这里。

    成本：抽取纯本地不花钱；译术语表**全篇只发一次请求**，且结果进持久缓存，
    重跑同一篇零额外开销。任何一步失败都原样退回旧术语库，绝不影响主流程。
    """
    if not texts or not getattr(cfg, "auto_glossary", True):
        return glossary
    if not getattr(translator, "supports_terms", False):
        return glossary          # Mock 等不具备该能力，连抽取都不必跑
    from .glossary import auto_extract_terms
    limit = int(getattr(cfg, "auto_glossary_limit", 30) or 30)
    terms = auto_extract_terms(texts, limit=limit, known=glossary.entries)
    if not terms:
        return glossary
    try:
        pairs = translator.translate_terms(terms)
    except Exception:  # noqa: BLE001 — 术语表是增强项，失败不该连累翻译
        pairs = {}
    if not pairs:
        return glossary
    if report:
        report(f"本文术语表：自动抽取并统一 {len(pairs)} 条术语"
               f"（如 {'、'.join(list(pairs)[:3])}）", frac)
    path = (getattr(cfg, "save_glossary_path", "") or "").strip()
    if path:
        _save_terms_csv(path, pairs, report, frac)
    return glossary.merged_with(pairs)


def _save_terms_csv(path: str, pairs: dict, report=None, frac: float = 0.09) -> None:
    """把自动抽取的术语表存成 CSV，格式与 glossary/cs_terms.csv 一致。

    存下来是为了**能被人接管**：机器抽的术语表未必条条精准，导出成同格式的
    CSV，用户挑一挑就能并进自己的静态库，下次连那一次请求都省了。
    """
    import csv as _csv
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["en", "zh", "note"])
            for en, zh in sorted(pairs.items()):
                w.writerow([en, zh, "auto"])
        if report:
            report(f"本文术语表已导出：{p}", frac)
    except Exception as e:  # noqa: BLE001
        if report:
            report(f"术语表导出失败（不影响翻译）：{e}", frac)


def _estimate_line(translator, texts, cfg: Config) -> str:
    """T5：请求前成本预估（剔除缓存命中；粗估 token，可选换算金额）。"""
    todo = translator.pending_texts(texts)
    chars = sum(len(t) for t in todo)
    tok_in = int(chars / 3.6 * 1.18)          # 英文 ≈3.6 字符/词元 + 提示词开销
    tok_out = int(tok_in * 1.1)               # 中文译文略长
    line = (f"预计需请求 {len(todo)} 段（缓存命中 "
            f"{len(list(dict.fromkeys(texts))) - len(todo)} 段）"
            f"，约 {(tok_in + tok_out) / 10000:.1f} 万 tokens")
    pin, pout = getattr(cfg, "price_in", 0.0), getattr(cfg, "price_out", 0.0)
    if pin > 0 or pout > 0:
        cost = tok_in / 1e6 * pin + tok_out / 1e6 * pout
        line += f"（约 ¥{cost:.2f}）"
    return line


TEXT_EXTS = (".md", ".markdown", ".txt", ".srt")
SUPPORTED_EXTS = (".pdf", ".docx", ".pptx") + TEXT_EXTS


def translate_document(input_path: str, output_path: str, cfg: Config, **kw) -> dict:
    """按扩展名分派到对应格式的翻译器。

    各入口（CLI / 网页版）统一调用本函数，新增格式只需在此登记一处。
    """
    ext = Path(input_path).suffix.lower()
    if ext == ".docx":
        from .docx_translator import translate_docx
        return translate_docx(input_path, output_path, cfg, **kw)
    if ext == ".pptx":
        from .pptx_translator import translate_pptx
        return translate_pptx(input_path, output_path, cfg, **kw)
    if ext in TEXT_EXTS:
        from .text_translator import translate_text_file
        return translate_text_file(input_path, output_path, cfg, **kw)
    if ext in (".doc", ".ppt"):
        raise TranslatorError(
            f"旧版 {ext} 格式不支持，请先用 Office 另存为 {ext}x 后再翻译。")
    return translate_pdf(input_path, output_path, cfg, **kw)


def output_suffix(mode: str, ext: str = ".pdf") -> str:
    """统一的输出文件名后缀规则（各入口共用，避免命名不一致）。

    只有 PDF 有"左右对照"的版面概念；其余格式的双语一律是"原文+译文"并排
    成段/成行，统称 bilingual。
    """
    if ext.lower() != ".pdf":
        return "_translation_bilingual" if mode != "translated" else "_translation"
    return {"bilingual": "_translation_bilingual",
            "sidebyside": "_translation_sidebyside",
            "updown": "_translation_updown"}.get(mode, "_translation")


class PageRangeError(ValueError):
    """页码范围写法不合法（面向用户的中文提示）。"""


def parse_page_range(spec: str, n_pages: int) -> set:
    """把 "1-3,5,8-" 这类页码范围解析成**0 起**的页号集合（页码本身 1 起）。

    语法与同类工具（BabelDOC 的 `--pages`）对齐，四种写法：
        "5"     单页          "3-7"   闭区间
        "8-"    从第 8 页到末尾   "-4"    从第 1 页到第 4 页
    逗号分隔可任意组合；重叠与乱序都允许（结果取并集）。

    存在的意义：原有的"试译前 N 页"只能从头数，而真实需求常是"只要方法和
    结果那几页""跳过前面的封面"，以及**大文档分批翻译**（先 1-50，再 51-100，
    已译段走缓存不重复计费）——同类工具用 `--max-pages-per-part` 自动分段，
    我们这条流水线本就是逐页流式处理、每批落盘可续跑，把范围交给用户点名
    更直接，也不必引入拆分/合并文档那套额外机器。
    """
    out: set = set()
    if not spec or n_pages <= 0:
        return out
    for raw in spec.replace("，", ",").split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo_s, hi_s = part.split("-", 1)
                lo = int(lo_s) if lo_s.strip() else 1
                hi = int(hi_s) if hi_s.strip() else n_pages
            else:
                lo = hi = int(part)
        except ValueError:
            raise PageRangeError(
                f"页码范围「{part}」看不懂。正确写法如：1-3,5,8- （逗号分隔，"
                "支持单页 / 区间 / 开口区间）") from None
        if lo < 1 or hi < 1:
            raise PageRangeError(f"页码从 1 起，收到「{part}」")
        if lo > hi:
            lo, hi = hi, lo
        out.update(range(lo - 1, min(hi, n_pages)))
    if not out:
        raise PageRangeError(
            f"页码范围「{spec}」在这份 {n_pages} 页的文档里一页都选不中")
    return out


_KNOWN_SERVICES = {
    "api.deepseek.com": "DeepSeek",
    "api.moonshot.cn": "Kimi（月之暗面）",
    "open.bigmodel.cn": "智谱 GLM",
    "volces.com": "豆包（火山方舟）",
    "api.openai.com": "OpenAI",
}


def _is_local_url(base_url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")


def service_label(base_url: str) -> str:
    """从接口地址推断服务名，用于给用户看的错误提示（"连不上 DeepSeek"）。"""
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or base_url or "").lower()
    if _is_local_url(base_url):
        return "本地服务（Ollama？）"
    for key, name in _KNOWN_SERVICES.items():
        if host == key or host.endswith("." + key) or key in host:
            return name
    return host or "翻译服务"


def check_connection(cfg: Config) -> Tuple[bool, str]:
    """翻译前的**必做**联通性预检：验证能连上服务、Key/模型是否可用。

    返回 (是否成功, 面向用户的中文提示)。失败信息会点名具体服务，便于用户
    立刻判断是"网络连不上"还是"Key 无效/模型不对"。
    """
    label = service_label(cfg.base_url)
    local = _is_local_url(cfg.base_url)
    if not cfg.api_key and not local:
        return False, f"尚未填写 {label} 的 API Key，无法翻译。请先填写或勾选离线测试。"
    try:
        tr = DeepSeekTranslator(
            # 本地服务（Ollama）通常不需要 Key，给个占位避免构造报错
            api_key=cfg.api_key or ("local" if local else ""),
            model=cfg.model, base_url=cfg.base_url,
            temperature=cfg.temperature, max_retries=1, timeout=30,
            proxy=cfg.proxy, use_system_proxy=cfg.use_system_proxy,
            verify_ssl=cfg.verify_ssl, thinking=cfg.thinking,
        )
        out = tr._chat([
            {"role": "system", "content": "You are a translator."},
            {"role": "user", "content": "把 'hello' 翻译成中文，只输出译文。"},
        ])
        return True, f"✔ 已连接 {label}（模型 {cfg.model}）。返回示例：{out.strip()[:20]}"
    except TranslatorError as e:
        return False, f"✘ 无法使用 {label}：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"✘ 连接 {label} 失败：{e}"


def translate_pdf(
    input_path: str,
    output_path: str,
    cfg: Config,
    translator: Optional[BaseTranslator] = None,
    glossary: Optional[Glossary] = None,
    mock: bool = False,
    progress: Optional[ProgressCB] = None,
    should_cancel: Optional[CancelCB] = None,
) -> dict:
    def report(msg: str, frac: float):
        if progress:
            progress(msg, max(0.0, min(1.0, frac)))

    def check_cancel():
        if should_cancel and should_cancel():
            raise CancelledError("已取消。")

    check_cancel()
    report("正在解析 PDF…", 0.02)
    layouts = parse_pdf(
        input_path,
        progress=lambda msg, frac: report(msg, 0.02 + 0.06 * frac),
        formula_font_pattern=getattr(cfg, "formula_font_pattern", "") or "",
        formula_char_pattern=getattr(cfg, "formula_char_pattern", "") or "")
    blocks = [b for layout in layouts for b in layout.blocks if b.translatable]
    # 选页：page_range（"1-3,5,8-" 任意页码）优先，其次 T3 试译前 N 页。
    # 两者都是"只翻一部分、其余保留原文"，page_range 只是把范围说得更细。
    max_pages = int(getattr(cfg, "max_pages", 0) or 0)
    spec = (getattr(cfg, "page_range", "") or "").strip()
    wanted = parse_page_range(spec, len(layouts)) if spec else None
    if wanted is not None:
        blocks = [b for b in blocks if b.page_index in wanted]
    elif max_pages > 0:
        blocks = [b for b in blocks if b.page_index < max_pages]
    # T12 跨栏段落重排：被腰斩段配成同一翻译单元（整段送译，译后按比例拆回）
    units = _make_units(layouts, blocks)
    n_pairs = sum(1 for u in units if len(u) > 1)
    texts = [_unit_text(u) for u in units]
    n_ocr = sum(1 for L in layouts if any(b.from_ocr for b in L.blocks))
    if wanted is not None:
        trial = f"（只翻第 {spec} 页，共 {len(wanted)} 页）"
    else:
        trial = f"（试译前 {max_pages} 页）" if max_pages > 0 else ""
    pair_note = f"，跨栏/跨页续段配对 {n_pairs} 组" if n_pairs else ""
    if n_ocr:
        report(f"共 {len(layouts)} 页（其中 {n_ocr} 页扫描版经 OCR 识别）、"
               f"{len(texts)} 个待译段落{trial}{pair_note}", 0.08)
    else:
        report(f"共 {len(layouts)} 页、{len(texts)} 个待译段落{trial}{pair_note}", 0.08)

    if glossary is None:
        glossary = Glossary.load(cfg.resolved_glossary_path())
    if translator is None:
        translator = make_translator(cfg, mock=mock,
                                     doc_context=_doc_context(layouts))
    # 本文术语表（自动抽取 + 统一译名）：必须在翻译开始前建好，全篇共用
    glossary = build_doc_glossary(translator, texts, glossary, cfg, report)
    if texts and not mock:
        try:
            report(_estimate_line(translator, texts, cfg), 0.09)   # T5 成本预估
        except Exception:  # noqa: BLE001
            pass

    def tcb(done: int, total: int):
        check_cancel()  # 每批之间检查取消
        report(f"正在翻译… 第 {done}/{total} 批", 0.10 + 0.80 * (done / max(total, 1)))

    if texts:
        translations = translator.translate_texts(texts, glossary, tcb,
                                                  should_cancel=should_cancel)
        translations = [_maybe_pangu(tr, cfg) for tr in translations]  # 中文才加空格
        kept = 0
        for unit, tr in zip(units, translations):
            # 只重排真正翻译了的内容（商业文档翻译工具的最小干预原则）：
            # 译文不是目标语 = 模型原样退回（引用/URL/人名/占位符回退等）
            # → 保留原文原排版，不抹除不重排。
            if not _translated(unit[0].text, tr, cfg):
                for b in unit:
                    b.translation = None
                kept += len(unit)
            elif len(unit) == 1:
                unit[0].translation = tr
            else:
                a, b = unit
                ca, cb = _split_translation(tr, len(a.text), len(b.text))
                a.translation, b.translation = ca, cb
        if kept:
            report(f"{kept} 段未产生译文（引用/专名等），保留原文原排版", 0.90)
        hits = getattr(translator, "cache_hits", 0)
        if hits:
            report(f"持久缓存命中 {hits} 段，未重复计费", 0.90)
        failed = getattr(translator, "failed_texts", 0)
        if failed:
            report(f"注意：{failed} 段因网络/服务错误未翻译，已保留原文——"
                   "重新运行本任务即可补齐（已译段走缓存不重复计费）", 0.90)
        flags = getattr(translator, "quality_flags", 0)
        if flags:
            fixed = getattr(translator, "quality_fixed", 0)
            report(f"译文自检：{flags} 段发现异常（漏译/数字错漏等），"
                   f"重译修正 {fixed} 段", 0.90)
        ph_failed = getattr(translator, "ph_failures", 0)
        if ph_failed:
            report(f"提示：{ph_failed} 段公式占位符校验未通过，已保留原文以保住公式位置", 0.90)
    elif any(L.needs_ocr for L in layouts):
        # 提示必须分场景：打包版用户没有 pip，让他"pip install"是死路一条，
        # 正确出路是换完整版（full）。源码运行才适用装依赖。
        from .paths import is_frozen
        how = ("请换用**完整版（full）**，精简版（lite）不含扫描版识别组件"
               if is_frozen() else
               "请运行：pip install rapidocr-onnxruntime 后重试")
        report(f"未提取到可翻译文字：这是扫描版 PDF，且缺少 OCR 组件。{how}", 0.9)
    else:
        report("未提取到可翻译文字（可能是扫描版 PDF）", 0.9)

    check_cancel()
    backend = pick_backend(cfg)
    label = "PyMuPDF 精确抹除" if backend == "pymupdf" else "reportlab 覆盖（兜底）"
    report(f"正在生成译文 PDF…（回填后端：{label}）", 0.93)

    # 原子写出：先写 .part，成功才落位。任何失败/取消都不会在最终路径留下
    # 半截损坏的 PDF（那正是"重译同一篇却生成不出来/打不开"的元凶之一）。
    from .paths import atomic_output
    with atomic_output(output_path) as _out:
        if backend == "pymupdf":
            from . import pdf_writer_fitz
            try:
                pdf_writer_fitz.build_output(
                    input_path, _out.tmp, layouts, cfg.output_mode,
                    font_path=getattr(cfg, "font_path", ""))
            except pdf_writer_fitz.BackendUnsupported as e:
                report(f"PyMuPDF 后端不适用（{e}），回退 reportlab…", 0.94)
                backend = "reportlab"
            except Exception as e:  # noqa: BLE001
                if (getattr(cfg, "render_backend", "auto") or "auto") == "pymupdf":
                    raise  # 用户强制指定时不静默回退
                report(f"PyMuPDF 后端异常（{e}），回退 reportlab…", 0.94)
                backend = "reportlab"
        if backend == "reportlab":
            build_output(input_path, _out.tmp, layouts, cfg.output_mode)
    output_path = _out.path   # 目标被占用时可能已自动改名
    report("完成", 1.0)

    return {
        "pages": len(layouts),
        "blocks": len(blocks),
        "output": output_path,
        "mode": cfg.output_mode,
        "backend": backend,
    }
