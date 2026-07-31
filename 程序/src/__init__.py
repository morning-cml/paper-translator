"""论文翻译工具（Paper Translator）——保留排版的文档翻译。

主要模块：
    config          配置加载（API Key、模型、输出模式等）
    paths           运行路径解析（源码/打包、资源目录/用户数据目录）
    languages       语言方向定义（源 11 种含自动检测 × 目标 10 种）
    glossary        术语库加载与匹配
    pdf_parser      PDF 解析（文本块 + 坐标 + 字号；含 OCR 与版面模型接入）
    layout          共享重排引擎（断行禁则、绕图避障、自适应缩号）
    pdf_writer      译文回填 PDF（reportlab 覆盖，兜底后端）
    pdf_writer_fitz 译文回填 PDF（PyMuPDF 精确抹除，首选后端）
    docx_translator Word / PowerPoint / Markdown / TXT / SRT 翻译
    translator      翻译客户端（任意 OpenAI 兼容接口，含离线 Mock）
    quality         译文质量自检（截断/啰嗦/数字错漏/元话语/重复退化）
    pipeline        串联整个翻译流水线
    gui             Tkinter 经典界面（已冻结，仅作网页版启动失败时的备用）

版本号的唯一来源是 `version.py`，此处只做转发——两处各写一份必然会漂。
"""

from .version import __version__

__all__ = ["__version__"]
