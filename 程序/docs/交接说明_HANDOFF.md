# 论文翻译工具 — 项目交接说明（开发史）

> **读之前先知道两处改名**（2026-07-30）：程序名 `PDF翻译工具` → `论文翻译工具`，
> 启动脚本 `启动PDF翻译.bat/.command` → `启动论文翻译.bat/.command`。
> **下文按日期记录的原文一律保留当时的旧名**——它们是历史，改了就对不上当时的
> 提交与截图。看到旧名请自行代入新名。当前的目录结构与命名以 `../README.md` 为准。

> 目的：把英文科研论文 PDF 翻译成中文，**尽量保留图片、公式、排版**，译文落在原文对应位置；用**术语库**提升准确率；用 **DeepSeek** 翻译；带**图形界面**，非程序员可双击运行。
> 现状（2026-07 第二轮迭代后）：端到端可用。**回填内核已完成升级**——新增 PyMuPDF 精确抹除后端（首选）+ 共享重排引擎（避障绕图/中文禁则/向下扩展/自适应缩号）+ 行内公式占位符保护（检测→保护→矢量回贴），reportlab 覆盖方案降级为兜底后端。已在 15 页 Science Robotics 真实论文上离线回归（--mock）。
>
> **2026-07-17 真机验证补记**：PyMuPDF 后端已在用户 Windows 11 + Python 3.11.9 + pymupdf 1.27.2.3 上**真实运行通过**——离线自测 15 页/96 段/后端 pymupdf；第 2 页原文 987 英文词、译后仅残留 13 词（精确抹除生效）；38 处行内公式 `show_pdf_page` 全部矢量回贴成功（38/38，无静默失败）；旋转页（rotation=90）按预期抛 BackendUnsupported 并整体回退 reportlab；p1 混排/p3 绕图/p6 公式密集页目检通过。并已用真实 DeepSeek Key 完成整篇论文翻译（deepseek-v4-pro，pymupdf 后端，无占位符校验失败）。**目录已重组**：外层只保留 `启动PDF翻译.bat` 唯一入口，其余全部收入 `程序/`（见第 2 节）。
>
> **2026-07-18 真实翻译质量修复（第三轮）**：首次真实整篇翻译暴露出三个 Mock 回归测不出的严重缺陷（Mock 占位译文几乎纯中文且长度贴近原文），已全部修复：
> 1. **fitz 测宽/绘制字体不一致（主因）**：`insert_text(fontname="china-ss")` 实际嵌入 Song CID 字体，其 **ASCII 字形全宽 1em**；而排版测宽用的 `fitz.Font("china-ss")` 是比例宽度（≈0.47em/字符）——ASCII 串绘制比预留宽约一倍 → 压字叠印、行数暴涨、块溢出互叠。修复：`pdf_writer_fitz.py` 改**双字体**（ASCII 一律用 Base-14 `helv` 测宽+绘制，`_script_runs` 切段），测宽与出墨严格一致。
> 2. **分段阈值被超高行带抬高**：`_group_blocks` 的 `gap_limit=0.6*max(med,prev_h,cur_h)`，p1 作者行带高 27pt 把阈值抬到 16.2，吞掉作者行与摘要间 9.9pt 段隙 → 二者并成怪物块整体重排（作者名混入摘要）。修复：改 `0.6*max(med, min(prev_h,cur_h))`。
> 3. **整幅行中缝切半的孤块**：摘要某行恰在中缝有词隙被切成两半，右半不满足「正下方+左对齐」提升条件 → 成为与摘要区域重叠的孤块（译文双层叠印）。修复：`_promote_full_tails` 增加**同带提升**（同水平带+同字号并回 full 组）。
> 4. 另加**已放置区域避让**安全网（两后端）：同页每块排版时避让先前块已画的译文/公式矩形，任何残余重叠只会下移绕排，绝不叠印。
> 修复后 15 页解析零重叠块；ASCII 密集合成译文压力测试（保留引文编号/统计量/缩写）p1/p2/p6 目检干净；标准 Mock 自测回归通过（94 段）。
>
> **2026-07-18 扫描版 OCR 接入（第四轮，原第 9 节头号待办）**：新增 `src/ocr.py`（RapidOCR / onnxruntime，模型内置于 wheel、离线可用、无需显卡）。无文字层的页自动转 OCR，每个识别行伪装成带坐标的"词"回灌 pdf_parser 现有分栏/分行/分段管线；写回端对 OCR 块改用白底覆盖（原文是图像像素，redaction 抹不掉）并按原文行形状绕开照片。已用自造扫描样张（`samples/make_scanned.py` 把真实论文前 3 页渲成无文字层 PDF）离线回归：3 页全部 OCR 成功、分栏正确、绕图正常、零重叠块。踩坑与对策见第 4 节 ocr.py 与第 8 节。
>
> **2026-07-18 成品质量优化（第五轮，专业性 + 阅读体验）**：对照真实译文 PDF 目检后，做了三项排版级优化，显著提升专业观感：
> 1. **章节标题层级恢复**：原文用红色大标题（DISCUSSION）/黑色粗体子标题（Knowledge acquisition）/粗斜体次级标题构成层级，旧版全被拍平成正文。现 `_group_blocks` 在**加粗变化**（fontname 含 bold）与**颜色变化**处断块，把标题切成独立块 → 独立翻译、按**原色 + 合成加粗**回填（fitz：`render_mode=2`+`border_width=0.045`；reportlab：微偏移双描）。单词标题（DISCUSSION/RESULTS/METHODS）放宽 `_is_translatable` 使其可译。
> 2. **中西文自动加空格（盘古之白）**：新增 `src/textfix.py::pangu`，在汉字与"含字母的西文词"间插空格（`RF条件`→`RF 条件`、`主题S1-2和S1-3`→`主题 S1-2 和 S1-3`），纯数字不加（`研究1`/`16名` 保持），占位符 ⟦Fn⟧ 天然不受影响。pipeline 在译后、回填前统一应用。
> 3. 真实整篇复译目检（p1 红色"人机交互"/"引言"+粗体标题、p7 红色"讨论"+粗体子标题层级清晰、缩写全部带空格），标准 Mock 自测回归通过（132 段，较旧版 94 段增加是因标题现独立成块）。
>
> **2026-07-18 参考文献保护 + 尾页分栏修复（第六轮，用户指出"参考文献没翻译还格式混乱"）**：借鉴有道/知云等学术文档翻译工具的两条产品原则——**引用不译不动、只重排真正翻译了的内容**：
> 1. **参考文献区整体保留原文原版式**：旧版把引用条目当普通段落送翻译→模型原样退回英文→我们抹掉原文精排（悬挂缩进/斜体刊名/粗体卷号）重排成散文，"没翻译还毁版式"且白耗 token。新增 `_mark_reference_blocks`（pdf_parser 尾部后处理）：REFERENCES 标题（仍可译）或引用样块进入引用区，按"编号条目起始 `\d+. 大写`×3+年份"强规则、"编号开头+年份+页码区间"中规则、"区内续块（换栏/换页）年份+页码≥2 或含 doi"弱规则标记 `translatable=False`；任何散文块（如 Acknowledgments）立即退出状态；页脚（top>0.92H，含年份易误捕）豁免不改状态。
> 2. **末页分栏修复**：`_detect_split_x` 峰值门槛 8 → 4——p14 右栏只有 ~10 行（cov=6）被误判单栏，左右两栏拧成全宽行、译文阅读顺序错乱；中缝覆盖度实为 0，信号明确。误报仍由"中缝 ≤ 0.30×min(两峰)"相对条件兜住。
> 3. **无中文译文 → 保留原排版**（pipeline 安全网）：译文不含任何 CJK = 模型原样退回（引用/URL/人名/占位符回退）→ `translation=None`，写回端完全不动该块。从机制上杜绝"未翻译却被重排"这一类缺陷。
> 效果：p13-14 引用区原版精排原样保留（对比图确认），p14 恢复双栏、致谢/资助/日期各归其位照常翻译；正文 129 段可译（引用两大块不再耗 token）。
>
> **2026-07-18 十二项工程升级（第七轮，任务清单见 `TASKS.md`，全部完成）**：对照 PDFMathTranslate / BabelDOC（沉浸式翻译）/ 有道等主流方案实施。摘要：
> - **T1 持久化翻译缓存**（`src/transcache.py`，cache/translations.json，键=模型|领域|上下文|原文哈希；每批落盘；失败段不缓存）——实测重跑整篇仅请求 2 段、89 段命中、费用≈0。
> - **T2 失败降级**：单批异常回退原文继续跑完并统计上报，重跑自动补缺。
> - **T3 试译模式**（`--pages N` / GUI"试译前 N 页"）+ **T5 成本预估**（请求前上报"预计 N 段/M tokens"，config `price_in/out` 可换算金额）。
> - **T4 双语·左右对照**（output_mode=sidebyside，2W×H 宽页左原右译+中缝分隔线，两后端均支持；GUI 三模式单选）。
> - **T6 全文上下文注入**（标题+摘要入 system prompt，参与缓存键）；**T10 领域可配置**（config `domain`，GUI 可编辑下拉）。
> - **T7 OCR 行→段合并**（gap×1.35、字号突变阈 0.45，扫描版 p1 块数 22→15）；**T8 两端对齐**（`_flow` 非段末行均摊行尾余量，上限 0.30×字号/处）。
> - **T9 服务预设**（GUI：DeepSeek/OpenAI/Ollama 本地/自定义，切换预填接口与模型；base_url 本就兼容一切 OpenAI 接口）。
> - **T11 版面分析插件**（`src/layout_model.py`，DocLayout-YOLO onnx 已下载至 models/ 并启用，CPU 0.4s/页；table/isolate_formula 区块保留原样、figure/table 并入障碍物；任何推理失败自动整体停用；删模型文件即关闭）。实测本论文图区检测准确、零误报。
> - **T12 跨栏段落缝合**（pipeline 配对"左栏尾+右栏首"/"页尾+页首"未终句块，合并送译后按比例在句读处拆回；页脚豁免防误配）——实测 14 组配对全部为真实腰斩句（含 p14 "software and | methodology"）。
> 新增顶层文件：`TASKS.md`（任务卡）、`models/`（版面模型）、`cache/`（翻译缓存，.gitignore 已排除）。外层新增《…_对照版.pdf》左右对照成品。
>
> **2026-07-18 网页前端上线（第八轮，用户要求"皮肤化前端，未来商用"）**：新增本地网页界面，与 tkinter 版并存（外层 `启动网页版.bat`）。架构详见 `docs/前端设计.md`：
> - **零新依赖**：`webui.py` 用标准库 ThreadingHTTPServer，只绑 127.0.0.1；API 10 个端点（config/themes/pick/test/jobs/cancel/open + 静态）；任务后台线程执行、轮询进度；文件选择用服务端 tkinter 原生对话框（浏览器拿不到本地路径）。
> - **前端分层**（`web/`，原生 ES Modules 零构建）：api.js（唯一 fetch 出口，云端化只改 BASE）→ store.js（发布订阅，组件间唯一通信）→ components.js（文件篮/设置/运行/进度/结果/Toast）→ 皮肤层。
> - **皮肤契约**：`web/themes/<id>/theme.css` 只写 17 个 CSS 令牌 + 装饰挂点（吉祥物/背景/动画），`themes.json` 注册即用，localStorage 记忆。已含 `sponge`（海绵泡泡：原创黄海绵吉祥物 SVG、海底气泡动画背景、卡通厚边控件；**未使用任何受版权保护形象**，商用安全）与 `clean`（极简样板=契约最小实现）。
> - 端到端已测：静态资源/主题注册/配置读写/mock 任务创建→完成→产物落盘全通过。商业化预留（付费皮肤闸口、云端化路径、i18n）见前端设计文档第 5 节。
> - **tkinter 版退役、单一入口（2026-07-19）**：双端并行的维护成本已实际造成缺陷（v2 修订那批不一致），且皮肤/商业化只有网页端能承载，故**网页版成为唯一正式界面**。顶层 `启动PDF翻译.bat` 改为启动 `webui.py`（删除 `启动网页版.bat`，恢复"只有一个启动脚本"）；tkinter 版**冻结**为应急备胎（`程序/备用-经典界面.bat`，删除原调试 bat），`src/gui.py` 顶部已加冻结声明——**新功能一律只加网页版**。备胎冻结时功能完整（T1–T12 齐备），故随时可用。注意 webui.py 的文件对话框也依赖 tkinter，两者共享该依赖，无取舍差异。
> - **国产模型预设（2026-07-19）**：双端 SERVICES 同步新增 Kimi（api.moonshot.cn/v1，kimi-k2.6/k3）、智谱 GLM（open.bigmodel.cn/api/paas/v4，glm-5.2）、豆包（ark.cn-beijing.volces.com/api/v3，doubao-seed-2.0-pro/lite，需在方舟控制台先开通模型）。`_chat` 增加兼容性保障：HTTP 400 时自动去掉 DeepSeek 风格的 `thinking` 字段重试一次，保证严格校验的第三方服务可用。双端一致性已用自动对照脚本校验（js/py 的 SERVICES 逐项相等）。模型 ID 会随厂商迭代过期——预设只是预填，界面均可手改。
> - **v2 修订（用户复审后）**：与桌面版功能对齐并修 7 处缺陷——① 模型/领域改"真下拉+自定义…输入"（datalist 会按已填文字过滤选项，导致"只显示计算机科学"的假象，弃用）；② 补术语库选择（/api/pick 支持 type=csv）；③ 补"输出到"同目录/指定文件夹（type=dir + 任务 outdir 参数）；④ 补"记住 Key"（不勾则 Key 不落盘、仅本次任务用）；⑤ 轮询失联提示（服务窗口被关时不再静默卡死）；⑥ 主题注册表异常兜底；⑦ .gitignore 排除 models/。回归：新控件齐全 + 自定义输出目录/sidebyside 命名 + config 往返，全通过。

