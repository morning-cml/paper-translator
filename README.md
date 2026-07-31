# 论文翻译工具

[![测试](https://github.com/morning-cml/paper-translator/actions/workflows/test.yml/badge.svg)](https://github.com/morning-cml/paper-translator/actions/workflows/test.yml)
[![许可](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![下载](https://img.shields.io/github/v/release/morning-cml/paper-translator?label=下载)](https://github.com/morning-cml/paper-translator/releases/latest)

把文档翻译成另一种语言，**图片、公式、表格与排版原样保留**，译文就落在原文
的位置上。面向科研论文，非程序员双击即用。

一般的翻译工具要么只给你纯文本，要么把版式冲得七零八落。本工具的做法是：精确
抹除原文字符、把译文按原排版重新排进去、公式以矢量图回贴、图表位置逐行避让，
所以译完的 PDF 看起来还是原来那篇论文，只是文字变成了你看得懂的语言。

---

## 下载使用（普通用户）

支持 **Windows** 与 **macOS**，都分「完整版 full」「精简版 lite」两档：

| | 完整版 full | 精简版 lite |
|---|---|---|
| 扫描版 / 图片型 PDF | ✅ 内置离线 OCR | ❌ 不支持 |
| 其余全部功能 | 完全一致 | 完全一致 |
| 体积（解压后） | 约 315 MB | 约 150 MB |

> **怎么判断要不要 full**：用阅读器打开 PDF，试着**选中文字**。选得中就用 lite
> 足够；选不中、只能整块框选的是扫描件（纸质书扫描、拍照转的 PDF），才需要 full。

### Windows

到 [**Releases**](https://github.com/morning-cml/paper-translator/releases/latest)
下载 zip，解压后双击 `论文翻译工具.exe` 即可，**不需要装 Python**。

> **⚠️ 整个文件夹一起用**：exe 本体只有 14 MB，真正的程序在旁边的 `_internal\`
> 文件夹里。只拷 exe 出来是跑不起来的。

### macOS（Apple 芯片）

到 [**Releases**](https://github.com/morning-cml/paper-translator/releases/latest)
下载文件名带 **`-mac-arm64`** 的 zip，解压出「论文翻译工具」文件夹，里面是
`论文翻译工具.app` 与说明文档。与 Windows 版**同源构建、同一次 CI 同步发布**，
**不需要装 Python**。

> **⚠️ 第一次打开一定会被系统拦下，这是正常的**——本工具没有购买苹果的开发者
> 认证（每年几百块），不是软件有问题。按
> [《Mac 使用说明》](程序/docs/Mac使用说明.md) 放行**一次**即可（约 2 分钟），
> 之后就能像普通软件一样直接双击。
>
> 你会看到「**无法验证开发者**」或「**已损坏，无法打开**」两种提示之一，说明里
> 分别对应第 1 步和第 2 步。⚠️ **别点「移到废纸篓」。**

**Intel Mac** 暂不提供打包版，请见下方 [从源码运行](#从源码运行开发者)。

---

**首次运行**会引导你配置翻译服务，三选一：申请 API Key（推荐 DeepSeek，便宜）、
装 [Ollama](https://ollama.com/) 完全免费离线跑、或先离线试用看排版效果。程序不含
任何内置密钥，你的 Key 只存在自己电脑上。详细用法见包内《使用说明.html》。

---

## 能做什么

**支持格式**

| 格式 | 说明 |
|---|---|
| **PDF** | 核心能力。分栏、公式、表格、图片、参考文献全部保排 |
| **Word** `.docx` | 按 run 就地替换，标题层级 / 项目符号 / 表格 / 页眉页脚天然保留 |
| **PowerPoint** `.pptx` | 文本框、表格、演讲者备注，样式保真 |
| **Markdown** / **TXT** / **SRT** | 代码块、链接地址、字幕时间轴用占位符保护，绝不被改动 |

**语言方向**：源语言 11 种（含自动检测）× 目标语言 10 种——中 / 英 / 日 / 韩 /
德 / 法 / 西 / 俄 / 葡 / 意。选定目标语后界面会推荐更合适的翻译服务。

**翻译服务**：DeepSeek · Kimi · 智谱 GLM · 豆包 · OpenAI · **Ollama 本地**
· 任意 OpenAI 兼容接口。Ollama 完全免费且不联网。

**排版保真**（核心竞争力）
- 精确抹除原文字符，深色背景零露白；PyMuPDF 为主、reportlab 兜底自动回退
- 行内公式检测 → 占位保护 → 译后校验 → **矢量回贴**，公式不会糊成图片
- 中文断行禁则、逐行避障绕图、向下扩展、自适应缩号、两端对齐
- 数据驱动分栏、标题层级还原、参考文献免译保排、跨栏跨页断句缝合
- 表格**逐单元格**翻译，译文不越格

**输出**：纯译文 / 双语·前后页 / 双语·左右对照宽页，三选一。

**省钱与质量**
- 持久化缓存：同文档重跑几乎零成本
- 术语库注入 + 全文语境注入，专业名词统一
- 译文质量自检：截断 / 啰嗦 / 数字错漏 / 元话语 / 重复退化五类检查，命中则定向重译
- 失败降级：单批失败不中断，重跑自动补齐（已译段走缓存不重复计费）
- 成本预估与试译模式（只翻前 N 页）

---

## 从源码运行（开发者）

```bash
git clone https://github.com/morning-cml/paper-translator.git
cd paper-translator/程序
py -m pip install -r requirements.txt
py webui.py                                # 启动（原生应用窗口）
py -m pytest -q                            # 测试套件（198 项）
py translate_cli.py "论文.pdf" --mock      # 离线跑通版式，不花 token
```

打包成 exe：

```bash
py build.py --profile full --zip           # 完整版
py build.py --profile lite --zip           # 精简版
py build.py --list                         # 查看已构建版本
```

**架构与模块说明 → [`程序/README.md`](程序/README.md)**

---

## 文档

| 文档 | 读者 | 内容 |
|---|---|---|
| [`程序/README.md`](程序/README.md) | 开发者 | 架构、模块表、打包、测试、工程约定 |
| [`程序/docs/项目总览.md`](程序/docs/项目总览.md) | 全体 | 现状快照 + 遗留问题 + 未来任务排序 |
| [`程序/docs/Mac使用说明.md`](程序/docs/Mac使用说明.md) | Mac 用户 | 在 Mac 上第一次打开 `.app` 的放行步骤 |
| [`CHANGELOG.md`](CHANGELOG.md) | 全体 | 每个版本相比上一版新增了什么 |
| [`程序/docs/后续规划.md`](程序/docs/后续规划.md) | 决策者 | 六大主题逐项标价值/工作量/风险 |
| [`程序/docs/交接说明_HANDOFF.md`](程序/docs/交接说明_HANDOFF.md) | 维护者 | 完整开发史与踩坑记录 |
| [`程序/docs/第三方许可.md`](程序/docs/第三方许可.md) | 合规 | 依赖许可清单 |

---

## 许可

本项目采用 **[AGPL-3.0](LICENSE)**。你可以自由使用、修改、再分发，但**分发
（含通过网络提供服务）时必须以同样许可提供完整源代码**。

之所以是 AGPL 而非更宽松的许可：核心依赖 PyMuPDF 与 DocLayout-YOLO 本身即
AGPL，必须兼容。依赖清单与各自许可见
[`程序/docs/第三方许可.md`](程序/docs/第三方许可.md)，第三方声明见
[`NOTICE.md`](NOTICE.md)。

---

## 致谢

站在这些项目的肩膀上：[PyMuPDF](https://github.com/pymupdf/PyMuPDF) ·
[pdfplumber](https://github.com/jsvine/pdfplumber) ·
[reportlab](https://www.reportlab.com/) ·
[RapidOCR](https://github.com/RapidAI/RapidOCR) ·
[python-docx](https://github.com/python-openxml/python-docx) ·
[python-pptx](https://github.com/scanny/python-pptx) ·
[pywebview](https://github.com/r0x0r/pywebview)
