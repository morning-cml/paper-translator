"""译文回填 —— reportlab 兜底后端（无 PyMuPDF 时使用）。

相对旧版「整块白框覆盖」的升级：
  · 只遮盖原文**实际行矩形**（line_rects），不再整块盖大白框——
    行内公式区域之外的空白、深色背景的暴露面积都大幅减小；
  · 复用 layout.py 重排：按块宽断行、可向下扩展（避让图表）、字号自适应、
    中文禁则；
  · 行内公式（⟦Fn⟧）用 pdfplumber 渲染的高分辨率位图在新位置回贴；
  · 仍用 reportlab 内置中文字体 STSong-Light + pypdf 合并，零新增依赖。

真正的「精确抹除」（不留白框、深色背景无痕）由 PyMuPDF 后端
（pdf_writer_fitz.py）提供，本模块是环境缺 PyMuPDF 时的兜底。
"""
from __future__ import annotations

from io import BytesIO
from typing import Dict, List, Optional, Tuple

from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .layout import (collect_avoid_rects, compute_target_box, layout_block,
                     ocr_line_shape_avoids)
from .pdf_parser import Block, PageLayout

FONT = "STSong-Light"  # reportlab 内置简体中文字体
_FONT_READY = False

_ASCENT = 0.85         # 基线相对字号的近似上高
_COVER_PAD_X = 1.2
_COVER_PAD_Y = 0.8
_CLIP_DPI = 220        # 公式位图渲染分辨率


def _ensure_font():
    global _FONT_READY
    if not _FONT_READY:
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))
        _FONT_READY = True


def _measure(text: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, FONT, size)


# ---------------------------------------------------------------------------
# 公式位图：整页渲染一次，按公式矩形裁剪
# ---------------------------------------------------------------------------

class _FormulaClipper:
    def __init__(self, input_path: str):
        self._path = input_path
        self._pdf = None
        self._page_images: Dict[int, "object"] = {}

    def _page_image(self, page_index: int):
        if page_index not in self._page_images:
            if self._pdf is None:
                import pdfplumber
                self._pdf = pdfplumber.open(self._path)
            page = self._pdf.pages[page_index]
            self._page_images[page_index] = page.to_image(
                resolution=_CLIP_DPI).original.convert("RGB")
        return self._page_images[page_index]

    def clip(self, page_index: int, rect: Tuple[float, float, float, float]):
        """rect 为 pdfplumber 坐标（左上原点）。返回 PIL 图。"""
        img = self._page_image(page_index)
        s = _CLIP_DPI / 72.0
        pad = 1.0
        x0, top, x1, bottom = rect
        box = (max(0, int((x0 - pad) * s)), max(0, int((top - pad) * s)),
               min(img.width, int((x1 + pad) * s) + 1),
               min(img.height, int((bottom + pad) * s) + 1))
        return img.crop(box)

    def close(self):
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None
        self._page_images.clear()


# ---------------------------------------------------------------------------
# 覆盖层绘制
# ---------------------------------------------------------------------------