---

## 1. 技术栈与运行环境

- 语言：Python 3.9+（用户机是 Windows + Python 3.11）
- 解析 PDF：`pdfplumber`（提取文字、坐标、字号、图片位置）
- 生成/回填 PDF：`pymupdf`（首选，精确抹除+嵌字）/ `reportlab`+`pypdf`（兜底覆盖）
- 扫描版 OCR：`rapidocr-onnxruntime`（本地识别，模型内置、无需联网/显卡；可选，缺失时扫描页提示安装）
- 翻译：`requests` 调 DeepSeek 的 OpenAI 兼容接口（未用官方 SDK）
- 界面：Python 自带 `tkinter`（Windows 无需额外安装）
- 沙箱限制：开发环境无法联网到 DeepSeek，故用 `MockTranslator` 离线跑通全流程；渲染校验用 `pdfplumber` 的 `page.to_image()`

## 2. 目录结构

2026-07-17 重组：**外层只暴露一个启动脚本**（用户要求），程序本体整体下沉到 `程序/`。`src/config.py` 的 `ROOT` 取 `src/` 的上一级，故 config.json / glossary / fonts 随迁后路径自洽，无需改代码。2026-07-18 二次整理：开发文档收入 `docs/`，外层新增《使用说明.html》用户手册；新增 `models/`（版面模型）与 `cache/`（翻译缓存）。

