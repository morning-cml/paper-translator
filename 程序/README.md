# 论文翻译工具 — 技术说明

[![测试](https://github.com/morning-cml/paper-translator/actions/workflows/test.yml/badge.svg)](https://github.com/morning-cml/paper-translator/actions/workflows/test.yml)

把文档翻译成另一种语言，**完整保留图片、公式、表格与排版**，译文落在原文对应
位置。支持格式：**PDF · Word(.docx) · PowerPoint(.pptx) · Markdown · TXT · SRT 字幕**。
语言方向：源语言 11 种（含自动检测）× 目标语言 10 种（中/英/日/韩/德/法/西/俄/葡/意，
见 `src/languages.py`）。另有扫描版 PDF（本地 OCR）、表格逐单元格翻译、双语对照输出、
译文质量自检、持久化翻译缓存、多翻译服务（任意 OpenAI 兼容接口）。

> **使用者请看外层的《使用说明.html》**（双击浏览器打开）。
> **想快速了解项目现状与下一步 → `docs/项目总览.md`**（现状快照 + 遗留问题
> + 未来任务排序）。**版本区别与每步优化 → 外层 `CHANGELOG.md`**。本文件面向
> 开发/维护者，讲架构与工程约定；完整开发史见 `docs/交接说明_HANDOFF.md`，
> 路线全景见 `docs/未来路线图.md`。

## 快速开始（开发者）

```bash
py -m pip install -r requirements.txt     # 依赖（含 OCR/DOCX；文件对话框用自带 tkinter）
py webui.py                               # 【正式】原生应用窗口（pywebview）
py webui.py --browser                     # 同一界面，改用系统浏览器（调试用）
py -m pytest tests -q                     # 测试套件（198 项）
py run_gui.py                             # 备用 tkinter 界面（已冻结，勿加新功能）
py translate_cli.py "论文.pdf" --mock     # 离线跑通版式（不花 token）
py translate_cli.py "论文.pdf" --pages 2  # 真实试译前 2 页
py selftest_backend.py                    # 回填后端自测（配 自测-PyMuPDF.bat）
py samples/make_scanned.py                # 生成扫描版样张 → 回归 OCR 管线
```

> **macOS / Linux**：把上面的 `py` 换成 `python3`；一键启动用根目录的
> `启动论文翻译.command`（首次可能要 `chmod +x` 一次）。源码在三平台通用。

## 架构（数据流）

```
解析 pdf_parser ──► 翻译 translator ──► 重排 layout ──► 写回 pdf_writer_fitz / pdf_writer
   │                    │                                        │
   ├ 分栏/分行/分段      ├ 批量+并发+重试+失败降级                 ├ fitz：redaction 精确抹除
   ├ 行内公式 ⟦Fn⟧ 保护  ├ 术语库注入 + 全文上下文                 │        + 公式矢量回贴
   ├ 加粗/颜色→标题层级  ├ 持久化缓存 transcache                  ├ reportlab：白底覆盖（兜底）
   ├ 参考文献区免译保排  ├ 占位符校验/强化重试                     ├ 共享重排：避障/禁则/两端
   ├ 扫描页 OCR 回灌     └ 译后 pangu 加空格                      │   对齐/向下扩展/缩号
   └ 版面模型(可选)                                              └ 双语前后页 / 左右对照
```

编排在 `pipeline.translate_pdf`：跨栏断句配对成翻译单元 → 成本预估 → 翻译 →
无中文译文的块保留原排版 → 选后端写回（PyMuPDF 失败自动回退 reportlab）。

## 模块一览

| 文件 | 职责 |
|---|---|
| `src/pdf_parser.py` | 【核心】词→行→栏→段；公式检测；标题层级；引用区识别；OCR 回灌 |
| `src/ocr.py` | 扫描页 OCR（RapidOCR，离线）；竖排字过滤、方向分类器关闭 |
| `src/layout_model.py` | 可选版面模型（DocLayout-YOLO onnx）：表格/图区/独立公式 |
| `src/translator.py` | DeepSeek/OpenAI 兼容客户端；批量并发；缓存；失败降级；Mock |
| `src/transcache.py` | 持久化翻译缓存（cache/translations.json，键含模型/领域/上下文） |
| `src/glossary.py` | 术语库加载与注入（整词/长短语优先） |
| `src/layout.py` | 共享重排引擎：断行/禁则/逐行避障/向下扩展/缩号/两端对齐 |
| `src/pdf_writer_fitz.py` | 【首选】精确抹除+CJK 嵌入+公式矢量回贴+双语拼页 |
| `src/pdf_writer.py` | 【兜底】reportlab 行矩形覆盖+位图回贴 |
| `src/textfix.py` | 译后中西文加空格（盘古之白） |
| `src/docx_translator.py` | Word 文档翻译（按 run 就地替换，样式天然保留） |
| `src/pptx_translator.py` | PowerPoint 翻译（文本框/表格/备注，样式保真） |
| `src/text_translator.py` | Markdown / TXT / SRT（结构与代码块用占位符保护） |
| `src/quality.py` | 译文质量自检（截断/啰嗦/数字错漏/元话语/重复退化） |
| `src/languages.py` | 多语言注册中心（语言表/目标语判定/长度带/模型推荐） |
| `src/pipeline.py` | 编排；格式分派；跨栏配对；成本预估；后端选择与回退 |
| `src/gui.py` | tkinter 界面（服务预设/领域/试译/三种输出模式） |
| `src/config.py` | 配置加载/保存（ROOT = src/ 上一级） |

## 目录结构

```
Translation/                       # 外层 = 用户桌面工作区
├─ 启动论文翻译.bat                # 【唯一入口】起本地服务并打开浏览器
├─ 使用说明.html                   # 用户手册
└─ 程序/
   ├─ webui.py                     # 【正式界面】网页版本地服务（标准库 HTTP，仅 127.0.0.1）
   ├─ web/                         # 前端：index.html + css/js + themes/（皮肤包）
   ├─ run_gui.py + src/gui.py      # 【已冻结】tkinter 备用界面，入口 备用-经典界面.bat
   ├─ translate_cli.py / selftest_backend.py
   ├─ 备用-经典界面.bat / 自测-PyMuPDF.bat
   ├─ requirements.txt / config.json(含Key) / config.example.json
   ├─ glossary/cs_terms.csv        # 术语库（可编辑）
   ├─ models/…onnx                 # 版面模型（删除即关闭该增强）
   ├─ cache/translations.json      # 翻译缓存（删除即重新计费）
   ├─ fonts/                       # （可选）思源字体，自动嵌入
   ├─ samples/make_sample.py / make_scanned.py + 样张
   ├─ selftest_out/                # 自测输出
   ├─ docs/项目总览.md             # 【入口】现状快照 + 遗留问题 + 未来任务快照
   ├─ docs/后续规划.md             # 【规划】v1.0 后的六大主题 + 推进顺序
   ├─ docs/交接说明_HANDOFF.md     # 完整开发史/算法细节/已知限制
   ├─ docs/前端设计.md             # 网页前端架构（分层/皮肤契约/API/扩展路线）
   ├─ docs/未来路线图.md           # 历史决策与调研（许可分析/竞品，已归档）
   ├─ docs/第三方许可.md           # 依赖许可清单与 AGPL 约束
   ├─ docs/archive/                # 已完成的历史任务卡
   └─ src/                         # 见模块一览
```

## 打包发布（build.py）

```bash
py build.py                     # 完整版（含 OCR）→ release/v1.0.0-full/
py build.py --profile lite      # 精简版（无 OCR，体积小很多）
py build.py --set-version 1.1.0 # 改版本号后再构建
py build.py --zip               # 额外产出 zip
py build.py --overwrite         # 重建同一版本（旧产物归档而非删除）
py build.py --list              # 查看已构建的历史版本
py build.py --clean             # 清理中间产物（只动 build/，不动 release/）
```

- **跨平台打包**：Windows 上产出 `.exe` 文件夹；**macOS 上产出 `.app`**
  （`-mac-arm64` zip，用 ditto 压缩以保住 bundle 签名与权限）。PyInstaller 不能
  交叉编译——各平台的包只能在对应平台构建；CI（`.github/workflows/release.yml`）
  已配 Windows + macOS(Apple 芯片) 打 tag 自动构建。Intel Mac 需在 Intel 机器上
  跑 `python3 build.py`。
- **版本唯一真源**：`src/version.py`；Windows 文件属性、界面关于、
  `build_info.json` 全部读它。
- **多版本互不干扰**：产物落在 `release/v<版本>-<profile>/`，含 exe 文件夹、
  zip、`SHA256SUMS.txt`、`build_info.json`（版本/git 提交/时间/profile）。
- **留记录，不留字节**：`release/` 下**只保留每个 profile 的最新一次构建**，
  更早的目录在新构建自检通过后自动删除（`--keep-old` 关闭）——它们动辄几百 MB，
  且都能凭台账记的来源提交重建。安全网：目标目录已存在时**默认中止**并提示改
  版本号，要重建须显式 `--overwrite`；且旧产物是先改名暂存、等新产物**自检通过
  才真删**，构建失败则留在 `release/.replaced-<名字>-<时间戳>/` 可手工还原。
- **版本台账只增不减**：`release/releases.json`（数据）与 `RELEASES.md`（人读，
  由前者渲染）都**纳入 git**（`.gitignore` 里单独取反）。二进制不入库、走 GitHub
  Releases，但每一次构建的"版本/时间/来源提交/体积"永久可查；产物删掉后记录
  仍在、标为「已清理」。**删产物可以，删记录不行**——台账是"还能不能重建"的
  唯一依据。注意标了 ⚠️脏工作区 的构建当时有未提交改动，只能等价重建、不保证
  字节一致。功能差异见 `../CHANGELOG.md`。
- **图标自动生成**：从皮肤里的原创吉祥物 SVG 渲染成多尺寸 `.ico`，无需美术资源。
- **路径隔离**（`src/paths.py`）：只读资源走 `sys._MEIPASS`；配置/缓存/模型/
  字体写到 **exe 同级 `data/`**（便携），该处不可写时退回 `%APPDATA%`。
  ⚠️ 用户数据绝不能落在 `_MEIPASS`——那是临时目录，退出即删。
  改程序名时**必须同步加目录迁移**（`_adopt_legacy_dir`）：老用户的 Key 和
  花过钱的翻译缓存都按旧名存着，`paths.APP_NAME` 因此故意不从 `version.py` 取。
- **关于"防逆向"**：本项目为 AGPL-3.0，分发时必须提供完整源码，故混淆无意义
  且可能违反许可，`--obfuscate` 会被显式忽略并给出提示。正当加固为：不打包
  任何密钥、入包的是字节码、产出 SHA256 清单、预留 `--sign` 代码签名钩子
  （签名才是对抗"被二次打包投毒"的正解）。

## 界面策略（2026-07-19 起）

**网页版是唯一正式界面**，新功能只加在 `web/` + `webui.py`。tkinter 版
（`run_gui.py` + `src/gui.py`）**已冻结**为应急备胎：功能停在 2026-07-19
（T1–T12 齐备），不再跟进新特性，入口不在顶层暴露（`程序/备用-经典界面.bat`）。
> 退役原因：双端并行导致同一功能写两遍，已实际引发过两端不一致的缺陷；
> 且皮肤化与后续商业化只有网页端能承载。若确需改动 gui.py，务必同步
> `web/js/components.js` 并跑双端 SERVICES 对照校验。

## 网页前端（webui.py + web/）

零新依赖（标准库 HTTP 服务，仅绑定 127.0.0.1）。分层与皮肤契约详见
`docs/前端设计.md`：皮肤 = `web/themes/<id>/theme.css` 一个文件（只准写
CSS 变量与装饰），`themes.json` 注册即出现在界面切换器；组件层不直接
fetch、组件间经 store 通信；将来云端化只需改 `api.js` 的 BASE。
已含两套皮肤：`sponge`（海绵泡泡·原创卡通）与 `clean`（极简样板）。

## 测试

- **一键自测**：双击 `自测-PyMuPDF.bat`——Mock 跑 15 页真实论文、校验页数/
  文字层、导出前后对比 PNG 到 `selftest_out/`。
- **扫描版回归**：`py samples/make_scanned.py && py translate_cli.py
  samples/sample_scanned.pdf --mock`。
- **改内核必做**：ASCII 密集压力测试（Mock near纯中文测不出 ASCII 宽度/溢出
  类缺陷，教训见 handoff §8.11），或真实 Key `--pages 2` 抽译目检。
- 渲染检查：`pdfplumber` 的 `page.to_image(resolution=140).save(...)`。

## 许可证：AGPL-3.0

本项目以 **GNU Affero GPL v3** 发布（全文见根目录 `LICENSE`，合规说明见
`NOTICE.md`）。选择理由：首选回填后端 PyMuPDF 与可选的 DocLayout-YOLO 版面
模型均为 AGPL-3.0，衍生作品必须采用兼容许可。

对开发者的三条硬约束：

1. 任何**分发版本**与任何**联网提供的服务**（AGPL 第 13 条）都必须附带完整源代码；
2. **不得闭源分发**——若将来确需闭源，须先移除 AGPL 组件（PyMuPDF 降为用户
   自装的可选增强、停用版面模型），或购买 Artifex / Ultralytics 商业授权；
3. **新引入依赖前先核许可**，优先 MIT / BSD / Apache 2.0，并更新
   `docs/第三方许可.md`。

商业模式为「开源 + 增值服务」（云端额度、皮肤、企业支持、私有化部署），
这些均不改变本程序的 AGPL 属性。

## 已知限制（详见 handoff §8）

- 旋转页整文件走 reportlab 兜底；扫描版倒置页不自动纠正、深色纸背露白。
- reportlab 兜底不删原文字层（白框遮盖）。
- OCR 个别行丢词间空格（翻译模型通常能正确理解）。
- 数字两侧空格是否保留交给模型，全篇不保证完全统一。
- 术语库随领域切换尚未自动换库（领域只影响提示词）。