def _draw_block(c: canvas.Canvas, b: Block, layout: PageLayout,
                clipper: _FormulaClipper, placed: Optional[list] = None):
    page_h = layout.height
    # 1) 白底遮盖：只盖原文各行的实际矩形
    c.setFillColorRGB(1, 1, 1)
    for (x0, top, x1, bottom) in b.line_rects:
        c.rect(x0 - _COVER_PAD_X, page_h - bottom - _COVER_PAD_Y,
               (x1 - x0) + 2 * _COVER_PAD_X, (bottom - top) + 2 * _COVER_PAD_Y,
               fill=1, stroke=0)

    # 2) 重排译文（逐行避让图片/图表/不可译块）
    fdims = {f.idx: (f.width + 2.0, f.height + 2.0) for f in b.formulas}
    box = compute_target_box(b, layout.blocks, layout.obstacles, page_h)
    avoid = list(collect_avoid_rects(b, layout.blocks, layout.obstacles))
    if getattr(b, "from_ocr", False):
        avoid += ocr_line_shape_avoids(b, box)
    start_size = min(max(b.size, 5.0), 20.0)
    min_size = 4.0 if getattr(b, "cell_rect", None) else 5.0
    laid = layout_block(b.translation or "", fdims, box, start_size, _measure,
                        avoid=avoid + (placed or []), min_size=min_size,
                        align=getattr(b, "align", "left"))
    if placed is not None:   # 记录本块占位，同页后续块逐行避让，杜绝叠印
        placed.extend((it.x, it.y_top, it.x + it.w, it.y_top + it.h)
                      for it in laid.items)

    # 3) 逐项绘制
    r, g, bl = getattr(b, "color", (0, 0, 0)) or (0, 0, 0)
    if (r + g + bl) / 3 > 0.85:   # 兜底后端盖白底，近白文字改深灰保证可读
        r = g = bl = 0.25
    is_bold = getattr(b, "bold", False)   # 章节标题：描边合成加粗
    frects = {f.idx: (f.x0, f.top, f.x1, f.bottom) for f in b.formulas}
    for it in laid.items:
        if it.kind == "text":
            baseline_from_top = it.y_top + 0.5 * (it.h - it.size) + _ASCENT * it.size
            y = page_h - baseline_from_top
            c.setFillColorRGB(r, g, bl)
            c.setFont(FONT, it.size)
            c.drawString(it.x, y, it.text)
            if is_bold:   # 合成加粗：微偏移重描一遍（reportlab 无内置粗宋体）
                off = max(0.2, 0.03 * it.size)
                c.drawString(it.x + off, y, it.text)
        else:
            rect = frects.get(it.fidx)
            if rect is None:
                continue
            pil = clipper.clip(b.page_index, rect)
            c.drawImage(ImageReader(pil), it.x, page_h - (it.y_top + it.h),
                        width=it.w, height=it.h)


def _make_overlay(layout: PageLayout, clipper: _FormulaClipper) -> Optional[BytesIO]:
    blocks = [b for b in layout.blocks if b.translatable and b.translation]
    if not blocks:
        return None
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(layout.width, layout.height))
    placed: list = []
    for b in blocks:
        _draw_block(c, b, layout, clipper, placed)
    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# 输出组装
# ---------------------------------------------------------------------------

def build_output(input_path: str, output_path: str,
                 layouts: List[PageLayout], mode: str = "translated") -> None:
    _ensure_font()
    clipper = _FormulaClipper(input_path)
    try:
        writer = PdfWriter()
        reader_main = PdfReader(input_path)
        reader_orig = (PdfReader(input_path)
                       if mode in ("bilingual", "sidebyside", "updown") else None)

        for i, page in enumerate(reader_main.pages):
            layout = layouts[i] if i < len(layouts) else None
            overlay = _make_overlay(layout, clipper) if layout else None

            if mode == "bilingual":
                writer.add_page(reader_orig.pages[i])  # 先放原文页

            if overlay is not None:
                ov_page = PdfReader(overlay).pages[0]
                page.merge_page(ov_page)

            if mode == "sidebyside":
                # T4 左右对照：2W×H 宽页，左贴原文、右贴译文
                from pypdf import Transformation
                w = float(page.mediabox.width)
                h = float(page.mediabox.height)
                wide = writer.add_blank_page(width=2 * w, height=h)
                wide.merge_transformed_page(
                    reader_orig.pages[i], Transformation())
                wide.merge_transformed_page(
                    page, Transformation().translate(tx=w, ty=0))
            elif mode == "updown":
                # 上下对照：W×2H 长页，上原文下译文。页宽不变，「适合宽度」下
                # 仍是 100%——左右对照在同样模式下只有一半，这是它存在的理由。
                from pypdf import Transformation
                w = float(page.mediabox.width)
                h = float(page.mediabox.height)
                tall = writer.add_blank_page(width=w, height=2 * h)
                # PDF 坐标原点在**左下**、y 向上：上半 = ty h~2h，下半 = ty 0~h。
                tall.merge_transformed_page(
                    reader_orig.pages[i], Transformation().translate(tx=0, ty=h))
                tall.merge_transformed_page(page, Transformation())
            else:
                writer.add_page(page)  # 译文页（translated 模式即为唯一页）

        with open(output_path, "wb") as f:
            writer.write(f)
    finally:
        clipper.close()
