"""端到端：两个后端 × 三种输出模式 × 试译模式，全部走 Mock（不联网、不计费）。"""
import pdfplumber
import pytest

from src.config import load_config
from src.pipeline import translate_pdf


def _cjk_words(page):
    return sum(1 for w in page.extract_words()
               if any("一" <= c <= "鿿" for c in w["text"]))


@pytest.mark.parametrize("backend", ["pymupdf", "reportlab"])
def test_backends_produce_chinese(sample_path, tmp_path, backend):
    out = tmp_path / f"{backend}.pdf"
    res = translate_pdf(sample_path, str(out),
                        load_config(render_backend=backend), mock=True)
    assert res["backend"] == backend
    assert out.exists()
    with pdfplumber.open(str(out)) as d:
        assert len(d.pages) == res["pages"]
        assert _cjk_words(d.pages[0]) > 5, "译文页应含中文"


def test_bilingual_doubles_pages(sample_path, tmp_path):
    out = tmp_path / "bi.pdf"
    res = translate_pdf(sample_path, str(out),
                        load_config(output_mode="bilingual"), mock=True)
    with pdfplumber.open(str(out)) as d:
        assert len(d.pages) == res["pages"] * 2


def test_sidebyside_makes_wide_pages(sample_path, tmp_path):
    out = tmp_path / "sbs.pdf"
    res = translate_pdf(sample_path, str(out),
                        load_config(output_mode="sidebyside"), mock=True)
    with pdfplumber.open(sample_path) as src, pdfplumber.open(str(out)) as d:
        assert len(d.pages) == res["pages"], "左右对照不增加页数"
        assert d.pages[0].width == pytest.approx(src.pages[0].width * 2, abs=1)
        assert d.pages[0].height == pytest.approx(src.pages[0].height, abs=1)


def test_updown_makes_tall_pages(sample_path, tmp_path):
    """上下对照：W×2H 长页。**页宽必须与原文一致**——它存在的全部理由就是
    在「适合宽度」下不缩水（左右对照的 2W 宽页在同样模式下只有一半大）。"""
    out = tmp_path / "ud.pdf"
    res = translate_pdf(sample_path, str(out),
                        load_config(output_mode="updown"), mock=True)
    with pdfplumber.open(sample_path) as src, pdfplumber.open(str(out)) as d:
        assert len(d.pages) == res["pages"], "上下对照不增加页数"
        assert d.pages[0].width == pytest.approx(src.pages[0].width, abs=1)
        assert d.pages[0].height == pytest.approx(src.pages[0].height * 2, abs=1)
        assert _cjk_words(d.pages[0]) > 5, "译文应在同一页上"


def test_trial_mode_limits_translated_pages(paper_path, tmp_path):
    out = tmp_path / "trial.pdf"
    translate_pdf(paper_path, str(out), load_config(max_pages=2), mock=True)
    with pdfplumber.open(str(out)) as d:
        assert _cjk_words(d.pages[1]) > 5, "前 2 页应已翻译"
        assert _cjk_words(d.pages[6]) == 0, "第 7 页应保持原文"


def test_untranslated_blocks_keep_original_layout(paper_path, tmp_path):
    """参考文献页：不译区必须原样保留英文（曾出现"没翻译却被重排"）。"""
    out = tmp_path / "refs.pdf"
    translate_pdf(paper_path, str(out), load_config(), mock=True)
    with pdfplumber.open(str(out)) as d:
        text = d.pages[12].extract_text() or ""
    assert "Kapur" in text or "Vanlehn" in text, "参考文献原文丢失"


def test_progress_and_result_contract(sample_path, tmp_path):
    msgs = []
    res = translate_pdf(sample_path, str(tmp_path / "o.pdf"), load_config(),
                        mock=True, progress=lambda m, f: msgs.append((m, f)))
    assert set(res) >= {"pages", "blocks", "output", "mode", "backend"}
    assert msgs and msgs[-1][1] == 1.0, "进度应以 100% 收尾"
    assert all(0.0 <= f <= 1.0 for _, f in msgs)


def test_cancel_raises(sample_path, tmp_path):
    from src.pipeline import CancelledError
    with pytest.raises(CancelledError):
        translate_pdf(sample_path, str(tmp_path / "x.pdf"), load_config(),
                      mock=True, should_cancel=lambda: True)
