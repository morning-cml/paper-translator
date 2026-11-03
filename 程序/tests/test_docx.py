"""Word 文档翻译（A1）：内容覆盖、样式保真、双语模式、格式分派。"""
import subprocess
import sys

import pytest

from src.config import load_config
from src.pipeline import output_suffix, translate_document
from tests.conftest import ROOT

docx = pytest.importorskip("docx", reason="需要 python-docx")
DOC = ROOT / "samples" / "sample_doc.docx"


@pytest.fixture(scope="module")
def src_doc():
    if not DOC.exists():
        subprocess.run([sys.executable, "samples/make_docx_sample.py"],
                       cwd=str(ROOT), check=True, capture_output=True)
    if not DOC.exists():
        pytest.skip("无法生成 Word 样张")
    return str(DOC)


@pytest.fixture(scope="module")
def translated(src_doc, tmp_path_factory):
    out = tmp_path_factory.mktemp("docx") / "zh.docx"
    res = translate_document(src_doc, str(out), load_config(), mock=True)
    return docx.Document(str(out)), res


def _has_cjk(s):
    return any("一" <= c <= "鿿" for c in s)


def test_returns_docx_backend(translated):
    _, res = translated
    assert res["backend"] == "docx" and res["blocks"] > 10


def test_body_paragraphs_translated(translated):
    doc, _ = translated
    body = [p.text for p in doc.paragraphs if p.text.strip()]
    assert body and all(_has_cjk(t) for t in body), "正文应全部译为中文"


def test_heading_levels_preserved(translated):
    doc, _ = translated
    styles = [p.style.name for p in doc.paragraphs if p.text.strip()]
    assert sum(1 for s in styles if s.startswith("Heading") or s == "Title") >= 3


def test_list_bullets_preserved(translated):
    doc, _ = translated
    assert sum(1 for p in doc.paragraphs if p.style.name == "List Bullet") == 2


def test_table_cells_translated(translated):
    doc, _ = translated
    cells = [c.text for t in doc.tables for row in t.rows for c in row.cells]
    assert len(cells) == 9
    assert all(_has_cjk(c) for c in cells), "表格每格都应翻译"


def test_header_footer_translated(translated):
    doc, _ = translated
    s = doc.sections[0]
    assert _has_cjk(s.header.paragraphs[0].text)
    assert _has_cjk(s.footer.paragraphs[0].text)


def test_dominant_run_formatting_preserved(src_doc, tmp_path):
    """以加粗为主的段落，译文应仍是加粗（主导 run 承载译文）。"""
    d = docx.Document(src_doc)
    p = d.add_paragraph()
    p.add_run("x ")
    r = p.add_run("This entire sentence is emphasised for the reader.")
    r.bold = True
    mixed = tmp_path / "mixed.docx"
    d.save(str(mixed))

    out = tmp_path / "mixed_zh.docx"
    translate_document(str(mixed), str(out), load_config(), mock=True)
    last = docx.Document(str(out)).paragraphs[-1]
    carrier = [r for r in last.runs if r.text.strip()]
    assert carrier and carrier[0].bold, "主导加粗格式应保留"


def test_bilingual_appends_paragraphs(src_doc, tmp_path):
    out = tmp_path / "bi.docx"
    translate_document(str(src_doc), str(out), load_config(
        output_mode="bilingual"), mock=True)
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert any(not _has_cjk(t) for t in texts), "双语模式应保留原文段"
    assert any(_has_cjk(t) for t in texts), "双语模式应有译文段"
    src_n = len([p for p in docx.Document(src_doc).paragraphs if p.text.strip()])
    assert len(texts) > src_n, "双语模式段落数应增加"


@pytest.mark.parametrize("text,expected", [
    ("135", False), ("2026 — 135", False), ("—", False), ("", False),
    ("42%", False),
    ("Results", True), ("46 students", True),
    ("这已经是中文", False),          # 已是中文，不重复翻译
])
def test_worth_translating_rules(text, expected):
    from src.docx_translator import _worth_translating
    assert _worth_translating(text) is expected


