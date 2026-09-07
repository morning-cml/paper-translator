"""版面保真度量化评测（借鉴 BabelDOC 论文的评测方法）。

**为什么需要**：本项目此前判断"排版改得好不好"全靠导出 PNG 用眼睛看。
`quality.py` 管的是**译文文本**（截断/啰嗦/数字错漏），版面这一侧一直没有
任何数字——于是每次动排版内核都只能凭感觉，回归测试也卡不住"字号被系统性
压平""块被挤出框"这类整体性劣化。

BabelDOC（ACL 2026 Demo）的评测里给了两个可直接照搬的量：

* **BIoU**（BBox IoU）：原文与译文版面元素在归一化坐标下的几何重叠度。
  论文里 BabelDOC 50.0% / PDFMathTranslate 48.7% / DeepL 19.8%——注意这个
  绝对值天生不高（译文长度变了，框必然动），**它的用法是纵向自比**：
  同一份文档、同一套流程，改动前后跑一次，数字掉了就是版面退化了。
* **UTB**（Untranslated Text Blocks）：输出里仍是源语言的块数，每页计。
  用来抓"整块漏译"——那类缺陷 BIoU 反而看不出来（没翻的块框一动不动，
  IoU 恰好是 1.0，越漏译分越高）。两个数必须一起看。

**只对 `translated`（纯译文）模式有意义**：双语/左右/上下对照的输出页几何
本来就和原文不同，比 IoU 没有意义。

用法：
    py -m src.metrics 原文.pdf 译文.pdf
    py -m src.metrics 原文.pdf 译文.pdf --json
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]   # 归一化后的 (x0, top, x1, bottom)

# IoU 达到多少算"这个块找到了对应"。0.5 是检测任务的通用惯例。
MATCH_THRESHOLD = 0.5


def _iou(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0.0 else 0.0


def page_boxes(path: str, pages: Optional[int] = None) -> Dict[int, List[dict]]:
    """解析 PDF，返回 {页号: [{box: 归一化框, text: 文本}]}。

    两侧都走**同一个解析器**（`pdf_parser.parse_pdf`），这是刻意的：比的是
    "同一套眼睛看到的版面"，任何分块口径的偏差在两侧同时出现、会互相抵消。
    换成外部工具反而引入不可控的第三方差异。
    """
    from .pdf_parser import parse_pdf
    out: Dict[int, List[dict]] = {}
    for L in parse_pdf(path):
        if pages is not None and L.page_index >= pages:
            break
        w = max(float(L.width), 1.0)
        h = max(float(L.height), 1.0)
        out[L.page_index] = [
            {"box": (b.x0 / w, b.top / h, b.x1 / w, b.bottom / h),
             "text": b.text or ""}
            for b in L.blocks
            if (b.x1 - b.x0) > 0 and (b.bottom - b.top) > 0
        ]
    return out


def biou_from_boxes(src: Dict[int, List[dict]],
                    out: Dict[int, List[dict]]) -> dict:
    """已解析好的两侧版面 → BIoU 指标（纯函数，便于测试）。"""
    per_page: List[dict] = []
    scores: List[float] = []
    for pno in sorted(src):
        a = [d["box"] for d in src[pno]]
        b = [d["box"] for d in out.get(pno, ())]
        if not a:
            continue
        # 每个原文块取"和它重叠得最好的那个译文块"的 IoU。不做一对一匹配：
        # 译文重排本来就可能把一块拆成两块或并成一块，强行一对一会误判为退化。
        best = [max((_iou(x, y) for y in b), default=0.0) for x in a]
        per_page.append({
            "page": pno,
            "blocks": len(a),
            "biou": sum(best) / len(best),
            "matched": sum(1 for s in best if s >= MATCH_THRESHOLD) / len(best),
        })
        scores.extend(best)
    return {
        "pages": len(per_page),
        "blocks": len(scores),
        "biou": (sum(scores) / len(scores)) if scores else 0.0,
        "matched": (sum(1 for s in scores if s >= MATCH_THRESHOLD)
                    / len(scores)) if scores else 0.0,
        "per_page": per_page,
    }


def untranslated_blocks(out: Dict[int, List[dict]], target_code: str = "zh",
                        min_chars: int = 12) -> dict:
    """UTB：输出里仍不含目标语文字的**正文长度**块数（每页均值一并给出）。

    短块（页码、图号、公式残片、专名）本来就不该翻，计进去全是噪声，故只看
    长度够 `min_chars` 的块。这个数**必须与 BIoU 同看**：漏译的块框纹丝不动、
    IoU 满分，只盯 BIoU 会把"整块没翻"读成"版面完美"。
    """
    from .languages import looks_like
    total = flagged = 0
    for pno in sorted(out):
        for d in out[pno]:
            t = (d["text"] or "").strip()
            if len(t) < min_chars:
                continue
            total += 1
            if not looks_like(t, target_code):
                flagged += 1
    pages = max(len(out), 1)
    return {"blocks": total, "untranslated": flagged,
            "ratio": (flagged / total) if total else 0.0,
            "per_page": flagged / pages}


def evaluate(src_path: str, out_path: str, target_code: str = "zh",
             pages: Optional[int] = None) -> dict:
    """跑一次完整评测。仅适用于 `translated`（纯译文）模式的输出。"""
    a = page_boxes(src_path, pages)
    b = page_boxes(out_path, pages)
    res = biou_from_boxes(a, b)
    res["utb"] = untranslated_blocks(b, target_code)
    return res


def format_report(res: dict) -> str:
    utb = res["utb"]
    lines = [
        "版面保真评测（纵向自比：同一文档改动前后对照看，不与他人横比）",
        f"  BIoU            {res['biou']:.3f}   "
        f"（{res['blocks']} 个原文块 / {res['pages']} 页）",
        f"  匹配率(IoU≥{MATCH_THRESHOLD})  {res['matched']:.1%}   "
        "有对应译文块的原文块占比",
        f"  UTB             {utb['untranslated']}/{utb['blocks']} "
        f"（{utb['ratio']:.1%}，每页 {utb['per_page']:.2f} 块）"
        "   仍是源语言的正文块",
    ]
    worst = sorted(res["per_page"], key=lambda p: p["biou"])[:3]
    if worst:
        lines.append("  最差的三页：" + "、".join(
            f"第 {p['page'] + 1} 页 {p['biou']:.2f}" for p in worst))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="版面保真评测（BIoU + UTB）。只对纯译文模式的输出有意义。")
    ap.add_argument("source", help="原文 PDF")
    ap.add_argument("output", help="译文 PDF（--mode translated 的产物）")
    ap.add_argument("--target", default="zh", help="目标语代码（默认 zh）")
    ap.add_argument("--pages", type=int, default=None, help="只评测前 N 页")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非人读报告")
    args = ap.parse_args(argv)

    res = evaluate(args.source, args.output, args.target, args.pages)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json
          else format_report(res))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