```
Translation/                     # 外层 = 用户桌面工作区
├─ 启动PDF翻译.bat               # 【唯一入口】校验 程序\run_gui.py 存在 → cd 程序 → 装依赖 → pyw 启动
├─ 使用说明.html                 # 用户手册（双击浏览器打开；面向非程序员）
├─ （用户的 PDF 及译文，如 Observing….pdf / …_translation.pdf / …_对照版.pdf）
└─ 程序/                         # 程序本体（原项目根整体迁入）
   ├─ 启动PDF翻译-调试.bat       # 保留控制台的调试启动
   ├─ 自测-PyMuPDF.bat           # 回填后端离线自测
   ├─ run_gui.py                 # GUI 入口（os.chdir 到自身目录；error.log 也写在此）
   ├─ translate_cli.py           # 命令行入口（--mock/--pages/--mode/--domain/--no-cache）
   ├─ selftest_backend.py        # 自测脚本（真实论文优先在 ROOT.parent 即外层查找）
   ├─ requirements.txt           # 依赖（纯 ASCII，避免 pip 用 GBK 读取报错）
   ├─ config.example.json        # 配置模板（含全部 21 个字段）
   ├─ config.json                # 运行时配置（含 API Key，勾选“记住”时生成；.gitignore 已排除）
   ├─ glossary/cs_terms.csv      # 计算机领域术语库（en,zh,note，184 条）
   ├─ fonts/                     # （可选）思源宋体/黑体 ttf/otf，fitz 后端自动嵌入
   ├─ models/…onnx               # 【T11】DocLayout-YOLO 版面模型（删除即关闭该增强）
   ├─ cache/translations.json    # 【T1】持久化翻译缓存（删除即重新计费）
   ├─ samples/make_sample.py     # 生成测试用双栏 PDF
   ├─ samples/make_scanned.py    # 生成"扫描版"样张（渲成无文字层 PDF）测 OCR
   ├─ selftest_out/              # 自测输出（PNG 对比图 + 自测 PDF）
   ├─ docs/交接说明_HANDOFF.md   # 本文件
   ├─ docs/TASKS.md              # 12 项工程任务卡（2026-07-18 全部完成）
   └─ src/
      ├─ config.py               # 配置加载/保存（ROOT = src/ 上一级 = 程序/）
      ├─ glossary.py             # 术语库加载 + 按整词/长短语优先匹配 + 注入提示词
      ├─ pdf_parser.py           # 【核心难点】PDF → 文本块 + 行内公式检测（⟦Fn⟧）+ 颜色 + 加粗 + 障碍物 + OCR 回灌
      ├─ ocr.py                  # 【新】扫描页 OCR（RapidOCR）：渲染位图 → 识别行 → 伪装成"词"
      ├─ textfix.py              # 【新】译文后处理：中西文加空格（盘古之白）
      ├─ translator.py           # DeepSeek 客户端 + 占位符保护校验/强化重试 + Mock
      ├─ layout.py               # 共享重排引擎：断行/禁则/逐行避障/向下扩展/缩号 + OCR 流形状避让
      ├─ pdf_writer_fitz.py      # 【首选】PyMuPDF 精确抹除 + CJK 嵌入 + 公式矢量回贴（OCR 块白底覆盖）
      ├─ pdf_writer.py           # 【兜底】reportlab 行矩形覆盖 + 公式位图回贴
      ├─ pipeline.py             # 编排 + 后端选择（auto/pymupdf/reportlab）与自动回退
      └─ gui.py                  # tkinter 图形界面（含回填后端下拉）
```

