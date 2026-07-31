"""译文回填 —— PyMuPDF (fitz) 首选后端：精确抹除 + CJK 嵌入 + 公式矢量回贴。

相对 reportlab 覆盖方案的本质优势：
  · **真正抹除**原文 glyph（redaction），不画白框——深色/带底纹背景零露白，
    输出 PDF 的文字层干净（原文字符被删除而非被盖住）；
  · 中文字体**嵌入**输出文件（内置 CJK 或 fonts/ 目录下的思源字体），
    任何阅读器渲染一致；
  · 行内公式用 `show_pdf_page` 从原始副本**矢量回贴**，无限清晰、无白底；
  · 排版复用 layout.py（与兜底后端行为一致）。

版本兼容：核心 API（add_redact_annot / apply_redactions / insert_text /
show_pdf_page / insert_font）自 PyMuPDF 1.18 起稳定；较新的可选参数
（fill=False、graphics=...、subset_fonts）逐级 try 降级。
坐标系：fitz 与 pdfplumber 同为「左上角原点、y 向下」，无需翻转。
旋转页（page.rotation != 0）暂不支持精确路径——检测到即抛
BackendUnsupported，由 pipeline 整体回退 reportlab 后端。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from .layout import (collect_avoid_rects, compute_target_box, layout_block,
                     ocr_line_shape_avoids)
from .pdf_parser import PageLayout

_ASCENT = 0.85     # 基线相对字号的近似上高（与兜底后端一致）
_ERASE_PAD_X = 0.5
_ERASE_PAD_Y = 0.2
_CLIP_PAD = 1.0    # 公式矢量回贴的裁剪外扩

# 内置 CJK 字体（MuPDF 自带，无需外部文件）。
# ⚠️ 名不副实，别被 "china-ss" 骗了：`fitz.Font("china-ss")`（测宽用）拿到的是
# **Droid Sans Fallback Regular**——单一字重、无粗体、非宋体；而 insert_text 走
# 内置名时嵌入的又是 Song CID 字体。测宽与绘制本来就不是同一套字形，且**没有
# 任何粗体可用**，所以标题只能靠描边合成——汉字笔画密，描粗必糊。
# 想要真正的层级，就得往 fonts/ 放外置字体（见 fonts/生成字体.py）。
_BUILTIN_CJK = "china-ss"
# ASCII 专用西文字体（Base-14，比例宽度）。注意：insert_text 的内置
# "china-ss" 实际嵌入 Song CID 字体，其 ASCII 字形是**全宽 1em**，与
# fitz.Font("china-ss") 的比例测宽不一致——ASCII 必须单独用西文字体
# 测宽并绘制，否则绘制比排版预留宽约一倍，导致压字/叠印/整块溢出。
_LATIN = "helv"


def _script_runs(text: str):
    """把文本切成 (is_ascii, 段) 序列：ASCII 与非 ASCII 分属不同字体。"""
    runs = []
    cur = ""
    cur_ascii = None
    for ch in text:
        a = ord(ch) < 128
        if cur_ascii is None or a == cur_ascii:
            cur += ch
            cur_ascii = a
        else:
            runs.append((cur_ascii, cur))
            cur, cur_ascii = ch, a
    if cur:
        runs.append((cur_ascii, cur))
    return runs

from .paths import data_file, user_dir

ROOT = user_dir()   # 用户可把字体放进用户数据目录的 fonts/


class BackendUnsupported(Exception):
    """当前文件/环境不适用本后端，应回退 reportlab。"""


def available() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def find_font_file(explicit: str = "") -> Optional[str]:
    """定位外置中文字体：显式路径 > fonts/ 目录（偏好思源/Noto 简体）。"""
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = data_file(*Path(explicit).parts)
        if p.exists():
            return str(p)
    fdir = data_file("fonts")
    if not fdir.is_dir():
        return None
    cands = [p for p in sorted(fdir.iterdir())
             if p.suffix.lower() in (".ttf", ".otf")]
    if not cands:
        return None

    def rank(p: Path):
        n = p.name.lower()
        score = 0
        for i, kw in enumerate(("sc", "cn", "han", "song", "serif", "hei", "sans")):
            if kw in n:
                score -= (10 - i)
        return score

    return str(sorted(cands, key=rank)[0])


_BOLD_NAME = re.compile(r"bold|black|heavy|semibold|demibold|[-_]b\b", re.I)


class FontSet(NamedTuple):
    """正文与标题各一套字面。中文排版惯例：正文宋体、标题黑体。"""
    body_file: Optional[str]     # None = 用 MuPDF 内置
    bold_file: Optional[str]     # None = 没有真粗体，退回描边合成

    @property
    def body_name(self) -> str:
        return "zhBody" if self.body_file else _BUILTIN_CJK

    @property
    def bold_name(self) -> str:
        return "zhBold" if self.bold_file else self.body_name

    @property
    def synth_bold(self) -> bool:
        """没有真粗体字面时才描边合成——那是下策，只作兜底。"""
        return self.bold_file is None


def find_font_pair(explicit: str = "") -> FontSet:
    """挑出「正文字体 + 标题粗体」两个文件。

    显式指定 --font 时只覆盖正文，粗体仍从 fonts/ 里找：用户通常只想换正文
    字体，不该因此丢掉标题层级。
    """
    fdir = data_file("fonts")
    bold = None
    if fdir.is_dir():
        bolds = [p for p in sorted(fdir.iterdir())
                 if p.suffix.lower() in (".ttf", ".otf") and _BOLD_NAME.search(p.name)]
        if bolds:
            bold = str(bolds[0])

    body = find_font_file(explicit)
    # find_font_file 不区分字重，可能正好挑中粗体文件；那样正文会整篇变粗。
    if body and bold and Path(body).name == Path(bold).name:
        alts = [p for p in sorted(fdir.iterdir())
                if p.suffix.lower() in (".ttf", ".otf")
                and not _BOLD_NAME.search(p.name)]
        body = str(alts[0]) if alts else None
    return FontSet(body_file=body, bold_file=bold)


def _make_measure(fontfile: Optional[str]) -> Callable[[str, float], float]:
    """测宽函数。**必须与绘制用的字体完全一致**，否则排版预留与实际绘制对不上，
    轻则压字、重则整块溢出。"""
    import fitz
    try:
        if fontfile:
            # 外置的思源/Noto 是泛 CJK 字体，**自带比例西文字形**，中西文同一
            # 套字面即可（实测 "Abc"@10pt = 18.9pt，是比例宽而非全宽 1em）。
            # 这样译文里的英文与中文风格一致，不再是宋体配 Helvetica 的拼接感。
            f = fitz.Font(fontfile=fontfile)
            return lambda t, s: f.text_length(t, fontsize=s)
        # 内置字体：ASCII 字形是全宽 1em，与比例测宽差近一倍，必须拆开算。
        f_cjk = fitz.Font(_BUILTIN_CJK)
        f_lat = fitz.Font(_LATIN)

        def measure(t: str, s: float) -> float:
            return sum((f_lat if is_a else f_cjk).text_length(seg, fontsize=s)
                       for is_a, seg in _script_runs(t))
        return measure
    except Exception:  # noqa: BLE001
        # 极端兜底：按 CJK=1em / ASCII=0.5em 估宽
        return lambda t, s: sum(s * (0.5 if ord(c) < 128 else 1.0) for c in t)


def _heading_start_sizes(layouts: List[PageLayout],
                         measure: Callable[[str, float], float]) -> dict:
    """让**原文字号相同的标题**在译文里也保持相同字号。

    起因（实测）：`layout_block` 是每块**独立**缩字号去凑合塞进框的——译文长了
    就 0.5pt 往下退。ACM acmart 的一级与二级标题原文都是 10.9pt，译文里却缩成了
    10.9 和 8.9。层级看着有了，其实是随机的：**两个同级标题一样会缩成不同大小**，
    那才是真正让人觉得"不齐整"的地方。

    做法：先空跑一遍所有标题块，按原始字号分组（0.5pt 归一），取组内**众数**
    作为该组所有标题的起始字号。**不做 H1/H2/H3 语义推断**——那需要一堆启发式
    且容易错，而"原文一样大的，译文也一样大"既简单又忠于原排版。

    ⚠️ 取众数而非最小值：最小值会被一个特别挤的标题拖垮全组。实测某篇论文的
    35 个章节标题分布在 {10.9:6, 10.4:17, 9.9:10, 9.4:1, 8.9:1}，取最小值会把
    全部 35 个压到 8.9，与图表题注（8.97）挤成一团，层级反而没了。众数 10.4
    只让最大的 6 个降 4.6%（看不出来），却把主体拉齐。

    空跑不带 placed（同页已放置块），得到的字号可能略大于实跑；实跑仍允许继续
    缩，真放不下的标题因此可能再小一档。宁可如此，也不为了统一而让标题压字。
    """
    from collections import Counter

    groups: dict = {}
    for L in layouts:
        for b in L.blocks:
            if not (b.translatable and b.translation and getattr(b, "bold", False)):
                continue
            fdims = {f.idx: (f.width + 2.0, f.height + 2.0) for f in b.formulas}
            box = compute_target_box(b, L.blocks, L.obstacles, L.height)
            avoid = list(collect_avoid_rects(b, L.blocks, L.obstacles))
            laid = layout_block(b.translation, fdims, box,
                                min(max(b.size, 5.0), 20.0), measure,
                                avoid=avoid, min_size=5.0,
                                align=getattr(b, "align", "left"))
            groups.setdefault(round(b.size * 2) / 2, []).append((id(b), laid.font_size))

    forced = {}
    for members in groups.values():
        hist = Counter(round(s, 1) for _, s in members)
        # 众数；并列时取较大的那个（宁大勿小，缩小是不可逆的观感损失）
        top = max(hist.items(), key=lambda kv: (kv[1], kv[0]))[0]
        for bid, _ in members:
            forced[bid] = top
    return forced


# 正文里的引用标记："[41]"、"[46, 82]"、"[25–27]"。只认方括号内清一色是
# 数字与分隔符的串，避免把 "[见附录]" 这类也当成引用。
_CITE_GROUP = re.compile(r"\[[\d\s,;–—-]+\]")
_NUM = re.compile(r"\d+")
# 链接源文字是否像"引用片段"：原文里一条链接常只盖住 "[46," 或 "82]" 半截。
# 允许句点：链接框常紧贴句末的 "."，而句点很窄，稍有重叠就会被算进来，
# 于是文字成了 "[99]."。不放行的话这一类会被整批拒掉（实测占被拒的绝大多数）。
_CITE_FRAG = re.compile(r"^[\[\]\d.,;\s–—-]+$")


def _snapshot_links(page) -> tuple:
    """抹除**之前**把页面链接存下来。

    起因：原文（LaTeX hyperref 生成）里正文的 "[41]" 本来就链到参考文献条目，
    实测某篇论文前 6 页就有 100 条。而 `apply_redactions` 会**连带删除与抹除区
    重叠的注释**——译文输出里 111 条只剩 12 条，等于我们把原有的超链接毁掉了。

    所以要做的不是"实现超链接"，而是**别弄丢，并且跟着重排后的译文重新锚定**。

    返回 (原始链接列表, {引用号: 链接字典})。
    """
    import fitz
    links = page.get_links()
    cand = [lk for lk in links
            if lk.get("kind") in (fitz.LINK_GOTO, fitz.LINK_NAMED)
            and lk.get("page", -1) >= 0]
    cites: dict = {}
    if not cand:
        return links, cites

    # 整页词表只取一次。逐个链接调 page.get_textbox() 会把**整页重新解析一遍**，
    # 而一页上百个引用链接就是上百次全页解析——构建时间会成倍膨胀。
    words = page.get_text("words")   # (x0, y0, x1, y1, word, block, line, wordno)
    for lk in cand:
        lx0, ly0, lx1, ly1 = lk["from"]
        # 要求词的**多半宽度**落在链接框内。只判"有重叠"的话，一个只盖住
        # "[41]" 的窄框会把紧挨着的邻词整个拉进来，拼出 "the [41] of" 这种串，
        # 下面的 _CITE_FRAG 当场判否——实测因此漏掉了大半引用链接。
        parts = []
        for w in words:
            if not (w[1] < ly1 and w[3] > ly0):
                continue
            ov = min(w[2], lx1) - max(w[0], lx0)
            if ov > 0.5 * max(w[2] - w[0], 0.01):
                parts.append(w[4])
        txt = " ".join(parts).strip()
        if not txt or len(txt) > 10 or not _CITE_FRAG.match(txt):
            continue
        for m in _NUM.finditer(txt):
            cites.setdefault(int(m.group()), lk)
    return links, cites


def _cite_rects(text: str, x: float, y_top: float, h: float, size: float,
                measure: Callable[[str, float], float]) -> list:
    """在一段**已绘制**的译文里定位引用编号，返回 [(编号, 矩形)]。

    位置靠对前缀重新测宽得到——测宽函数与绘制用的是同一套字体，所以算出来的
    x 与实际字形位置一致（这也是 measure 必须按 正文/粗体 分开的原因之一）。
    """
    out = []
    for g in _CITE_GROUP.finditer(text):
        for m in _NUM.finditer(g.group()):
            i, j = g.start() + m.start(), g.start() + m.end()
            x0 = x + measure(text[:i], size)
            x1 = x + measure(text[:j], size)
            if x1 > x0:
                out.append((int(m.group()), (x0, y_top, x1, y_top + h)))
    return out


def _relink(page, links, cites, blocks, new_cites) -> None:
    """恢复链接：没动过的原样放回，重排过的按译文新位置重新锚定。"""
    import fitz
    erased = [fitz.Rect(r) for b in blocks for r in b.line_rects]
    alive = {(round(fitz.Rect(l["from"]).x0, 1), round(fitz.Rect(l["from"]).y0, 1))
             for l in page.get_links()}

    # ① 落在未抹除区域的链接：文字没变，原样恢复（作者 ORCID、DOI、外链等）
    for lk in links:
        r = fitz.Rect(lk["from"])
        if (round(r.x0, 1), round(r.y0, 1)) in alive:
            continue                      # 还活着，别插重复的
        if any(r.intersects(e) for e in erased):
            continue                      # 原文已被抹掉，位置作废
        try:
            page.insert_link(lk)
        except Exception:  # noqa: BLE001
            pass

    # ② 被重排的引用编号：锚到译文里的新位置，目标沿用原链接（精确到条目）
    seen = set()
    for num, rect in new_cites:
        lk = cites.get(num)
        if lk is None:
            continue
        key = (num, round(rect[0], 1), round(rect[1], 1))
        if key in seen:
            continue
        seen.add(key)
        d = {"kind": fitz.LINK_GOTO, "from": fitz.Rect(rect), "page": lk["page"]}
        if lk.get("to") is not None:
            d["to"] = lk["to"]
        try:
            page.insert_link(d)
        except Exception:  # noqa: BLE001
            pass


def _redact_page(page, blocks) -> None:
    import fitz
    added = 0
    for b in blocks:
        if getattr(b, "from_ocr", False):
            continue   # 扫描页无文字层可抹，改在 _draw_page 里白底覆盖
        for (x0, top, x1, bottom) in b.line_rects:
            rect = fitz.Rect(x0 - _ERASE_PAD_X, top - _ERASE_PAD_Y,
                             x1 + _ERASE_PAD_X, bottom + _ERASE_PAD_Y)
            if rect.is_empty or not rect.is_valid:
                continue
            try:
                page.add_redact_annot(rect, fill=False)   # 不填充 → 背景零露白
            except Exception:  # noqa: BLE001
                page.add_redact_annot(rect)               # 旧版：默认白填充
            added += 1
    if not added:
        return
    img_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
    try:
        page.apply_redactions(
            images=img_none,
            graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0))
    except TypeError:
        try:
            page.apply_redactions(images=img_none)   # 图片一律保留
        except TypeError:
            page.apply_redactions()


def _draw_page(page, layout: PageLayout, blocks, src_doc,
               fonts: "FontSet",
               measures: dict, forced_sizes: Optional[dict] = None) -> None:
    import fitz
    for fname, ffile in ((fonts.body_name, fonts.body_file),
                         (fonts.bold_name, fonts.bold_file)):
        try:
            if ffile:
                page.insert_font(fontname=fname, fontfile=ffile)
            else:
                page.insert_font(fontname=fname)  # fontname 为内置保留名
        except Exception:  # noqa: BLE001
            pass  # insert_text 时仍可用内置名自动加载

    placed: List[tuple] = []   # 本页已放置译文/公式的矩形，后续块逐行避让
    new_cites: List[tuple] = []   # 译文里引用编号的新位置，供 _relink 重新锚定
    for b in blocks:
        if getattr(b, "from_ocr", False):
            # 扫描页：原文是图像像素，先用白底盖住原文各行再写译文
            for (x0, top, x1, bottom) in b.line_rects:
                r = fitz.Rect(x0 - 1.0, top - 0.6, x1 + 1.0, bottom + 0.6)
                if not r.is_empty and r.is_valid:
                    page.draw_rect(r, color=None, fill=(1, 1, 1))
        is_bold = bool(getattr(b, "bold", False))
        # 真粗体字面优先；没有才退回描边合成。测宽必须跟着换，否则排版按
        # 正文宽度预留、实际用更宽的粗体绘制，标题会压到下一行。
        use_bold_face = is_bold and fonts.bold_file is not None
        fontname = fonts.bold_name if use_bold_face else fonts.body_name
        measure = measures[use_bold_face]
        active_file = fonts.bold_file if use_bold_face else fonts.body_file
        fdims = {f.idx: (f.width + 2.0, f.height + 2.0) for f in b.formulas}
        box = compute_target_box(b, layout.blocks, layout.obstacles, layout.height)
        avoid = list(collect_avoid_rects(b, layout.blocks, layout.obstacles))
        if getattr(b, "from_ocr", False):
            avoid += ocr_line_shape_avoids(b, box)
        start_size = min(max(b.size, 5.0), 20.0)
        # 标题：改用「同原始字号组」统一的起始字号，见 _heading_start_sizes
        if forced_sizes:
            start_size = forced_sizes.get(id(b), start_size)
        # 表格单元格空间紧、不可越格，允许缩得更小以塞进本格
        min_size = 4.0 if getattr(b, "cell_rect", None) else 5.0
        laid = layout_block(b.translation or "", fdims, box, start_size,
                            measure, avoid=avoid + placed, min_size=min_size,
                            align=getattr(b, "align", "left"))
        placed.extend((it.x, it.y_top, it.x + it.w, it.y_top + it.h)
                      for it in laid.items)
        color = tuple(getattr(b, "color", (0, 0, 0)) or (0, 0, 0))
        # 描边合成加粗**只在没有真粗体字面时**才用：汉字笔画本就密，把笔画描
        # 粗会糊成一团，真黑体是重新设计的笔形而不是把宋体加粗。
        bold_kw = (dict(render_mode=2, fill=color, border_width=0.045)
                   if (is_bold and not use_bold_face) else {})
        frects = {f.idx: (f.x0, f.top, f.x1, f.bottom) for f in b.formulas}
        for it in laid.items:
            if it.kind == "text":
                baseline = it.y_top + 0.5 * (it.h - it.size) + _ASCENT * it.size
                new_cites.extend(_cite_rects(it.text, it.x, it.y_top, it.h,
                                             it.size, measure))
                if active_file:
                    # 外置字体自带比例西文字形，中西文同一套字面，整串一次画完。
                    page.insert_text((it.x, baseline), it.text, fontname=fontname,
                                     fontsize=it.size, color=color, **bold_kw)
                else:
                    # 内置字体的 ASCII 是全宽 1em，必须换西文字体单独画，
                    # 否则绘制宽度约为排版预留的两倍，直接压字/溢出。
                    x = it.x
                    for is_a, seg in _script_runs(it.text):
                        page.insert_text((x, baseline), seg,
                                         fontname=_LATIN if is_a else fontname,
                                         fontsize=it.size, color=color, **bold_kw)
                        x += measure(seg, it.size)
            else:
                r = frects.get(it.fidx)
                if r is None:
                    continue
                clip = fitz.Rect(r[0] - _CLIP_PAD, r[1] - _CLIP_PAD,
                                 r[2] + _CLIP_PAD, r[3] + _CLIP_PAD)
                target = fitz.Rect(it.x, it.y_top, it.x + it.w, it.y_top + it.h)
                if target.is_empty or clip.is_empty:
                    continue
                try:
                    page.show_pdf_page(target, src_doc, layout.page_index,
                                       clip=clip)
                except Exception:  # noqa: BLE001
                    pass  # 单个公式回贴失败不影响整页
    return new_cites


def build_output(input_path: str, output_path: str,
                 layouts: List[PageLayout], mode: str = "translated",
                 font_path: str = "") -> None:
    import fitz

    doc = fitz.open(input_path)   # 工作文档：redact + 写中文
    src = fitz.open(input_path)   # 原始副本：公式矢量回贴来源

    try:
        for page in doc:
            if getattr(page, "rotation", 0):
                raise BackendUnsupported(
                    f"第 {page.number + 1} 页含旋转（rotation="
                    f"{page.rotation}），PyMuPDF 精确路径暂不支持")

        fonts = find_font_pair(font_path)
        # 两套测宽：正文一套、标题粗体一套。粗体字面的西文比正文宽，
        # 共用一套测宽会让标题排版失准。
        measures = {
            False: _make_measure(fonts.body_file),
            True: _make_measure(fonts.bold_file or fonts.body_file),
        }
        forced_sizes = _heading_start_sizes(layouts,
                                            measures[fonts.bold_file is not None])

        for layout in layouts:
            if layout.page_index >= len(doc):
                continue
            page = doc[layout.page_index]
            blocks = [b for b in layout.blocks if b.translatable and b.translation]
            if not blocks:
                continue
            # 链接必须在抹除**之前**快照：apply_redactions 会连带删掉与抹除区
            # 重叠的注释，原文正文里的 "[41] → 参考文献" 会成片消失。
            links, cites = _snapshot_links(page)
            _redact_page(page, blocks)
            new_cites = _draw_page(page, layout, blocks, src, fonts, measures,
                                   forced_sizes)
            _relink(page, links, cites, blocks, new_cites)

        if mode == "bilingual":
            out = fitz.open()
            for i in range(len(doc)):
                out.insert_pdf(src, from_page=i, to_page=i)   # 原文页
                out.insert_pdf(doc, from_page=i, to_page=i)   # 译文页
            _subset_fonts(out)
            out.save(output_path, garbage=3, deflate=True)
            out.close()
        elif mode == "updown":
            # 上下对照：W×2H 长页，上原文下译文。
            # 存在的理由是**可读性**：左右对照是 2W 宽页，在「适合宽度」下只能
            # 缩到单页的一半（实测 1.47× vs 2.94×），字小到看不清，而这是它的
            # 物理上限、调不动。上下拼保持页宽不变，适合宽度下就是 100%。
            out = fitz.open()
            for i in range(len(doc)):
                r = src[i].rect
                w, h = r.width, r.height
                page = out.new_page(width=w, height=2 * h)
                page.show_pdf_page(fitz.Rect(0, 0, w, h), src, i)          # 上：原文
                page.show_pdf_page(fitz.Rect(0, h, w, 2 * h), doc, i)      # 下：译文
                page.draw_line(fitz.Point(0, h), fitz.Point(w, h),
                               color=(0.8, 0.8, 0.8), width=0.7)
            _subset_fonts(out)
            out.save(output_path, garbage=3, deflate=True)
            out.close()
        elif mode == "sidebyside":
            # T4 左右对照：2W×H 宽页，左原文右译文，中缝细分隔线
            out = fitz.open()
            for i in range(len(doc)):
                r = src[i].rect
                w, h = r.width, r.height
                page = out.new_page(width=2 * w, height=h)
                page.show_pdf_page(fitz.Rect(0, 0, w, h), src, i)
                page.show_pdf_page(fitz.Rect(w, 0, 2 * w, h), doc, i)
                page.draw_line(fitz.Point(w, 0), fitz.Point(w, h),
                               color=(0.8, 0.8, 0.8), width=0.7)
            _subset_fonts(out)
            out.save(output_path, garbage=3, deflate=True)
            out.close()
        else:
            _subset_fonts(doc)
            doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()
        src.close()


def _subset_fonts(doc) -> None:
    """裁剪嵌入字体子集，显著减小文件体积（需 fontTools；失败不致命）。"""
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001
        pass