def test_numeric_only_paragraph_untouched(src_doc, tmp_path):
    """纯数字/符号段落必须原样保留（不该被送去翻译）。"""
    d = docx.Document(src_doc)
    d.add_paragraph("135 — 2026")
    f = tmp_path / "nums.docx"
    d.save(str(f))
    out = tmp_path / "nums_zh.docx"
    translate_document(str(f), str(out), load_config(), mock=True)
    assert docx.Document(str(out)).paragraphs[-1].text.strip() == "135 — 2026"


@pytest.mark.parametrize("mode,ext,expected", [
    ("translated", ".pdf", "_translation"),
    ("sidebyside", ".pdf", "_translation_sidebyside"),
    ("translated", ".docx", "_translation"),
    ("sidebyside", ".docx", "_translation_bilingual"),   # DOCX 无左右对照
])
def test_output_suffix_rules(mode, ext, expected):
    assert output_suffix(mode, ext) == expected


def test_legacy_doc_rejected(tmp_path):
    from src.translator import TranslatorError
    f = tmp_path / "old.doc"
    f.write_bytes(b"fake")
    with pytest.raises(TranslatorError, match="另存为"):
        translate_document(str(f), str(tmp_path / "o.doc"), load_config())


# ---------------- 采集口径：去重与只读 ----------------
def _doc_with_table(path, n_lead: int):
    d = docx.Document()
    for i in range(n_lead):                 # 空段 / 纯数字段：不入选、代理会被释放
        d.add_paragraph("" if i % 2 else "12345")
    t = d.add_table(rows=10, cols=3)
    for r in range(10):
        for c in range(3):
            t.cell(r, c).text = f"Cell text number {r} {c}"
    d.save(str(path))


def test_table_cells_collected_after_many_skipped_paragraphs(tmp_path):
    """回归：去重曾按 id(元素) 记——被释放的代理地址被复用，id 撞上，
    表格段落被当成"见过"整片跳过。正文 300 个空段时 30 个单元格一个都没采到。"""
    from src.docx_translator import _collect
    f = tmp_path / "t.docx"
    _doc_with_table(f, 300)
    units = _collect(docx.Document(str(f)))
    assert sum(u.text.startswith("Cell text") for u in units) == 30


def test_merged_cells_still_deduplicated(tmp_path):
    """横向合并的单元格被 python-docx 重复返回，仍只能采一次。"""
    from src.docx_translator import _collect
    d = docx.Document()
    t = d.add_table(rows=1, cols=3)
    t.cell(0, 0).merge(t.cell(0, 2)).text = "Merged header cell text"
    f = tmp_path / "m.docx"
    d.save(str(f))
    texts = [u.text for u in _collect(docx.Document(str(f)))]
    assert texts.count("Merged header cell text") == 1


def test_no_header_parts_invented(tmp_path):
    """回归：读取未定义的页眉页脚会让 python-docx 当场新建空部件——一份无页眉
    的文档译完多出 6 个 header/footer 部件。翻译不该改动它没碰的结构。"""
    import zipfile
    d = docx.Document()
    d.add_paragraph("A plain paragraph that will be translated.")
    src = tmp_path / "plain.docx"
    d.save(str(src))
    out = tmp_path / "plain_zh.docx"
    translate_document(str(src), str(out), load_config(), mock=True)
    names = zipfile.ZipFile(str(out)).namelist()
    assert not [n for n in names if "header" in n or "footer" in n]


def test_defined_header_still_translated(tmp_path):
    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "Running header of the paper"
    d.add_paragraph("Body paragraph of the paper.")
    src = tmp_path / "h.docx"
    d.save(str(src))
    out = tmp_path / "h_zh.docx"
    translate_document(str(src), str(out), load_config(), mock=True)
    head = docx.Document(str(out)).sections[0].header.paragraphs[0].text
    assert _has_cjk(head)