## 3. 工作原理（数据流）

`解析(pdf_parser) → 翻译(translator) → 回填(pdf_writer)`，由 `pipeline.translate_pdf()` 串起来：

1. 解析：每页提取单词（坐标/字号/字体/颜色）→ 检测分栏（含首页混排二次检测）→ 分行（含字号断差切行）→ 行内公式检测（⟦Fn⟧ 占位 + FormulaSpan 矩形）→ 合并段落块 `Block`（含 line_rects/formulas/color）→ 收集页面障碍物（图片/大型矢量）。
2. 翻译：收集所有可译块文本 → 去重 → 分批 → 并发请求 DeepSeek（术语对照 + 占位符保护指令）→ 译后逐段校验占位符（丢失→强化重试→回退原文）→ 回填到各 `Block.translation`。
3. 回填（`pipeline.pick_backend` 选择后端，二者共用 layout.py 重排）：
   - **pymupdf**：redaction 按行矩形精确删除原文 → 嵌入 CJK 写译文 → 公式 `show_pdf_page` 矢量回贴 → `subset_fonts` 瘦身。
   - **reportlab**：行矩形白底遮盖 → 写译文 → 公式高清位图回贴 → pypdf 合并。
   - `translated` 模式：译文直接落在原页；`bilingual` 模式：原文页 + 译文页交替。
   - 图片、矢量图、未翻译的独立公式不被改动，**天然保留**。

