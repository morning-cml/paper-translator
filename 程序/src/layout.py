"""共享排版引擎：把译文（含 ⟦Fn⟧ 公式占位符）排进给定区域，支持逐行避障。

两个写回后端（PyMuPDF / reportlab）共用本模块，各自只负责「画」：
  · 输入：译文文本、公式尺寸表、可用矩形（含向下扩展余量）、起始字号、
    避让矩形（图片/矢量图/不可译块），以及后端提供的精确测宽函数
    measure(text, size) -> width。
  · 输出：绝对定位的绘制项列表（text 项 / formula 项），后端逐项绘制即可。

规则：
  · 中文逐字可断行；连续 ASCII 串（英文词/数字/URL）与公式占位符不拆断。
  · 行首禁则：句读/闭括号等不出现在行首（悬挂到上一行行尾）。
  · 行尾禁则：开括号/开引号不悬在行尾（挪到下一行）。
  · 公式项按原始尺寸参与排版，垂直方向与本行文字居中对齐。
  · **逐行避障**：每行的可用水平区间 = 目标框减去与该行竖直相交的避让
    矩形（取最宽剩余区间）。区间过窄则跳过该障碍带继续向下。用于处理
    「照片嵌入栏内、文字绕图」的版式。
  · 放不下时逐级缩小字号（0.5pt 步进）直到 min_size；仍放不下则在框底
    之下继续排（宁可溢出，不丢内容），并标记 overflow。

坐标系与解析层一致：左上角原点，top 向下增大。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

PLACEHOLDER_RE = re.compile(r"⟦F(\d+)⟧")

# 行首禁则（不得作为行首）与行尾禁则（不得作为行尾）
_NO_LINE_START = set("。，、；：？！）】》〉」』”’％%‰℃…·—～!,.;:?)]}>")
_NO_LINE_END = set("（【《〈「『“‘([{<")

Rect = Tuple[float, float, float, float]  # (x0, top, x1, bottom)

# 一行文字自身占的高度（相对字号）：汉字字面约 1em，留一点给下伸部与舍入。
# 只用于「框底之下还放不放得下一行」的判断，不影响行与行的实际间距。
_INK_H = 1.12


@dataclass
class Item:
    """一个绘制项。text 项：draw text at (x, y_top, size)；
    formula 项：把编号 fidx 的公式区域贴到 (x, y_top, w, h)。"""
    kind: str          # "text" | "formula"
    x: float
    y_top: float
    w: float
    h: float
    text: str = ""
    fidx: int = 0
    size: float = 0.0


@dataclass
class LaidBlock:
    items: List[Item] = field(default_factory=list)
    font_size: float = 0.0
    leading: float = 0.0
    used_height: float = 0.0
    overflow: bool = False


# ---------------------------------------------------------------------------
# 切分排版单元
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[Tuple[str, object]]:
    """切成排版单元：("ph", idx) 公式占位符；("tok", str) 文本单元。
    文本单元 = 单个非 ASCII 字符 / 连续 ASCII 非空白串 / 单个空格。"""
    units: List[Tuple[str, object]] = []
    pos = 0
    for m in PLACEHOLDER_RE.finditer(text):
        _split_text_units(text[pos:m.start()], units)
        units.append(("ph", int(m.group(1))))
        pos = m.end()
    _split_text_units(text[pos:], units)
    return units


def _char_breakable(ch: str) -> bool:
    """该字符可单独成行（逐字断行）：仅 CJK 表意文字与日文假名。

    其余一律并入"词"（不在词中断行）——这对拉丁字母、**带重音符的欧洲语言**
    （德语 ä/ö/ü/ß、法语 é/à…，它们非 ASCII 但绝不能在词中断开）、西里尔、
    韩文谚文都成立。中文行为不变（表意文字仍逐字可断）。
    """
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF          # CJK 表意
            or 0x3040 <= o <= 0x30FF)         # 日文假名


def _split_text_units(seg: str, out: List[Tuple[str, object]]):
    buf = ""
    for ch in seg:
        if ch.isspace():
            if buf:
                out.append(("tok", buf))
                buf = ""
            out.append(("tok", " "))
        elif _char_breakable(ch):
            if buf:
                out.append(("tok", buf))
                buf = ""
            out.append(("tok", ch))
        else:
            buf += ch                          # 词形字符（拉丁/重音/西里尔/谚文/数字/标点）
    if buf:
        out.append(("tok", buf))


# ---------------------------------------------------------------------------
# 逐行避障流式排版
# ---------------------------------------------------------------------------

def _line_interval(x0: float, x1: float, y: float, lh: float,
                   avoid: Sequence[Rect]) -> Tuple[float, float]:
    """当前行 [y, y+lh) 的可用水平区间：从 [x0,x1] 中减去竖直相交的避让
    矩形，返回最宽剩余区间（等宽取最左）。"""
    segs = [(x0, x1)]
    for (ax0, atop, ax1, abot) in avoid:
        if abot <= y or atop >= y + lh:
            continue
        nxt = []
        for (sx0, sx1) in segs:
            if ax1 <= sx0 or ax0 >= sx1:
                nxt.append((sx0, sx1))
                continue
            if ax0 - sx0 > 1.0:
                nxt.append((sx0, ax0))
            if sx1 - ax1 > 1.0:
                nxt.append((ax1, sx1))
        segs = nxt
        if not segs:
            return (x0, x0)  # 全被挡住
    best = max(segs, key=lambda s: (round(s[1] - s[0], 2), -s[0]))
    return best


def _skip_band_bottom(x0: float, x1: float, y: float, lh: float,
                      avoid: Sequence[Rect]) -> float:
    """被障碍挡住时，返回可越过障碍的下一个 y。"""
    bots = [abot for (ax0, atop, ax1, abot) in avoid
            if not (abot <= y or atop >= y + lh) and not (ax1 <= x0 or ax0 >= x1)]
    return (min(bots) + 0.8) if bots else (y + lh)


def _flow(units, formulas: Dict[int, Tuple[float, float]], size: float,
          leading: float, box: Rect, avoid: Sequence[Rect],
          measure: Callable[[str, float], float], hard_bottom: bool,
          align: str = "left"):
    """把 units 排进 box（可避障）。返回 (items, end_y, done)。
    hard_bottom=True 时超出 box 底则停止（done=False 表示还有剩余）。"""
    x0, top, x1, bottom_max = box
    min_w = max(24.0, 2.5 * size)
    items: List[Item] = []
    y = top
    i = 0
    n = len(units)

    def width_of(kind, payload) -> float:
        if kind == "ph":
            return formulas.get(payload, (0.0, 0.0))[0]
        return measure(payload, size)

    guard = 0
    while i < n:
        guard += 1
        if guard > 10000:  # 防御：任何异常几何都不允许死循环
            break
        # 竖向能否再放一行：按**这一行自己的墨迹高**判断，而不是整个 leading。
        #
        # leading（1.30×字号）是**行与行之间**的距离；框底之下没有下一行了，
        # 那份行间距根本不会被占用。拿它当判据，等于凭空多要 30% 的高度。
        # 后果实测过：原文一个 10.9pt 的单行标题，墨迹高 11.1pt，译文却因为
        # 「10.9 × 1.30 = 14.2 > 11.1」被判放不下，一路缩到 9.0pt 才过关——
        # **每个单行块都被系统性地缩掉约 20%**，与内容长短无关（那些标题的
        # 译文只有 21.8pt 宽，框却有 100~240pt，宽度从来不是瓶颈）。
        # 章节标题、图表题注、表格短标签全中招，整篇的字号层级因此被压平。
        if hard_bottom and y + size * _INK_H > bottom_max + 0.6:
            return items, y, False
        lx0, lx1 = _line_interval(x0, x1, y, leading, avoid)
        if lx1 - lx0 < min_w:
            ny = _skip_band_bottom(x0, x1, y, leading, avoid)
            y = ny if ny > y + 0.1 else y + leading
            continue

        # ---- 填充一行 ----
        line: List[Tuple[str, object, float]] = []
        wsum = 0.0
        while i < n:
            kind, payload = units[i]
            if kind == "tok" and payload == " " and not line:
                i += 1
                continue  # 行首空格丢弃
            w = width_of(kind, payload)
            if line and wsum + w > (lx1 - lx0) + 0.5:
                # 行首禁则：单个句读悬挂本行行尾
                if (kind == "tok" and isinstance(payload, str)
                        and payload in _NO_LINE_START):
                    line.append((kind, payload, w))
                    wsum += w
                    i += 1
                break
            line.append((kind, payload, w))
            wsum += w
            i += 1
        # 行尾处理：去尾空格；开括号挪下行
        while line and line[-1][0] == "tok" and line[-1][1] == " ":
            wsum -= line[-1][2]
            line.pop()
        if line and line[-1][0] == "tok" and isinstance(line[-1][1], str) \
                and line[-1][1] in _NO_LINE_END and i < n:
            wsum -= line[-1][2]
            line.pop()
            i -= 1
        if not line:
            y += leading
            continue

        # ---- 生成绘制项 ----
        fh_max = max((formulas.get(p, (0, 0))[1] for k, p, _ in line if k == "ph"),
                     default=0.0)
        lh = max(leading, fh_max + 0.15 * size)
        # T8 两端对齐：非段末行（后面还有内容）、填充率>0.82、单元≥3 时，
        # 把行尾剩余宽度均摊到单元间距（每处上限 0.30×字号，防止过疏）。
        # 段末行保持左对齐——与原文排版惯例一致。
        just = 0.0
        # 居中块不参与两端对齐：两者互斥，同时开会把居中撑成满行。
        if align == "left" and i < n and len(line) >= 3:
            room = (lx1 - lx0) - wsum
            if 0.0 < room <= 0.30 * size * (len(line) - 1) \
                    and wsum > 0.82 * (lx1 - lx0):
                just = room / (len(line) - 1)
        x = lx0
        if align == "center":
            x = lx0 + max(0.0, (lx1 - lx0) - wsum) / 2.0
        for k_idx, (kind, payload, w) in enumerate(line):
            if kind == "ph":
                fw, fh = formulas.get(payload, (0.0, 0.0))
                items.append(Item("formula", x, y + max(0.0, (lh - fh) / 2),
                                  fw, fh, fidx=payload))
            else:
                items.append(Item("text", x, y + (lh - leading) / 2,
                                  w, leading, text=payload, size=size))
            x += w
            if k_idx < len(line) - 1:
                x += just
        y += lh

    return items, y, True


def layout_block(
    text: str,
    formulas: Dict[int, Tuple[float, float]],   # idx -> (宽, 高)（原始尺寸）
    box: Rect,                                   # (x0, top, x1, bottom_max)
    start_size: float,
    measure: Callable[[str, float], float],
    avoid: Sequence[Rect] = (),
    leading_ratio: float = 1.30,
    min_size: float = 5.0,
    align: str = "left",
) -> LaidBlock:
    units = _tokenize(text)
    size = max(start_size, min_size)
    while True:
        leading = size * leading_ratio
        items, end_y, done = _flow(units, formulas, size, leading, box,
                                   avoid, measure, hard_bottom=True,
                                   align=align)
        if done or size <= min_size:
            break
        size = round(size - 0.5, 2)

    if not done:
        # 最小字号仍放不下：继续向下溢出排完（宁可溢出，不丢内容）
        items, end_y, _ = _flow(units, formulas, size, size * leading_ratio,
                                box, avoid, measure, hard_bottom=False,
                                align=align)

    laid = LaidBlock(items=_merge_text_items(items), font_size=size,
                     leading=size * leading_ratio,
                     used_height=end_y - box[1], overflow=not done)
    return laid


# ---------------------------------------------------------------------------
# 目标框与避让矩形（几何工具，写回后端共用）
# ---------------------------------------------------------------------------

def compute_target_box(block, page_blocks, obstacles,
                       page_height: float, margin: float = 2.0,
                       bottom_margin: float = 20.0) -> Rect:
    """译文可用矩形：原块区域 + 向下扩展到「下一个障碍物」为止。

    障碍物 = 同页其他块（含不可译块）与图片/大型矢量图形，条件是位于本块
    下方且水平重叠超过本块宽度的 1/4。扩展从根上缓解「中文比英文长导致
    溢出」的问题；扩展不会越过图表，也不会低于页面下边距。
    """
    # B1 表格单元格：译文严格限制在本格内——向下扩展会串进下一行，
    # 表格结构当场崩掉。宁可缩号（min_size 更低）也不越格。
    cell = getattr(block, "cell_rect", None)
    if cell:
        # 用**整格**宽度，不能夹到原文文字宽度——短词（"Low"）的文字宽可能
        # 不足重排引擎的最小行宽，会导致整行被跳过、译文丢失。
        return (cell[0] + 1.0, cell[1] + 0.5, cell[2] - 1.0, cell[3] - 0.5)

    limit = page_height - bottom_margin
    for o in page_blocks:
        if o is block:
            continue
        if o.top >= block.bottom - 1.0:
            ow = min(block.x1, o.x1) - max(block.x0, o.x0)
            if ow > 0.25 * max(block.width, 1.0):
                limit = min(limit, o.top - margin)
    for (ox0, otop, ox1, obot) in obstacles:
        if otop >= block.bottom - 1.0:
            ow = min(block.x1, ox1) - max(block.x0, ox0)
            if ow > 0.25 * max(block.width, 1.0):
                limit = min(limit, otop - margin)
    # 扫描页（OCR 块）：照片/图表只是整页大图里的像素，检测不到障碍物，
    # 无限向下扩展会把译文写到图上。限制为原块高再加约 2.5 行的余量
    # （中文译文通常短于英文原文，扩展需求本来就小）。
    if getattr(block, "from_ocr", False):
        n_lines = max(len(getattr(block, "line_rects", None) or ()), 1)
        line_h = block.height / n_lines
        limit = min(limit, block.bottom + 2.5 * line_h)
    x0, x1 = block.x0, block.x1
    if getattr(block, "align", "left") == "center":
        # 居中必须相对**栏**来算。沿用原文墨迹框的话，译文只是在「原文本身
        # 那么宽」的范围里居中——原文多宽它就多宽，等于没居中，标题照样左偏。
        cx0 = getattr(block, "col_x0", 0.0)
        cx1 = getattr(block, "col_x1", 0.0)
        if cx1 - cx0 > x1 - x0:
            x0, x1 = cx0, cx1
    return (x0, block.top, x1, max(limit, block.bottom))


def collect_avoid_rects(block, page_blocks, obstacles) -> List[Rect]:
    """本块重排时需要逐行避让的矩形：

      · 页面图片/大型矢量图形（照片嵌栏、图表）；
      · 其他**不可译块**（保持原样显示的公式行、页码等）。
    排除与本块原始区域大量重叠的矩形（图注常落在图表包围盒内，属共生
    而非障碍）。可译块不避让——它们的原文会被抹除。
    """
    out: List[Rect] = []
    lines = getattr(block, "line_rects", None) or \
        [(block.x0, block.top, block.x1, block.bottom)]
    lines_area = max(sum((r[2] - r[0]) * (r[3] - r[1]) for r in lines), 1.0)

    def text_overlap(r: Rect) -> float:
        """障碍物与本块**实际文字行**的重叠占比。绕图文字原本就躲开图片
        （行重叠≈0 → 是真障碍，保留）；图注常印在图表包围盒内（行重叠
        高 → 属共生，排除）。"""
        acc = 0.0
        for (lx0, ltop, lx1, lbot) in lines:
            ox = max(0.0, min(lx1, r[2]) - max(lx0, r[0]))
            oy = max(0.0, min(lbot, r[3]) - max(ltop, r[1]))
            acc += ox * oy
        return acc / lines_area

    for r in obstacles:
        if text_overlap(r) < 0.15:
            out.append(r)
    for o in page_blocks:
        if o is block or getattr(o, "translatable", False):
            continue
        r = (o.x0, o.top, o.x1, o.bottom)
        if text_overlap(r) < 0.15:
            out.append(r)
    return out


def ocr_line_shape_avoids(block, box: Rect, pad: float = 1.5) -> List[Rect]:
    """扫描页（OCR）块的「流形状」避让矩形。

    照片/图表在扫描页里只是整页大图的像素，检测不到障碍物；但原文各行的
    横向范围忠实记录了可写区域——绕图的行天然变窄。把每行相对块框未覆盖
    的左右余量转为避让矩形，译文重排时便按原文的形状绕流，不会压到图上。
    """
    x0, _, x1, _ = box
    out: List[Rect] = []
    for (lx0, ltop, lx1, lbot) in getattr(block, "line_rects", None) or ():
        if lx0 - x0 > 6.0:
            out.append((x0, ltop - pad, lx0 - 1.0, lbot + pad))
        if x1 - lx1 > 6.0:
            out.append((lx1 + 1.0, ltop - pad, x1, lbot + pad))
    return out


def _merge_text_items(items: List[Item]) -> List[Item]:
    out: List[Item] = []
    for it in items:
        if (out and it.kind == "text" and out[-1].kind == "text"
                and abs(out[-1].y_top - it.y_top) < 0.01
                and abs(out[-1].x + out[-1].w - it.x) < 0.05
                and out[-1].size == it.size):
            prev = out[-1]
            prev.text += it.text
            prev.w += it.w
        else:
            out.append(it)
    return out
