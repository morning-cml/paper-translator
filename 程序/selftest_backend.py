"""回填后端自测（在你的 Windows 机器上运行，验证 PyMuPDF 精确抹除路径）。

用法：
    双击 自测-PyMuPDF.bat
    或   python selftest_backend.py

做什么：
  1. 检查依赖（pdfplumber / reportlab / pypdf / requests 必需；pymupdf 推荐）
  2. 用离线 Mock 翻译跑通整条流水线（不联网、不花 token）
     · 有 pymupdf → 用「PyMuPDF 精确抹除」后端
     · 没有       → 提示安装命令，并用 reportlab 兜底后端跑
  3. 校验输出页数、文字层（原文应被删除/覆盖、中文应写入）
  4. 把原文页与译文页渲染成 PNG 存到 selftest_out/ 供目检
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "selftest_out"
# 真实论文放在外层用户目录（程序/ 的上一级），找不到再看本目录
_PAPER_NAME = "Observing a robot peer's failures facilitates students' classroom learning.pdf"
PAPER = (ROOT.parent / _PAPER_NAME) if (ROOT.parent / _PAPER_NAME).exists() else (ROOT / _PAPER_NAME)
SAMPLE = ROOT / "samples" / "sample_paper.pdf"


def say(msg: str):
    print(msg, flush=True)


def main() -> int:
    say("=" * 62)
    say("论文翻译工具 · 回填后端自测")
    say("=" * 62)

    # ---- 1. 依赖 ----
    missing = []
    for mod in ("pdfplumber", "reportlab", "pypdf", "requests"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    if missing:
        say(f"[x] 缺少必需依赖：{', '.join(missing)}")
        say(f"    请运行：python -m pip install {' '.join(missing)}")
        return 1
    say("[✓] 基础依赖齐全（pdfplumber / reportlab / pypdf / requests）")

    has_fitz = True
    try:
        import fitz  # noqa: F401
        ver = getattr(fitz, "__version__", None) or \
            getattr(fitz, "VersionBind", "?")
        say(f"[✓] pymupdf 已安装（{ver}）→ 将测试「PyMuPDF 精确抹除」后端")
    except Exception:  # noqa: BLE001
        has_fitz = False
        say("[!] 未安装 pymupdf → 本次只能测 reportlab 兜底后端。")
        say("    安装精确回填组件：python -m pip install pymupdf fonttools")

    # ---- 2. 选测试文件 ----
    if PAPER.exists():
        src_pdf = PAPER
    elif SAMPLE.exists():
        src_pdf = SAMPLE
    else:
        say("[x] 找不到测试 PDF（15 页论文或 samples/sample_paper.pdf）")
        return 1
    say(f"[✓] 测试文件：{src_pdf.name}")

    # ---- 3. 跑流水线（Mock，离线）----
    sys.path.insert(0, str(ROOT))
    from src.config import load_config
    from src.pipeline import translate_pdf

    OUT_DIR.mkdir(exist_ok=True)
    out_pdf = OUT_DIR / (src_pdf.stem + "_selftest.pdf")
    backend_req = "pymupdf" if has_fitz else "reportlab"
    cfg = load_config(render_backend=backend_req)

    def progress(msg, frac):
        say(f"    [{int(frac * 100):3d}%] {msg}")

    try:
        res = translate_pdf(str(src_pdf), str(out_pdf), cfg,
                            mock=True, progress=progress)
    except Exception as e:  # noqa: BLE001
        say(f"[x] 流水线失败：{e}")
        traceback.print_exc()
        return 2
    say(f"[✓] 流水线完成：{res['pages']} 页 / {res['blocks']} 段 / "
        f"后端 {res['backend']}")
    if has_fitz and res["backend"] != "pymupdf":
        say("[!] 注意：pymupdf 已安装但实际用了兜底后端（见上方日志原因）")

    # ---- 4. 校验输出 ----
    import pdfplumber
    ok = True
    with pdfplumber.open(str(src_pdf)) as a, pdfplumber.open(str(out_pdf)) as b:
        pages_expected = len(a.pages) * (2 if cfg.output_mode == "bilingual" else 1)
        if len(b.pages) != pages_expected:
            say(f"[x] 页数不符：{len(b.pages)} != {pages_expected}")
            ok = False
        else:
            say(f"[✓] 页数正确（{len(b.pages)}）")
        pi = min(1, len(b.pages) - 1)
        src_ascii = sum(1 for w in a.pages[pi].extract_words()
                        if all(ord(c) < 128 for c in w["text"]))
        out_words = b.pages[pi].extract_words()
        out_ascii = sum(1 for w in out_words
                        if all(ord(c) < 128 for c in w["text"]))
        out_cjk = sum(1 for w in out_words
                      if any("一" <= c <= "鿿" for c in w["text"]))
        say(f"[i] 第 {pi + 1} 页词统计：原文英文词 {src_ascii} → 译后残留英文词 "
            f"{out_ascii}，中文词 {out_cjk}")
        if out_cjk < 10:
            say("[x] 译文页几乎没有中文，异常")
            ok = False
        if res["backend"] == "pymupdf" and src_ascii and out_ascii > 0.55 * src_ascii:
            say("[!] 英文残留偏多——精确抹除可能未生效，请把本输出发回排查")
        # 渲染 PNG 供目检
        try:
            a.pages[pi].to_image(resolution=110).save(
                str(OUT_DIR / f"page{pi + 1}_原文.png"))
            b.pages[pi].to_image(resolution=110).save(
                str(OUT_DIR / f"page{pi + 1}_译文.png"))
            say(f"[✓] 已导出对比图：selftest_out/page{pi + 1}_原文.png / _译文.png")
        except Exception as e:  # noqa: BLE001
            say(f"[!] PNG 导出失败（不影响主功能）：{e}")

    say("-" * 62)
    if ok:
        say("自测通过 ✔  请打开 selftest_out/ 目检两张 PNG：")
        say("  · 中文应落在原文对应位置，图片/公式保留")
        say("  · PyMuPDF 后端：无白色遮盖块（深色背景也干净）")
        say(f"  · 也可直接打开译文 PDF：{out_pdf}")
    else:
        say("自测发现问题 ✘  请把上面的完整输出发回给开发者")
    return 0 if ok else 3


if __name__ == "__main__":
    code = main()
    try:
        input("\n按回车键退出…")
    except EOFError:
        pass
    sys.exit(code)