## 4. 模块详解（关键算法）

### pdf_parser.py（最关键、最易出问题）
- 用 `page.extract_words(extra_attrs=["size","fontname"])` 取词。
- **分栏检测 `_detect_split_x`（数据驱动，不靠魔法阈值）**：在页面中部 `[0.34W, 0.66W]` 扫描竖线 x，`cov(x)=` 跨过该 x 的单词数；两栏中心 `cov` 高、中缝 `cov` 低。若中缝最小覆盖 `<= 0.30 * min(左峰,右峰)` 判为双栏，返回中缝 x。对整幅标题/摘要横幅、页边旋转文字稳健。
- **分行 `_group_lines`**：先按垂直重叠（>0.4）聚成「水平带」，再在**正好跨过中缝的空隙**处或**超大间隙**（`>max(36, 0.06W)`）处切开。这样即使左右栏间距很窄（真实论文实测仅 12pt）也能准确分栏，而整幅标题（词连续跨中缝、无空隙）保持整行。
  - 【历史坑】早期用固定 `x_gap≈16pt` 判断换栏，但该论文栏间距只有 12pt < 阈值，导致左右栏被并成整幅行、译文错乱。改为“中缝处切行”后解决。
- **段落合并 `_group_blocks`**：同栏内相邻行，若垂直间距 `>0.6×行高中位数` 或字号变化 `>25%` 则断开成新段。阅读顺序：整幅 → 左栏 → 右栏。
- **是否翻译 `_is_translatable`**：跳过公式/纯数字/页码——规则：拉丁字母占比 <50%、含 ≥3 个数学符号且密度高、词数 <2、块宽 <14pt（滤掉页边竖排碎片）等。
- 输出 `PageLayout(width,height,blocks[Block(text,x0,top,x1,bottom,size,translatable,translation)])`，坐标为 pdfplumber 左上角原点。
- **OCR 回灌（扫描页）**：`_parse_page(page, page_index, ocr)` 在 pdfplumber 抽不到词时调用 `ocr.words_for_page()` 拿到识别行（伪装成 word 字典）继续走同一套分栏/分行/分段。为 OCR 场景加了几个开关（文本 PDF 行为不变）：
  - `_make_line(..., detect_formulas=False)`：OCR 行**关闭公式检测**（识别框的字号/偏移无排版语义，误报会把原图英文像素回贴到白底之上）。
  - `_group_lines(..., gutter_tol=4.0)` + **渗缝裁剪**：OCR 框边缘常渗进中缝，小幅渗越（≤3×tol）的框裁回主体侧，使"中缝处切行"能触发；真正跨栏的整幅行不受影响。
  - `_is_translatable(..., from_ocr=True)`：OCR 常整行丢空格（`knowledgeacquisition…`），词数<2 判据放宽为"长度≥12 也算正文"。
  - `Block.from_ocr` / `PageLayout.needs_ocr` 两个新标记：前者驱动写回端白底覆盖，后者在"扫描页但没装 OCR"时供 pipeline 提示安装命令。

### ocr.py（新增，扫描页 OCR）
- `OcrEngine(pdf_path)`：懒加载 fitz 文档 + RapidOCR 实例（模型只初始化一次）。`words_for_page(i)`：用 fitz 按 200dpi 渲染该页位图 → RapidOCR 识别 → 每行返回 `{text,x0,x1,top,bottom,size,upright,fontname:"OCR",...}`，坐标 ÷ 缩放还原为 pt。
- **踩过的三个坑（都已解决，接手勿回退）**：
  1. **检测分辨率**：RapidOCR 默认把长边缩到 ~960px，200dpi 页图（>2000px）被压后小字行高仅 ~5px 成片漏检 → `det_limit_side_len=2400, det_limit_type="max"`。
  2. **方向分类器误翻**：`use_cls=True`（默认）会把部分正立行误判 180° 转成乱码被丢 → 页图由我们渲染必正立，直接 `use_cls=False`（代价：整页倒扫文档不自动纠正，罕见）。
  3. **竖排页边字**："Downloaded from…"等窄高框会与整栏每行相交、链式粘成乱序巨行 → 按 `h>2w 且 h>25pt` 丢弃（本就不翻译，原像素不动）。
