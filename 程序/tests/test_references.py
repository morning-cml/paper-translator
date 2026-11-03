"""参考文献区识别：不依赖论文文件的合成版面，CI 上也能跑。

真实论文回归（test_parser_real）只有 Science 的 "12. Kapur" 编号格式；ACM/IEEE
的 "[12] Kapur." 方括号格式此前没有任何用例覆盖——缺陷就是从这里溜进去的。
"""
from src.pdf_parser import Block, PageLayout, _mark_reference_blocks

H = 792.0


def _b(text, top, bold=False, page=0):
    return Block(text=text, x0=50, top=top, x1=300, bottom=top + 40, size=9.0,
                 page_index=page, bold=bold)


def _page(i, *blocks):
    L = PageLayout(page_index=i, width=612.0, height=H)
    L.blocks = list(blocks)
    return L


ENTRY = ("[{n}] Xuejun Du, Pengcheng An, and Jian Zhao. 2024. Understanding the "
         "Practices and Needs of Music Therapists. Proceedings of the ACM on "
         "Human-Computer Interaction 8, CSCW1 (2024), 1–25. "
         "[{m}] Brian Abrams. 2018. Understanding humanistic dimensions of "
         "music therapy. Music Therapy Perspectives 36, 2 (2018), 139–143. "
         "[{k}] Ann Smith. 2020. Another study of things. CHI 2020, 1–12.")


def test_running_header_does_not_end_reference_region():
    """回归（ACM CHI 论文实测）：页眉 "Sun et al." 夹在两页参考文献之间，被当
    成"不像引用的块"把状态切出引用区——后续两页约 1.6 万字符的条目全被送译。"""
    layouts = [
        _page(0, _b("REFERENCES", 400, bold=True),
              _b(ENTRY.format(n=1, m=2, k=3), 450)),
        _page(1, _b("Sun et al.", 60),                       # 页眉：贴顶、短、非粗体
              _b("and Needs of Music Therapists. Proceedings of the ACM 8, 2 "
                 "(2024), 1–25. https://doi.org/10.1145/3637", 100),
              _b(ENTRY.format(n=27, m=28, k=29), 300)),
    ]
    _mark_reference_blocks(layouts)
    body = [b for L in layouts for b in L.blocks if len(b.text) > 60]
    assert body and not any(b.translatable for b in body)
    assert layouts[1].blocks[0].translatable, "页眉本身照常翻译"


def test_bracket_entries_detected_without_heading():
    """没识别到 REFERENCES 标题时，方括号编号条目自身也要认得出。"""
    L = _page(0, _b(ENTRY.format(n=5, m=6, k=7), 100))
    _mark_reference_blocks([L])
    assert not L.blocks[0].translatable


def test_bold_appendix_heading_ends_reference_region():
    """参考文献之后的附录（粗体标题开头）必须照常翻译。"""
    layouts = [
        _page(0, _b("REFERENCES", 100, bold=True),
              _b(ENTRY.format(n=1, m=2, k=3), 150)),
        _page(1, _b("A PRE-DEFINED CASES DETAILS", 85, bold=True),
              _b("In our study, therapists mentioned similar ideas about "
                 "using different methods in 2019 and pages 3–4 of the guide "
                 "(doi pending).", 120)),
    ]
    _mark_reference_blocks(layouts)
    assert layouts[1].blocks[0].translatable
    assert layouts[1].blocks[1].translatable, "附录正文不得被当成引用续块"


def test_numbered_references_heading():
    """"7 REFERENCES" / "VII. References" 这类带章节号的标题也要认。"""
    for heading in ("7 REFERENCES", "VII. References", "References:"):
        layouts = [_page(0, _b(heading, 100, bold=True),
                         _b("Kapur M. Productive failure in learning math, "
                            "Cognitive Science 38 (2014) 1008-1022.", 150))]
        _mark_reference_blocks(layouts)
        assert not layouts[0].blocks[1].translatable, heading


def test_body_with_bracket_citations_still_translated():
    """正文里的 [12] 引用不得让整段被当成参考文献。"""
    L = _page(0, _b("Prior work [12] showed that robots help, and later "
                    "studies [13, 14] confirmed it in 2019 and 2021 across "
                    "classrooms [15]. We extend this line of work.", 200))
    _mark_reference_blocks([L])
    assert L.blocks[0].translatable
