"""PDF 解析：把每页拆成「文本块（段落）」，保留坐标与字号。

坐标系沿用 pdfplumber 的「左上角原点」（top 向下增大），在写回时再转换。
关键：分栏中缝用「列覆盖度谷底」从单词分布中检测（不依赖行宽阈值），
再把每一行「正好在中缝处」切开，从而稳健分离左右栏、保留整幅标题。
图片与矢量图形不在此处理——写回阶段保持原样，因此天然保留。

行内公式保护（双层策略）：
  · 强信号（必须图像/矢量回贴）：提取即乱码（私用区字符/cid）、上下标（字号
    明显缩小且垂直偏移）、高型符号（积分号/大括号等非字母数字的超高 glyph）。
    这些内容无法用纯文本重排表达，检测为 FormulaSpan，正文中以 ⟦Fn⟧ 占位，
    翻译时原样保留占位符，回填时在占位符位置回贴原始区域。
  · 弱信号（可作为文本重排）：数学字体（STIX/CMMI 等）的线性符号、单字母斜体
    变量，如 “n = 53”“P < 0.001”。这类交给翻译提示词原样保留即可，避免满页
    图像补丁。弱信号词只在与强信号相邻时才被并入公式区。
每个块额外记录 line_rects（原文各行矩形），供写回端做精确抹除/遮盖。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pdfplumber

# 占位符：⟦Fn⟧（U+27E6 / U+27E7，正文中几乎不可能自然出现）
PLACEHOLDER_FMT = "⟦F{}⟧"
PLACEHOLDER_RE = re.compile(r"⟦F(\d+)⟧")

Rect = Tuple[float, float, float, float]  # (x0, top, x1, bottom)


@dataclass
class FormulaSpan:
    """行内公式区域（块内局部编号，对应正文占位符 ⟦F{idx}⟧）。"""
    idx: int
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class Block:
    text: str            # 已插入 ⟦Fn⟧ 占位符的段落文本
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    page_index: int
    translatable: bool = True
    translation: Optional[str] = None
    line_rects: List[Rect] = field(default_factory=list)      # 原文各行矩形（抹除用）
    formulas: List[FormulaSpan] = field(default_factory=list)  # 行内公式（回贴用）
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)        # 文字主色（RGB 0~1）
    from_ocr: bool = False   # 来自扫描页 OCR：无文字层，写回需白底覆盖而非 redact
    bold: bool = False       # 粗体块（章节标题等）：写回时合成加粗，保留原色
    # 表格单元格块：译文严格限制在本单元格内（不得向下扩展串行到下一行）
    cell_rect: Optional[Rect] = None
    # 水平对齐："left" | "center" | "right"。从几何反推（见 _mark_alignment）。
    # 排版引擎原先只有左对齐一种模式，居中标题因此在译文变短后整体左偏。
    align: str = "left"
    # 所属栏的左右边界。居中/右对齐必须相对**栏**而不是原文墨迹框来算，
    # 否则「在原文自己的宽度里居中」等于没做。
    col_x0: float = 0.0
    col_x1: float = 0.0

    @property
    def in_table(self) -> bool:
        return self.cell_rect is not None

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class PageLayout:
    page_index: int
    width: float
    height: float
    blocks: List[Block] = field(default_factory=list)
    # 图片与大型矢量图形的包围盒：重排向下扩展时避让，避免译文压到图表
    obstacles: List[Rect] = field(default_factory=list)
    # 疑似扫描页（大图无文字层）但 OCR 组件不可用——供 pipeline 提示安装
    needs_ocr: bool = False
    # 本页检测到的表格单元格矩形（B1）；空表示无表格
    table_cells: List[Rect] = field(default_factory=list)


def _median(values, default=0.0):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else default


def _word_size(w) -> float:
    s = w.get("size")
    if s:
        return float(s)
    return float(w["bottom"] - w["top"])


# 「这个字体是粗体吗」——只能从字体名猜，PDF 里没有字重字段。
# 大小写不敏感的常见命名。注意 TeX 系的 Medium 就是正文粗体（NimbusRomNo9L-Medi）。
_BOLD_FONT = re.compile(
    r"bold|black|heavy|semib|demib|ultrab|"
    r"cmbx|"            # Computer Modern Bold Extended（TeX 默认粗体）
    r"bx\d|"            # SFBX1200 一类 TeX 命名
    r"[-,_]bd?\b|"      # Foo-B / Foo-Bd
    r"[-,_]medi",
    re.IGNORECASE)

# TeX / Linux Libertine 系的后缀约定：T=Text、B=Bold、I=Italic。
# 故 LinLibertineTB / LinBiolinumTB(I) 是粗体，而 …T（正常）、…TI（斜体）不是。
# **必须大小写敏感**：小写的 "tb" 在普通字体名里可能只是巧合（如 Whitby）。
_BOLD_SUFFIX = re.compile(r"TBI?$")

# 章节编号开头："3 "、"3.2 "、"3.2.1 "、"IV. "、"A. "。
# 仅用于在**连续的粗体行**之间断块：有些模板（如 ACM acmart）的一级与二级
# 标题**字号完全相同**（都 10.9pt），只差编号深度与大小写，靠字号永远分不开，
# 会把"2 相关工作"和"2.1 HCI 研究"并成一块、当作一个标题翻译。
# 每级限 1~2 位数字：真实章节号不会是 "2020"（年份）或 "3.14159"（小数），
# 而这两种恰恰是正文里最常见的数字开头，不收紧就会误断块。
_HEADING_NUM = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d{1,2})*|[IVXLC]{1,5}\.|[A-Z]\.)\s+\S")


def _word_bold(w) -> bool:
    """按字体名判断加粗（子集前缀 ABCDEF+ 已剥离）。

    ⚠️ 这里放宽过一次，起因是真实样本：ACM acmart 模板（CHI/CSCW/UIST 等
    全用它）的粗体叫 `LinLibertineTB`，旧正则 `bold|black|heavy|-b\\b` 一个都
    不匹配 → 全文**零个粗体块** → 章节标题不与正文断开，被当成正文一起翻译，
    输出里整页只剩一个字号，标题层级彻底消失。字体名是唯一线索，宁可多认一些。
    """
    fn = (w.get("fontname") or "").split("+")[-1]
    return bool(_BOLD_FONT.search(fn) or _BOLD_SUFFIX.search(fn))


def _color_differs(a, b, tol: float = 0.2) -> bool:
    """两色是否有明显差异（任一通道差 > tol）。缺省色视为不差异。"""
    if not a or not b:
        return False
    return any(abs(float(x) - float(y)) > tol for x, y in zip(a, b))


def _norm_color(raw) -> Tuple[float, float, float]:
    """把 pdfplumber 的 non_stroking_color 归一化为 RGB (0~1)。"""
    try:
        if raw is None:
            return (0.0, 0.0, 0.0)
        if isinstance(raw, (int, float)):
            v = min(max(float(raw), 0.0), 1.0)
            return (v, v, v)
        vals = [min(max(float(v), 0.0), 1.0) for v in raw]
        if len(vals) == 1:
            return (vals[0],) * 3
        if len(vals) == 3:
            return tuple(vals)
        if len(vals) == 4:  # CMYK → RGB
            c, m, y, k = vals
            return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    except Exception:  # noqa: BLE001
        pass
    return (0.0, 0.0, 0.0)


def _majority_color(colors: List[Tuple[float, float, float]]):
    if not colors:
        return (0.0, 0.0, 0.0)
    cnt = {}
    for c in colors:
        key = tuple(round(v, 2) for v in c)
        cnt[key] = cnt.get(key, 0) + 1
    return max(cnt.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# 行内公式检测
# ---------------------------------------------------------------------------

_MATH_FONT = re.compile(
    r"STIX|CM(?:MI|SY|EX|BSY|MIB|R\d)|MTMI|MTSY|MTEX|MSAM|MSBM|Symbol|"
    r"Euclid|rsfs|eufm|esint|wasy|stmary|LucidaNewMath|CambriaMath|MathPack",
    re.IGNORECASE,
)
_ITALIC_FONT = re.compile(r"Italic|Oblique|-It\b|-It$|\+It\b", re.IGNORECASE)

# 常见、可用中文字体安全渲染的数学字符——出现时不强制图像保护
_SAFE_MATH_CHARS = set("≤≥≠±×÷≈∼~°·−–—…‰′″<>=+*/^_|\\%")

# 需要图像保护的字符区间：数学运算符/补充、箭头、字母样式符号等
_HARD_RANGES = (
    (0x2200, 0x22FF), (0x2A00, 0x2AFF), (0x27C0, 0x27EF), (0x2980, 0x29FF),
    (0x2190, 0x21FF), (0x2100, 0x214F), (0x1D400, 0x1D7FF),
)


def _is_hard_char(ch: str) -> bool:
    if ch in _SAFE_MATH_CHARS:
        return False
    o = ord(ch)
    if 0xE000 <= o <= 0xF8FF:  # 私用区：字体子集乱码
        return True
    return any(lo <= o <= hi for lo, hi in _HARD_RANGES)


def _classify_word(w, line_size: float, line_center_y: float) -> str:
    """返回 'strong'（必须保护）/ 'weak'（数学相关，可并入相邻强区）/ 'plain'。"""
    t = w["text"]
    fn = w.get("fontname") or ""
    size = _word_size(w)
    h = w["bottom"] - w["top"]

    if "(cid:" in t or any(_is_hard_char(c) for c in t):
        return "strong"
    # 上下标：字号明显缩小 + 垂直中心偏移（作者标注/引用上标也按此保护，视觉一致）
    if size < 0.72 * line_size and line_size > 0:
        center = (w["top"] + w["bottom"]) / 2
        if abs(center - line_center_y) > 0.18 * line_size:
            return "strong"
    # 高型 glyph（积分号、大括号、根号等）；纯字母数字除外（避免误伤首字下沉）
    if h > 1.6 * line_size and line_size > 0 and not t.isalnum():
        return "strong"
    if _MATH_FONT.search(fn.split("+")[-1]):
        return "weak"
    # 单/双字母斜体变量（n, P, xi …）
    if _ITALIC_FONT.search(fn):
        alpha = [c for c in t if c.isalpha()]
        if 1 <= len(alpha) <= 2 and len(t) <= 4:
            return "weak"
    return "plain"


def _detect_formula_runs(ws: List[dict], line_size: float) -> List[Tuple[int, int]]:
    """在一行的词列表中找出需保护的公式区（词下标区间 [i, j]，含 j）。

    规则：连续的 strong/weak 词构成候选区，区内至少要有一个 strong 才成立；
    纯 weak 区（如 n = 53）作为文本交给翻译端原样保留。
    """
    if not ws:
        return []
    tops = min(w["top"] for w in ws)
    bots = max(w["bottom"] for w in ws)
    center_y = (tops + bots) / 2
    cls = [_classify_word(w, line_size, center_y) for w in ws]
    runs: List[Tuple[int, int]] = []
    i = 0
    n = len(ws)
    while i < n:
        if cls[i] == "plain":
            i += 1
            continue
        j = i
        while j + 1 < n and cls[j + 1] != "plain":
            j += 1
        if any(cls[k] == "strong" for k in range(i, j + 1)):
            runs.append((i, j))
        i = j + 1
    return runs


# ---------------------------------------------------------------------------
# 行构造（含公式占位符）
# ---------------------------------------------------------------------------

def _sanitize(text: str) -> str:
    """源文本中出现的 ⟦⟧ 替换掉，避免与占位符冲突。"""
    return text.replace("⟦", "[").replace("⟧", "]")


def _make_line(word_group, counter: List[int], detect_formulas: bool = True):
    """把一组词组装成行；检测公式区并以 ⟦Fn⟧（页内全局编号）占位。
    OCR 行关闭公式检测——识别框的字号/偏移不具备排版语义，误报会把
    原扫描图的英文像素回贴到白底覆盖之上。"""
    ws = sorted(word_group, key=lambda w: w["x0"])
    size = _median([_word_size(w) for w in ws], 10.0)
    runs = _detect_formula_runs(ws, size) if detect_formulas else []

    covered = {}
    for (i, j) in runs:
        counter[0] += 1
        gid = counter[0]
        for k in range(i, j + 1):
            covered[k] = gid if k == i else -1  # 区首放占位符，其余跳过

    parts: List[str] = []
    formulas: List[Tuple[int, Rect]] = []
    for k, w in enumerate(ws):
        mark = covered.get(k)
        if mark is None:
            parts.append(_sanitize(w["text"]))
        elif mark > 0:
            i, j = next(r for r in runs if r[0] == k)
            seg = ws[i:j + 1]
            rect = (min(s["x0"] for s in seg), min(s["top"] for s in seg),
                    max(s["x1"] for s in seg), max(s["bottom"] for s in seg))
            formulas.append((mark, rect))
            parts.append(PLACEHOLDER_FMT.format(mark))
        # mark == -1：同一公式区的后续词，跳过

    # 正文字号：尽量用非公式词的中位数（公式区可能含超大/超小字号）
    plain_ws = [w for k, w in enumerate(ws) if k not in covered]
    plain_sizes = [_word_size(w) for w in plain_ws]
    # 行加粗度：非公式词中加粗字符占比 ≥ 0.6 视为加粗行（章节标题）
    bold_chars = sum(len(w["text"]) for w in plain_ws if _word_bold(w))
    total_chars = sum(len(w["text"]) for w in plain_ws) or 1
    return {
        "text": " ".join(parts),
        "x0": min(w["x0"] for w in ws),
        "x1": max(w["x1"] for w in ws),
        "top": min(w["top"] for w in ws),
        "bottom": max(w["bottom"] for w in ws),
        "size": _median(plain_sizes, size),
        "formulas": formulas,
        "color": _majority_color([_norm_color(w.get("non_stroking_color"))
                                  for w in plain_ws]),
        "bold": bold_chars / total_chars >= 0.6,
    }


# ---------------------------------------------------------------------------
# 分栏中缝检测（基于单词覆盖度谷底，稳健应对整幅标题/摘要/页边竖排）
# ---------------------------------------------------------------------------

def _detect_split_x(words, page_width) -> Optional[float]:
    if len(words) < 40:
        return None
    W = page_width

    def cov(x):
        return sum(1 for w in words if w["x0"] < x < w["x1"])

    left_peak = max((cov(W * r) for r in (0.20, 0.25, 0.30)), default=0)
    right_peak = max((cov(W * r) for r in (0.70, 0.75, 0.80)), default=0)
    # 峰值下限取 4：末页等稀疏双栏页的短栏覆盖度可能只有 5~6，用 8 会漏检
    # （漏检导致左右栏并成全宽行、译文阅读顺序错乱）。误报由下方"中缝覆盖
    # 远低于两栏峰值"的相对条件兜住——单栏页中部覆盖度与两侧相当，不会触发。
    if left_peak < 4 or right_peak < 4:
        return None
    # 中部扫描，找覆盖度最小处（中缝）
    step = max(2.0, 0.005 * W)
    x = 0.34 * W
    hi = 0.66 * W
    best = None
    while x <= hi:
        c = cov(x)
        if best is None or c < best[0]:
            best = (c, x)
        x += step
    gutter_cov, gutter_x = best
    # 中缝覆盖远低于两栏中心 → 判定双栏
    if gutter_cov <= 0.30 * min(left_peak, right_peak):
        return gutter_x
    return None


# ---------------------------------------------------------------------------
# 分行：垂直重叠聚成「带」，再在「中缝」或「超大间隙」处切开
# ---------------------------------------------------------------------------

def _group_lines(words, split_x, page_width, counter: List[int],
                 detect_formulas: bool = True, gutter_tol: float = 0.0):
    # OCR 识别框边缘常渗过中缝（最多 ~3×tol），甚至与对侧框横向重叠，
    # 导致中缝切行永远不触发。把小幅渗缝的框裁回其主体所在侧；
    # 真正跨中缝的整幅行（渗越 ≫3×tol）不受影响。文本 PDF（tol=0）不裁。
    if split_x is not None and gutter_tol > 0:
        deep = 3.0 * gutter_tol
        for w in words:
            if w["x0"] < split_x - deep and split_x < w["x1"] <= split_x + deep:
                w["x1"] = split_x - 0.5
            elif w["x1"] > split_x + deep and split_x - deep <= w["x0"] < split_x:
                w["x0"] = split_x + 0.5
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    bands = []
    cur = []
    cur_top = cur_bottom = None
    for w in words:
        if not cur:
            cur = [w]
            cur_top, cur_bottom = w["top"], w["bottom"]
            continue
        overlap = min(cur_bottom, w["bottom"]) - max(cur_top, w["top"])
        min_h = max(min(cur_bottom - cur_top, w["bottom"] - w["top"]), 1.0)
        if overlap >= 0.4 * min_h:
            cur.append(w)
            cur_top = min(cur_top, w["top"])
            cur_bottom = max(cur_bottom, w["bottom"])
        else:
            bands.append(cur)
            cur = [w]
            cur_top, cur_bottom = w["top"], w["bottom"]
    if cur:
        bands.append(cur)

    big_gap = max(36.0, 0.06 * page_width)
    lines = []
    for band in bands:
        band = sorted(band, key=lambda w: w["x0"])
        seg = [band[0]]
        for prev, w in zip(band, band[1:]):
            gap = w["x0"] - prev["x1"]
            # OCR 识别框边缘常渗进中缝（gutter_tol 放宽判定）；文本 PDF 为 0，
            # 行为与原先完全一致
            crosses_gutter = (split_x is not None
                              and prev["x1"] <= split_x + gutter_tol
                              and w["x0"] >= split_x - gutter_tol
                              and w["x0"] > prev["x1"] + 0.5)
            # 字号断差：正文行与页边小字（版权注等）水平相邻时切开
            s1, s2 = _word_size(prev), _word_size(w)
            size_jump = (gap > 6.0
                         and max(s1, s2) / max(min(s1, s2), 0.1) > 1.6)
            if crosses_gutter or gap > big_gap or size_jump:
                lines.append(_make_line(seg, counter, detect_formulas))
                seg = [w]
            else:
                seg.append(w)
        lines.append(_make_line(seg, counter, detect_formulas))
    return lines


# ---------------------------------------------------------------------------
# 行 → 段落块
# ---------------------------------------------------------------------------

def _join_lines_text(lines) -> str:
    parts = []
    for i, ln in enumerate(lines):
        t = ln["text"].strip()
        if i == 0:
            parts.append(t)
            continue
        prev = parts[-1]
        if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha():
            parts[-1] = prev[:-1] + t
        else:
            parts.append(" " + t)
    return "".join(parts).strip()


def _group_blocks(lines, page_index, from_ocr: bool = False) -> List[Block]:
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: (l["top"], l["x0"]))
    med_h = _median([l["bottom"] - l["top"] for l in lines], 10.0)
    blocks = []
    cur = [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        gap = ln["top"] - prev["bottom"]
        # OCR 的"字号"来自识别框高、天然抖动，突变阈值放宽以免误断段（T7）
        size_jump_ratio = 0.45 if from_ocr else 0.25
        # 相邻两行**都是粗体** = 多半是紧挨着的两级标题（"2 相关工作" 紧跟
        # "2.1 HCI 研究"）。它们的字号差是排版刻意定的，但常常只有 10%~20%，
        # 够不着 0.25 的通用阈值，结果两级标题并成一块、层级丢失。标题行短、
        # 字号是有意为之，这里用更敏感的阈值；正文不受影响。
        both_bold = bool(prev.get("bold") and ln.get("bold"))
        if both_bold:
            size_jump_ratio = 0.10
        size_change = (abs(ln["size"] - prev["size"])
                       / max(prev["size"], 1.0) > size_jump_ratio)
        # 同字号的相邻两级标题：靠「本行另起一个章节编号」断开。只在两行都
        # 粗体时启用——正文里以数字开头的行（列表项、"2020 年…"）不受影响。
        new_heading = both_bold and bool(_HEADING_NUM.match(ln.get("text", "")))
        # 行距阈值按行高缩放：大字号标题行距天然更大，不应被拆成多块。
        # 只取相邻两行行高的**较小者**参与放宽——单个超高行带（如带上下标
        # 的作者行）不应把它与后续正文之间的真实段间隙也吞并。
        prev_h = prev["bottom"] - prev["top"]
        cur_h = ln["bottom"] - ln["top"]
        gap_limit = 0.6 * max(med_h, min(prev_h, cur_h))
        if from_ocr:
            # T7 扫描页行→段合并：识别框上下留白/字号均有抖动，按文本 PDF 的
            # 阈值会把段落拆成逐行碎块（句子腰斩、翻译上下文受损）。放宽合并。
            gap_limit *= 1.35
        # 加粗/颜色变化处断块：把章节标题（粗体、常为红色）从正文中分离，
        # 使其独立翻译并按原色+加粗回填，恢复原文的层级结构。OCR 页无字体/
        # 颜色信息（统一黑、非粗），这两条天然不触发，行为不变。
        emphasis_change = (not from_ocr) and (
            prev.get("bold", False) != ln.get("bold", False)
            or _color_differs(prev.get("color"), ln.get("color")))
        if gap > gap_limit or size_change or emphasis_change or new_heading:
            blocks.append(_make_block(cur, page_index, from_ocr))
            cur = [ln]
        else:
            cur.append(ln)
    blocks.append(_make_block(cur, page_index, from_ocr))
    return blocks


def _make_block(lines, page_index, from_ocr: bool = False) -> Block:
    text = _join_lines_text(lines)
    x0 = min(l["x0"] for l in lines)
    x1 = max(l["x1"] for l in lines)

    # 汇总公式并按块内出现顺序重新编号（页内全局 gid → 块内局部 idx）
    formulas: List[FormulaSpan] = []
    gid_to_local = {}
    for l in lines:
        for gid, rect in l.get("formulas", ()):
            gid_to_local[gid] = len(formulas) + 1
            formulas.append(FormulaSpan(len(formulas) + 1, *rect))
    if gid_to_local:
        text = PLACEHOLDER_RE.sub(
            lambda m: PLACEHOLDER_FMT.format(gid_to_local.get(int(m.group(1)), 0)),
            text)

    is_bold = sum(1 for l in lines if l.get("bold")) * 2 >= len(lines)
    return Block(
        text=text,
        x0=x0,
        top=min(l["top"] for l in lines),
        x1=x1,
        bottom=max(l["bottom"] for l in lines),
        size=_median([l["size"] for l in lines], 10.0),
        page_index=page_index,
        translatable=_is_translatable(text, from_ocr, is_bold) and (x1 - x0) >= 14.0,
        line_rects=[(l["x0"], l["top"], l["x1"], l["bottom"]) for l in lines],
        formulas=formulas,
        color=_majority_color([l.get("color", (0.0, 0.0, 0.0)) for l in lines]),
        from_ocr=from_ocr,
        bold=is_bold,
    )


_MATH_SYMS = set("=+*/^_|<>{}\\")


def _in_cjk(ch: str) -> bool:
    """表意文字（中日）或日文假名——判定 CJK 源文用。"""
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF)


def _is_translatable(text: str, from_ocr: bool = False, is_bold: bool = False,
                     in_table: bool = False) -> bool:
    # 占位符不参与可译性判断（公式已被抽走，剩余文字才是判断对象）
    t = PLACEHOLDER_RE.sub("", text).strip()
    if in_table:
        # 表格单元格：短标签才是常态（"Low"/"Condition"/"46 students"），
        # 只要含字母（任意脚本）就翻；纯数字/破折号等保持原样。
        return any(c.isalpha() for c in t) and len(t) >= 2
    if len(t) < 3:
        return False
    # 表意文字（中/日）无空格 → 按字符数而非词数判断
    cjk = sum(1 for c in t if _in_cjk(c))
    words = [w for w in t.split() if any(c.isalpha() for c in w)]
    # 词数<2 一般不译，但几种例外：
    #  · OCR 行常整行丢空格（"knowledgeacquisition…"）→ 长度够即当正文；
    #  · 粗体单词章节标题（DISCUSSION / RESULTS / METHODS 等）→ 也要译；
    #  · CJK 源文（≥4 个表意字符）→ 无空格分不出词，按字符数放行。
    single_ok = (from_ocr and len(t) >= 12) or cjk >= 4 or (
        is_bold and len(t) >= 4 and any(c.isalpha() for c in t))
    if len(words) < 2 and not single_ok:
        return False
    non_space = sum(not c.isspace() for c in t)
    letters = sum(c.isalpha() for c in t)   # 任意脚本（拉丁/西里尔/CJK…）
    if non_space and letters / non_space < 0.5:
        return False
    math_syms = sum(c in _MATH_SYMS for c in t)
    if math_syms >= 3 and non_space and math_syms / non_space > 0.06:
        return False
    return True


# ---------------------------------------------------------------------------
# 参考文献区检测：整区保留原文原版式（学术惯例，同有道/知云等文档翻译工具）
# ---------------------------------------------------------------------------
# 理由：引用条目不该翻译（读者需按原文检索文献），而送去翻译→模型原样退回→
# 我们抹掉原排版重排，会毁掉悬挂缩进/斜体刊名/粗体卷号，且白耗 token。
# 检测为引用块的一律 translatable=False，写回端完全不动它们。

_REF_HEADING_RE = re.compile(
    r"^\s*(references?(\s+and\s+notes)?|bibliography|works\s+cited)\s*$", re.I)
_ENTRY_START_RE = re.compile(r"(?:^|\s)\d{1,3}\.\s+[A-Z]")   # "12. M. Kapur,"
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_PAGES_RE = re.compile(r"\b\d+\s*[–—-]\s*\d+\b")             # "209–249"


def _looks_like_reference_list(text: str, in_refs: bool) -> bool:
    starts = len(_ENTRY_START_RE.findall(text))
    years = len(_YEAR_RE.findall(text))
    pages = len(_PAGES_RE.findall(text))
    if starts >= 3 and years >= 2:          # 多条目合并块（最常见形态）
        return True
    if (re.match(r"\s*\d{1,3}\.\s+[A-Z]", text)
            and years >= 1 and (pages >= 1 or starts >= 2)):
        return True                          # 以编号开头的单条目/短块
    if in_refs and (years + pages) >= 2:
        return True                          # 换栏/换页的无编号续块
    if in_refs and "doi" in text.lower():
        return True
    return False


def _mark_reference_blocks(layouts: List[PageLayout]) -> int:
    """按阅读顺序扫全篇，把参考文献条目块改为不可译。返回标记数量。

    状态机：REFERENCES 标题（本身保持可译，会译成"参考文献"）或首个引用样
    块进入引用区；任何不像引用的块（如 Acknowledgments 散文）立即退出——
    保证引用区之后的致谢/资助/数据可用性等正常翻译。
    """
    marked = 0
    in_refs = False
    for L in layouts:
        for b in L.blocks:
            if not b.translatable:
                continue
            # 页脚（"Chen et al., Sci. Robot. 10, … (2025)…"）含年份，会被
            # 续块规则误捕。它是页面"家具"、不在正文流里：跳过且不改状态。
            if b.top > 0.92 * L.height:
                continue
            if _REF_HEADING_RE.match(b.text or ""):
                in_refs = True
                continue
            if _looks_like_reference_list(b.text or "", in_refs):
                b.translatable = False
                in_refs = True
                marked += 1
            else:
                in_refs = False
    return marked


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------

def _collect_obstacles(page, layout: PageLayout) -> None:
    try:
        for im in page.images:
            layout.obstacles.append((float(im["x0"]), float(im["top"]),
                                     float(im["x1"]), float(im["bottom"])))
        for obj in list(page.rects) + list(page.curves):
            w = float(obj["x1"]) - float(obj["x0"])
            h = float(obj["bottom"]) - float(obj["top"])
            if w * h >= 400.0 and min(w, h) >= 8.0:
                layout.obstacles.append((float(obj["x0"]), float(obj["top"]),
                                         float(obj["x1"]), float(obj["bottom"])))
    except Exception:  # noqa: BLE001
        pass


def _table_cell_rects(page) -> List[Rect]:
    """检测表格并返回所有单元格矩形（B1）。

    保守判据，避免把版面分隔线误判成表格：至少 2×2、总单元格 ≥4、
    表格面积不超过页面 85%、单元格尺寸合理。
    """
    out: List[Rect] = []
    try:
        pw, ph = float(page.width), float(page.height)
        for t in page.find_tables():
            cells = [c for c in (t.cells or []) if c]
            if len(cells) < 4:
                continue
            x0, top, x1, bottom = (float(v) for v in t.bbox)
            if (x1 - x0) * (bottom - top) > 0.85 * pw * ph:
                continue
            xs = {round(float(c[0]), 1) for c in cells}
            ys = {round(float(c[1]), 1) for c in cells}
            if len(xs) < 2 or len(ys) < 2:      # 需真正的行列结构
                continue
            for c in cells:
                cx0, ctop, cx1, cbottom = (float(v) for v in c)
                if cx1 - cx0 >= 8 and cbottom - ctop >= 6:
                    out.append((cx0, ctop, cx1, cbottom))
    except Exception:  # noqa: BLE001 — 表格检测失败不影响正文解析
        return []
    return out


def _in_rect(w, r: Rect) -> bool:
    cx = (float(w["x0"]) + float(w["x1"])) / 2
    cy = (float(w["top"]) + float(w["bottom"])) / 2
    return r[0] <= cx <= r[2] and r[1] <= cy <= r[3]


def _build_cell_blocks(words, cells: List[Rect], page_index: int,
                       page_width: float, counter: List[int]):
    """把落在单元格内的词就地成块（一格 = 一个翻译单元）。

    返回 (单元格块列表, 剩余的非表格词)。逐格独立成块是关键——整行合并会
    让译文横跨列被重排，表格结构当场崩掉。
    """
    blocks: List[Block] = []
    claimed = set()
    for cell in cells:
        idxs = [i for i, w in enumerate(words)
                if i not in claimed and _in_rect(w, cell)]
        if not idxs:
            continue
        claimed.update(idxs)
        cell_words = [words[i] for i in idxs]
        lines = _group_lines(cell_words, None, page_width, counter)
        if not lines:
            continue
        b = _make_block(lines, page_index)          # 一格一块，不再二次切分
        b.cell_rect = cell
        b.translatable = _is_translatable(b.text, in_table=True) \
            and (b.x1 - b.x0) >= 8.0
        blocks.append(b)
    rest = [w for i, w in enumerate(words) if i not in claimed]
    return blocks, rest


def _looks_scanned(page) -> bool:
    """无文字层且有大图覆盖 → 疑似扫描页。"""
    try:
        pw, ph = float(page.width), float(page.height)
        img_area = sum(
            max(0.0, float(im["x1"]) - float(im["x0"]))
            * max(0.0, float(im["bottom"]) - float(im["top"]))
            for im in page.images)
        return img_area >= 0.35 * pw * ph
    except Exception:  # noqa: BLE001
        return False


def _parse_page(page, page_index, ocr=None) -> PageLayout:
    layout = PageLayout(page_index=page_index, width=page.width, height=page.height)
    _collect_obstacles(page, layout)
    try:
        words = page.extract_words(
            extra_attrs=["size", "fontname", "non_stroking_color"],
            use_text_flow=False,
            keep_blank_chars=False,
        )
    except Exception:
        try:
            words = page.extract_words(
                extra_attrs=["size", "fontname"],
                use_text_flow=False,
                keep_blank_chars=False,
            )
        except Exception:
            words = page.extract_words()
    # 旋转文字（页边竖排等）不参与翻译，保持原样即可
    words = [w for w in words if w.get("upright", True)]

    from_ocr = False
    if not words:
        # 无文字层：疑似扫描页 → OCR 生成"词"（每识别行一个）走原管线
        if ocr is not None:
            words = ocr.words_for_page(page_index)
            from_ocr = bool(words)
        if not words:
            layout.needs_ocr = _looks_scanned(page)
            return layout
    if from_ocr:
        # 扫描页整页是一张大图，不作为排版障碍物（否则译文没有落点）
        layout.obstacles = []

    counter = [0]  # 页内公式全局编号

    # B1 表格：先把单元格内的词就地成块，剩余词再走正常正文流程。
    # 这样表格既能被翻译，又不会因整行合并重排而结构崩坏。（OCR 页无矢量
    # 表格线，find_tables 无从检测，故仅对文字版启用。）
    cell_blocks: List[Block] = []
    if not from_ocr:
        cells = _table_cell_rects(page)
        if cells:
            layout.table_cells = cells
            cell_blocks, words = _build_cell_blocks(
                words, cells, page_index, page.width, counter)
            if not words:
                layout.blocks = cell_blocks
                return layout

    split_x = _detect_split_x(words, page.width)
    if split_x is None:
        # 首页常见「整幅标题/摘要 + 下半页双栏」混排：全页检测不到中缝时，
        # 对下半部分单独检测。找到的中缝对整页安全——跨中缝的整幅行不受影响。
        lower = [w for w in words if w["top"] > 0.45 * page.height]
        split_x = _detect_split_x(lower, page.width)
    lines = _group_lines(words, split_x, page.width, counter,
                         detect_formulas=not from_ocr,
                         gutter_tol=4.0 if from_ocr else 0.0)

    if split_x is None:
        layout.blocks = _group_blocks(lines, page_index, from_ocr)
    else:
        full, left, right = [], [], []
        for l in lines:
            if l["x0"] < split_x < l["x1"]:
                full.append(l)          # 跨中缝（整幅标题/摘要/横幅）
            elif (l["x0"] + l["x1"]) / 2 < split_x:
                left.append(l)
            else:
                right.append(l)
        # 行提升：整幅段落的**末行**往往较短、不再跨中缝，会被误分到左/右栏
        # （典型：大标题第二行、摘要收尾行）。若某左/右行紧贴某 full 行正下方、
        # 字号一致且起点对齐，则提升进 full 组，避免段落被腰斩。
        _promote_full_tails(full, left)
        _promote_full_tails(full, right)
        for grp in (full, left, right):
            layout.blocks.extend(_group_blocks(grp, page_index, from_ocr))
    layout.blocks.extend(cell_blocks)
    _mark_alignment(layout, split_x)
    return layout


def _mark_alignment(layout: "PageLayout", split_x: Optional[float]) -> None:
    """从几何反推每块的水平对齐，并记下所属栏的左右边界。

    为什么需要：排版引擎原先只有「从框左边开始排」一种模式。居中标题的框
    就是它自己的墨迹范围，中文译文比英文短约三分之一，于是每个居中标题都
    向左偏 (原文宽 − 译文宽)/2——偏移同向且固定，整篇看下来就是"歪的"。

    **只判居中，不判右对齐**：右对齐在论文里极罕见，而它的几何特征（左间隙
    大、右间隙小）与「首行缩进的两端对齐段落」几乎一样，误判会把正常段落
    整段推到右边。宁可漏，不可错。
    """
    blocks = [b for b in layout.blocks if not b.in_table]
    if not blocks:
        return

    # 分组：跨中缝的整幅块自成一组，其余按中心点归左/右栏。
    if split_x is None:
        groups = [blocks]
    else:
        full = [b for b in blocks if b.x0 < split_x < b.x1]
        left = [b for b in blocks
                if b not in full and (b.x0 + b.x1) / 2 < split_x]
        right = [b for b in blocks if b not in full and b not in left]
        groups = [g for g in (full, left, right) if g]

    for grp in groups:
        # 栏边界取**多行正文块**的极值：学术排版里正文是两端对齐的，
        # 它的左右边就是栏边。标题/公式/页眉都短，拿它们定边会偏。
        body = [b for b in grp
                if b.translatable and not b.bold and len(b.line_rects) >= 2]
        ref = body if len(body) >= 2 else grp
        L = min(b.x0 for b in ref)
        R = max(b.x1 for b in ref)
        if R - L < 40.0:            # 栏太窄，几何不可靠，全部按左对齐
            for b in grp:
                b.col_x0, b.col_x1 = L, R
            continue
        tol = max(2.0, 0.02 * (R - L))

        for b in grp:
            b.col_x0, b.col_x1 = L, R
            lines = b.line_rects
            if 2 <= len(lines) <= 3:
                # 多行块自带证据，**不看左右间隙**：块的包围盒是各行的并集，
                # 最长那行往往接近满栏，间隙因此接近 0——用间隙判会把居中的
                # 多行大标题整个漏掉（实测 ACM acmart 标题左隙仅 5.1pt）。
                #
                # 判别式：**左右两侧的参差量必须相当**。居中块左边缩进多少、
                # 右边就缩进多少（都等于行宽差的一半）；而
                #   · 悬挂缩进的编号列表 → 只有左边变，右边被两端对齐拉齐；
                #   · 左对齐的多行标题   → 只有右边变，左边全部对齐；
                #   · 引用块 \begin{quote} → 两边都不变（整体缩进而已）。
                # 只看「中点重合 + 起点分散」会把前两者一并误判（实测确实误判了）。
                #
                # 行数上限 3：论文里 4 行以上的居中块几乎不存在，那是段落。
                # 作者块（acmart 里每位作者的姓名/单位确实居中）也因此被排除
                # ——它有自己的窄框，按整栏宽居中会撑破作者网格，宁可不动。
                mids = [(x0 + x1) / 2 for x0, _, x1, _ in lines]
                l_spread = max(x0 for x0, _, _, _ in lines) - min(x0 for x0, _, _, _ in lines)
                r_spread = max(x1 for _, _, x1, _ in lines) - min(x1 for _, _, x1, _ in lines)
                if (max(mids) - min(mids) <= tol
                        and l_spread > tol and r_spread > tol
                        and abs(l_spread - r_spread) <= tol):
                    b.align = "center"
            elif len(lines) <= 1:
                # 单行块没有行间证据可用，只能看它在栏里是否左右等距。
                gap_l, gap_r = b.x0 - L, R - b.x1
                if gap_l > tol and gap_r > tol and abs(gap_l - gap_r) <= tol:
                    b.align = "center"


def _promote_full_tails(full: list, side: list) -> None:
    if not full:
        return
    changed = True
    while changed:
        changed = False
        for l in list(side):
            lh = l["bottom"] - l["top"]
            for f in full:
                fh = f["bottom"] - f["top"]
                same_size = abs(l["size"] - f["size"]) <= 0.10 * max(f["size"], 1.0)
                below = 0.0 <= l["top"] - f["bottom"] < 0.75 * max(lh, fh)
                aligned = abs(l["x0"] - f["x0"]) < 2.0
                # 同带提升：整幅行若在中缝附近恰有词间空隙会被切成两段，
                # 掉进左/右组的那半与整幅行同一水平带、同字号——并回 full，
                # 否则它会成为与整幅段落区域重叠的孤块（译文双层叠印）。
                ov = min(l["bottom"], f["bottom"]) - max(l["top"], f["top"])
                same_band = ov > 0.6 * min(lh, fh)
                if same_size and (same_band or (below and aligned)):
                    side.remove(l)
                    full.append(l)
                    changed = True
                    break


def parse_pdf(path: str, progress=None) -> List[PageLayout]:
    """解析整份 PDF。progress(msg, frac) 可选——OCR 扫描页较慢，逐页上报。"""
    from . import layout_model as _lm
    from . import ocr as _ocr

    engine = _ocr.OcrEngine(path) if _ocr.available() else None
    # T11 版面分析插件：models/ 下有 DocLayout-YOLO onnx 即自动启用（可选）
    lmodel = _lm.LayoutModel() if _lm.available() else None
    lm_marked = 0
    layouts = []
    try:
        with pdfplumber.open(path) as pdf:
            n = max(len(pdf.pages), 1)
            for i, page in enumerate(pdf.pages):
                layout = _parse_page(page, i, engine)
                if lmodel is not None:
                    dets = lmodel.detect(path, i)
                    if dets:
                        lm_marked += _lm.apply_to_layout(layout, dets)
                if progress and any(b.from_ocr for b in layout.blocks):
                    progress(f"第 {i + 1}/{n} 页为扫描版，已 OCR 识别 "
                             f"{len(layout.blocks)} 块", (i + 1) / n)
                layouts.append(layout)
    finally:
        if engine is not None:
            engine.close()
        if lmodel is not None:
            lmodel.close()
    if lm_marked and progress:
        progress(f"版面模型标记 {lm_marked} 个表格/独立公式块保留原样", 1.0)
    _mark_reference_blocks(layouts)   # 参考文献区保留原文原版式
    return layouts