- 低于 `text_score=0.5` 的识别行丢弃。engine 在 `parse_pdf` 里 try/finally 关闭。

### translator.py
- `DeepSeekTranslator._chat()`：`POST {base_url}/chat/completions`，`Authorization: Bearer <key>`，body 含 `model/messages/temperature/stream=false/thinking`。3 次指数退避重试；对 401/402/403 直接报错；对 SSLError 给出“关代理/直连/关杀软 HTTPS 扫描”的中文提示。
- **批量**：一次把 N 段用 `[[1]] ... [[2]] ...` 编号发出去，让模型按编号返回，正则解析对齐；编号数量不符则退化为逐段翻译保证对齐。
- **并发**：`ThreadPoolExecutor(max_workers)`；连接池随并发扩大。**注意：这是网络 I/O 并发，不吃 CPU**。
- **缓存**：按源文本去重，相同段落只翻一次。
- **术语注入**：`glossary.prompt_block(batch)` 汇总本批出现的术语，作为“必须遵守的对照表”加进提示词。
- **思考开关**：`thinking=True`（默认，质量优先）→ `{"type":"enabled"}`；`False`（提速）→ `disabled`。DeepSeek 思考模式默认开启，会先生成思维链，很慢。
- `MockTranslator`：离线占位翻译，套用术语库并生成长度大致相当的中文，用于测试排版。

### pdf_writer.py
- 每页用 `reportlab` 画一个与原页同尺寸的覆盖层：对每个可译块，先画白色矩形盖住英文，再用内置中文字体 `STSong-Light`（无需外部字体文件）写译文。
- **中文换行**：ASCII 连续串（英文词/数字/URL）不拆断，中文按字符换行；`_fit()` 自动缩小字号以塞进原块高度（最小到 4pt）。
- 用 `pypdf` 的 `page.merge_page(overlay)` 合并；`bilingual` 模式则「原页 + 译文页」都加入。

### pipeline.py
- `translate_pdf(input, output, cfg, mock, progress, should_cancel)`：进度回调 `(msg, 0~1)`；每批之间检查取消（抛 `CancelledError`）。
- `check_connection(cfg)`：用一句“把 hello 翻成中文”快速验证 Key/模型/网络。

### gui.py（tkinter）
- 三区：① 选择 PDF（可多文件批量）② 翻译设置 ③ 开始+进度。
- 设置项：API Key（显示/记住/测试连接）、模型下拉（v4-pro / v4-flash）、**并发数 Spinbox**、输出模式（纯中文/双语）、术语库、输出位置（同目录/指定）、离线测试、直连模式、思考模式。
- 后台线程翻译 + 队列回传进度，界面不卡；可取消；完成后“打开 PDF / 打开文件夹”。设置存 `config.json`。
- 输出命名：`原名_translation.pdf`（双语为 `_translation_bilingual.pdf`）。

### 启动器
- `启动PDF翻译.bat`：`chcp 65001` + `PYTHONUTF8=1`；找 `py`/`python`；缺依赖则 `pip install pdfplumber reportlab pypdf requests`（**按名安装，不读 requirements.txt，避开 GBK 编码坑**）；用 `pyw/pythonw` 无窗口启动。UTF-8 + CRLF。

## 5. DeepSeek API 关键事实（2026-07 核对官方文档）

- base_url：`https://api.deepseek.com`；端点 `POST /chat/completions`；OpenAI 兼容。
- 当前模型：`deepseek-v4-pro`（质量高）、`deepseek-v4-flash`（快）。旧名 `deepseek-chat`/`deepseek-reasoner` 于 2026/07/24 停用。
- **思考模式默认 `enabled`**；用 body 里 `{"thinking":{"type":"disabled"}}` 关闭。思考模式下 `temperature` 等参数被忽略。
- 申请 Key：https://platform.deepseek.com/api_keys

## 6. 配置项（config.json / Config）

`api_key, base_url, model(=deepseek-v4-pro), temperature(=1.0), source_lang(英文), target_lang(中文), output_mode(translated|bilingual), glossary_path, batch_size(=8), max_workers(=8), thinking(=true), use_system_proxy(=true), proxy(""), verify_ssl(=true)`

## 7. 已实现且可用

- 英文 PDF → 中文 PDF，图片/矢量图/独立公式保留；**双栏**正确分栏、译文回填对应位置。
- 术语库（184 条，可编辑 CSV）、纯中文/双语两种输出、批量多文件。
- 图形界面 + 双击启动器；连接测试、取消、进度日志、打开结果；回填后端可选（自动/PyMuPDF/reportlab）。
- 网络健壮性：代理/直连/SSL 校验开关、重试、SSL 错误中文指引。
- 速度：网络并发（默认 8，界面可调）；思考模式可开关（质量↔速度）。
- 已在真实 15 页论文离线回归：双栏/首页混排分栏干净、图片/图注/页码/页边竖排在原位。

### 7.1 第二轮迭代（2026-07）新增 —— 回填内核升级

**新架构**：`解析(pdf_parser) → 翻译(translator) → 重排(layout) → 双后端写回（pdf_writer_fitz 首选 / pdf_writer 兜底）`

- **行内公式保护（全链路）**：
  - 检测（pdf_parser）：双层策略。强信号=私用区/cid 乱码字符、上下标（字号缩小且垂直偏移）、非字母数字的高型 glyph（积分号等）→ 替换为 `⟦Fn⟧` 占位符并记录 FormulaSpan 矩形；弱信号（STIX/CMMI 等数学字体、单字母斜体变量）只在与强信号相邻时并入，`n = 53` 这类线性式作为文本交给提示词保留——避免满页图像补丁。
  - 保护（translator）：系统提示词第 6 条 + 批量/单段附注；译后逐段校验占位符集合，丢失→强化重试一次→仍失败回退原文（公式位置绝不丢），`ph_failures` 计数上报进度日志。
  - 回贴（写回端）：fitz 后端用 `show_pdf_page` 从原始副本**矢量回贴**（无限清晰）；reportlab 后端用 220dpi 位图回贴。
- **共享重排引擎 `src/layout.py`**：中文逐字断行 + ASCII 串/占位符不拆 + 行首行尾禁则；**逐行避障**（每行动态计算可用区间，支持“照片嵌栏、文字绕图”）；块框可**向下扩展**到下一障碍物（其他块/图片/图表），从根上缓解中文变长溢出；仍不足则 0.5pt 步进缩号（最小 5pt），最后宁溢出不丢字。后端只负责“画”，测宽函数由各后端注入。
- **PyMuPDF 后端 `src/pdf_writer_fitz.py`**：redaction 精确抹除（`fill=False` 不填充 → 深色背景零露白；`images=NONE/graphics=NONE` 保图片矢量）；内置 CJK（china-ss）或 `fonts/` 目录思源字体自动嵌入 + `subset_fonts()` 减小体积；双语交替页；版本兼容逐级 try 降级；旋转页抛 `BackendUnsupported` → pipeline 自动整体回退 reportlab。
- **reportlab 兜底升级**：整块大白框 → 只盖**实际行矩形**；同样走重排引擎与公式回贴。
- **解析层修复**（回归中发现并修复的真实 bug）：
  - 首页“整幅标题+下半页双栏”混排时中缝检测失败 → 左右栏文字交错污染翻译输入（下半页二次检测修复）；
  - 大标题多行被拆成多块（行距阈值按行高缩放修复）；
  - 整幅段落的短末行被误分到左/右栏（“行提升”修复，标题第二行/摘要末行归位）；
  - 作者行吞并右侧 5.5pt 版权小字（字号断差切行修复）；
  - 文字颜色捕获（红色栏目标题等按原色回填；兜底后端近白文字自动转深灰）。

## 8. 已知问题与局限

1. ~~行内公式被白底覆盖丢失~~ → **已解决**（占位符保护 + 回贴）。作者上标、引用上标同样按公式保护（视觉一致）。
2. ~~深色背景露白块~~ → **fitz 后端已解决**（精确抹除不填充）；reportlab 兜底后端仍会露白（行矩形范围）。
3. ~~译文溢出/留白~~ → **大幅缓解**（向下扩展 + 避障 + 缩号）；极端长译文仍可能缩号到 5pt 或轻微溢出。
4. ~~PyMuPDF 后端未经真实运行验证~~ → **已于 2026-07-17 真机验证通过**（pymupdf 1.27.2.3：自测、公式 38/38 回贴、旋转页回退、整篇真实翻译均正常，见文首补记）。
5. ~~扫描版 PDF 不支持~~ → **已于 2026-07-18 接入 OCR**（RapidOCR，见文首第四轮补记与第 4 节 ocr.py）。遗留局限：个别行丢词间空格（模型多能理解）；整页 180° 倒扫不自动纠正；OCR 块白底覆盖回填，深色纸背会露白；照片内文字也会被识别翻译。
6. 旋转页（page.rotation≠0）整文件回退 reportlab 兜底。
7. 段落跨栏续行（双栏摘要等）按栏分块翻译，断句处上下文有损；真正的跨栏阅读顺序重排未做。
8. 段落切分仍是启发式；小型大写页眉提取成混乱串（“SCienCe RobotiCS”）未处理。
9. 术语库与提示词偏计算机领域。
10. reportlab 兜底后端不删除原文字层（白框遮盖），文字层残留英文词条（fitz 后端无此问题）。
11. **Mock 回归的盲区（本轮教训）**：MockTranslator 产出几乎纯中文、长度贴近原文的占位译文，测不出「ASCII 宽度不一致」「译文变长导致的溢出叠印」这类真实翻译才触发的缺陷。改内核后务必跑一次 **ASCII 密集合成译文压力测试**（保留原文引文编号/统计量/缩写，替换单词为中文），或用真实 Key 抽译几页目检。
12. **数字两侧空格不统一**：`textfix.pangu` 有意只在汉字↔字母间加空格，数字紧贴汉字（`16名`/`研究1`）交给翻译模型；但模型自身时加时不加（有的"135 名"、有的"10名"），故成品数字空格略不一致。属可接受的模型级差异；若要强制统一，在 pangu 增加"数字↔汉字"规则（但会得到"研究 1""第 2 天"，未必更好）。
13. **个别术语未译全**：如"partial η²"的"partial"偶尔留英文（η² 是公式占位符，模型只看到"partial ⟦F⟧"）。属翻译内容层，可加术语库条目缓解，非排版问题。

## 9. 建议的下一步

1. ~~（最优先）Windows 验证 PyMuPDF 后端~~ → **已完成（2026-07-17，见文首补记）**。
2. ~~OCR 支持~~ → **已完成（2026-07-18，RapidOCR，见文首第四轮补记）**。可选增强：接双语（中英）识别模型、把 OCR 结果做行合并成段（现按识别行成段，跨行断句上下文略损）、扫描页译文改半透明底或"另存译文页"避免露白。
3. **版面分析模型**（可选增强）：doclayout-yolo / PP-DocLayout 替代启发式分栏分段；依赖重，建议做成可选插件路径。对扫描页尤其有用（可替代当前的 OCR 行启发式分栏）。
4. **跨栏段落重排**：把“左栏尾行 + 右栏首行”连成同一翻译单元（阅读顺序重建），解决第 8.7 条。
5. **两端对齐（justify）**：重排引擎目前左对齐，可在 `_flow` 里按行剩余宽度均摊字间距。
6. 术语库自动扩充、领域可配置化。
7. **标题层级再细分**：现"粗体"统一合成加粗；原文 Bold 与 BoldItalic 两级子标题（如"研究2分析"与"适应阶段…"）因同为粗体黑色被并成一块。若要再分级，可在 `_group_blocks` 追加 italic 变化断块，并对次级标题用略小字号/缩进区分。
8. **数字空格策略**：见第 8.12 条，如需统一数字两侧空格在 `textfix.py` 调整。

## 10. 如何测试

- **Windows 一键自测**：双击 `程序\自测-PyMuPDF.bat`（缺 pymupdf 会先自动安装）→ 离线 Mock 跑 15 页论文 → 校验页数/文字层 → 导出 `程序\selftest_out\pageN_原文.png / _译文.png` 目检。（2026-07-17 已通过。）
- 离线跑通版式（不花 token）：`python translate_cli.py "输入.pdf" --mock`；可加 `--backend pymupdf|reportlab` 强制后端、`--font 路径.otf` 指定字体。
- **扫描版 OCR 回归**：`python samples/make_scanned.py` 生成 `samples/sample_scanned.pdf`（真实论文前 3 页渲成无文字层），再 `python translate_cli.py samples/sample_scanned.pdf --mock` → 进度会显示"第 N 页为扫描版，已 OCR 识别 X 块"，渲染 p1/p3 目检分栏与绕图。首次运行 RapidOCR 会初始化模型（约 20s）。
- **排版优化回归**：`python -c "import sys;sys.path.insert(0,'.');from src.textfix import pangu;print(pangu('对RF条件下16名学生'))"` 应得 `对 RF 条件下16名学生`；解析后 `sum(b.bold for L in parse_pdf(paper) for b in L.blocks)` 应约 39（15 页），且可译块**零重叠**。标题渲染目检：p1 红色"人机交互"/"引言"、p7 红色"讨论"+粗体子标题。
- 渲染检查：`pdfplumber` 的 `page.to_image(resolution=110).save(...)` 导 PNG 对比。
- 真实文件在**外层目录**（`程序/` 的上一级）：`Observing a robot peer's failures facilitates students' classroom learning.pdf`（15 页双栏；重点看 p1 首页混排、p3 绕图页、p6 公式密集页）。
- 联网真翻：GUI 填 Key 或 `python translate_cli.py "输入.pdf" --api-key sk-xxx`。
- 沙箱注意：本轮开发沙箱 PyPI/apt 均被代理 403，pymupdf 装不上——fitz 代码是按 stub 干跑验证的；若你的环境能联网，直接 `pip install pymupdf fonttools` 后一切以真实运行为准。

## 11. 一句话任务（交给你）

至 2026-07-18，五轮迭代均已完成并验证：回填内核升级（精确抹除/公式保护/避障重排/双后端）、PyMuPDF 真机验证、真实翻译质量三大缺陷修复、**扫描版 OCR 接入**、**成品排版优化（标题层级/粗体/颜色 + 中西文加空格）**。目录为「外层唯一启动脚本 `启动PDF翻译.bat` + 程序/ 收纳一切」。核心功能与成品质量均已闭环，外层译文 PDF 为最新优化版。后续为可选增强：跨栏段落阅读顺序重排、版面分析模型、两端对齐、标题层级再细分、扫描页译文露白优化（见第 9 节）。翻译侧与界面可继续直接复用。
