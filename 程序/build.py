"""一键打包脚本（C1）。

用法（在 程序/ 目录下）：
    py build.py                     # 完整版（含 OCR），输出到 release/v1.0.0-full/
    py build.py --profile lite      # 精简版（无 OCR，体积小很多）
    py build.py --set-version 1.1.0 # 改版本号后再构建
    py build.py --zip               # 额外打包成 zip 便于分发
    py build.py --overwrite         # 重建同一版本（旧产物在构建成功后删除）
    py build.py --keep-old          # 保留同 profile 的更早构建，不自动清理
    py build.py --clean             # 只清理构建中间产物
    py build.py --list              # 查看已构建的历史版本

产物布局（多版本互不干扰，不会把文件夹搞乱）：
    release/
      RELEASES.md              ← 台账（人读），由 releases.json 渲染
      releases.json            ← 台账数据源，只增不减，纳入 git
      v1.0.0-full/
        论文翻译工具/          ← 给用户的整个文件夹（内含 exe）
        paper-translator-v1.0.0-full.zip
        SHA256SUMS.txt         ← 完整性校验
        build_info.json        ← 版本/提交/时间/profile
      v1.0.0-lite/…
    build/                     ← 中间产物，可随时删

**产物保留原则**：留记录，不留字节。release/ 下**只保留每个 profile 的最新
一次构建**，更早的目录在本次构建自检通过后自动删除（--keep-old 可关闭）——
它们体积以 GB 计，且都能从台账记的来源提交重新构建，正式对外发布的版本另有
GitHub Releases 永久托管，本地留着只是占地方。

但**台账只增不减**：每一次构建的版本/时间/来源提交/体积都写进 releases.json
并永久保留，产物删了记录仍在、标为「已清理」。这条是上一条的前提——没有台账
就谈不上"能重建"，所以删产物可以，删记录不行。

安全网：目标目录已存在时默认中止（提示改版本号），要重建须显式 --overwrite；
且旧产物是先改名暂存、等新产物自检通过才真删，构建失败则原样留在
release/.replaced-<名字>-<时间戳>/ 可手工还原。清理只发生在 build/ 与旧产物。

关于"防逆向"：本项目采用 AGPL-3.0，**分发时必须提供完整源代码**，因此对
二进制做混淆在法律与实际上都无意义（源码本来就公开），还可能妨碍 AGPL
赋予用户的权利。故默认只做正当加固：
  · 不把任何密钥/配置打进包（config.json 运行时才在用户目录生成）
  · 入包的是字节码而非 .py 源文件（PyInstaller 默认行为）
  · 产出 SHA256 校验清单，便于用户验证未被篡改
  · 预留代码签名钩子（--sign），签名才是对抗"被人二次打包投毒"的正解
若将来改走闭源路线，再启用 --obfuscate（需自行安装 PyArmor 等商业工具）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.version import (APP_NAME, APP_NAME_EN, COPYRIGHT,  # noqa: E402
                         HOMEPAGE, PUBLISHER, __version__)

# zip 文件名必须纯 ASCII：GitHub Releases 上传时会把非 ASCII 字符剥掉，
# "论文翻译工具-v1.2.0-full.zip" 到了发布页会剩下 "-v1.2.0-full.zip"，
# 看着像坏文件（v1.0.1 真踩过这个坑）。所以走英文名派生的 ASCII slug；
# 包内的文件夹仍叫中文名（zip 条目是 UTF-8，不受影响）。
ZIP_SLUG = APP_NAME_EN.lower().replace(" ", "-")     # → paper-translator

BUILD_DIR = ROOT / "build"
RELEASE_DIR = ROOT / "release"
INDEX_FILE = RELEASE_DIR / "RELEASES.md"   # 台账（人读），由 releases.json 渲染
LEDGER_FILE = RELEASE_DIR / "releases.json"  # 台账数据源，只增不减（纳入 git）
ENTRY = ROOT / "webui.py"

# 构建平台（PyInstaller 不能交叉编译：产物平台 = 运行 build.py 的平台）
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
PLATFORM_TAG = "mac" if IS_MAC else ("win" if IS_WIN else "linux")

# 随程序分发的只读资源（源 → 包内相对路径）
DATA_ITEMS = [
    ("web", "web"),
    ("glossary", "glossary"),
    ("config.example.json", "."),
    # 中文字体（思源宋体正文 + 思源黑体粗标题）。**必须进包**：缺了它就退回
    # MuPDF 内置字体——单一字重、无粗体，标题只能描边合成，层级当场消失。
    # 由 fonts/生成字体.py 从 Noto CJK 可变字体烘焙，SIL OFL，可随 AGPL 分发。
    ("fonts", "fonts"),
]
# 放在用户可见目录（不进 exe，便于查看/编辑）
DOC_ITEMS = [
    (ROOT.parent / "LICENSE", "LICENSE.txt"),
    (ROOT.parent / "NOTICE.md", "NOTICE.md"),
    (ROOT.parent / "使用说明.html", "使用说明.html"),
]

# OCR 与版面模型体积极大，精简版不含
OCR_MODULES = ["rapidocr_onnxruntime", "onnxruntime", "cv2"]
# full 版必须整包收进 rapidocr：它的三个 .onnx 模型与 config.yaml 是**包数据**，
# 且 src/ocr.py 里是函数内延迟 import，PyInstaller 静态分析扫不到。
# 只写 --hidden-import 不够（模型不会被带上），必须 --collect-all。
# 教训：v1.0.0 的 full 版就因为漏了这行，白白比 lite 大 150MB（onnxruntime+cv2
# 是被版面模型那条路径捎带进去的），而招牌功能"扫描版 OCR"其实用不了。
OCR_COLLECT = ["rapidocr_onnxruntime"]
HIDDEN_IMPORTS = [
    "pdfplumber", "reportlab", "pypdf", "requests", "fitz", "docx",
    "webview", "tkinter", "tkinter.filedialog",
]


def log(msg: str):
    print(f"  {msg}", flush=True)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(ROOT), capture_output=True, text=True)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                             capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 版本管理
# ---------------------------------------------------------------------------

def set_version(new: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        sys.exit(f"版本号格式应为 主.次.修订，例如 1.2.0（收到 {new!r}）")
    f = ROOT / "src" / "version.py"
    text = f.read_text(encoding="utf-8")
    text = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new}"', text)
    f.write_text(text, encoding="utf-8", newline="\n")
    log(f"版本号已更新为 {new}（记得提交 src/version.py）")


def list_releases() -> None:
    """列出台账里的每一次构建，并标明本地产物还在不在。"""
    rows = _merge_ledger()
    if not rows:
        print("尚无任何已构建版本。")
        return
    print(f"\n{'版本':<9}{'profile':<9}{'构建时间':<21}{'提交':<10}"
          f"{'大小':>9}  本地产物")
    print("-" * 70)
    for r in rows:
        print(f"{r.get('version', '?'):<9}{r.get('profile', '?'):<9}"
              f"{r.get('built_at', '?')[:19].replace('T', ' '):<21}"
              f"{r.get('commit', '?'):<10}{r.get('size_mb', 0):>8.1f}M"
              f"  {'在' if r.get('kept') else '已清理'}")
    kept = sum(1 for r in rows if r.get("kept"))
    print(f"\n共 {len(rows)} 次构建，本地留存 {kept} 个。"
          f"「已清理」的产物已删、记录留档，凭「提交」列可重新构建。")


# ---------------------------------------------------------------------------
# 图标：从皮肤里的原创吉祥物 SVG 生成 .ico（全自动，无需美术资源）
# ---------------------------------------------------------------------------

def make_icon(out: Path) -> Path | None:
    try:
        import urllib.parse

        import fitz
        from PIL import Image
        css = (ROOT / "web" / "themes" / "sponge" / "theme.css").read_text(
            encoding="utf-8")
        m = re.search(r'url\("data:image/svg\+xml,([^"]+)"\)', css)
        if not m:
            return None
        svg = urllib.parse.unquote(m.group(1))
        tmp_svg = BUILD_DIR / "icon.svg"
        tmp_svg.parent.mkdir(parents=True, exist_ok=True)
        tmp_svg.write_text(svg, encoding="utf-8")
        doc = fitz.open(str(tmp_svg))
        png = BUILD_DIR / "icon.png"
        doc[0].get_pixmap(matrix=fitz.Matrix(16, 16), alpha=True).save(str(png))
        doc.close()
        img = Image.open(png).convert("RGBA")
        img.save(out, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
        log(f"图标已生成：{out.name}")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"[i] 图标生成跳过（{e}）")
        return None


def make_icns(out: Path) -> "Path | None":
    """从皮肤吉祥物 SVG 生成 macOS .icns（用 iconutil，mac 系统必带）。"""
    try:
        import urllib.parse

        import fitz
        from PIL import Image
        css = (ROOT / "web" / "themes" / "sponge" / "theme.css").read_text(
            encoding="utf-8")
        m = re.search(r'url\("data:image/svg\+xml,([^"]+)"\)', css)
        if not m:
            return None
        svg = urllib.parse.unquote(m.group(1))
        tmp_svg = BUILD_DIR / "icon.svg"
        tmp_svg.parent.mkdir(parents=True, exist_ok=True)
        tmp_svg.write_text(svg, encoding="utf-8")
        doc = fitz.open(str(tmp_svg))
        png = BUILD_DIR / "icon_1024.png"
        doc[0].get_pixmap(matrix=fitz.Matrix(16, 16), alpha=True).save(str(png))
        doc.close()
        base = Image.open(png).convert("RGBA")
        iconset = BUILD_DIR / "icon.iconset"
        if iconset.exists():
            shutil.rmtree(iconset)
        iconset.mkdir(parents=True)
        # macOS 要求的标准尺寸集（含 @2x 视网膜）
        specs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
                 (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
                 (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
        for px, name in specs:
            base.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{name}.png")
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)])
        log(f"图标已生成：{out.name}")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"[i] 图标生成跳过（{e}）")
        return None


def make_version_file(path: Path, version: str) -> Path:
    """Windows 文件属性（右键→属性→详细信息 里显示的信息）。"""
    v = version.split(".") + ["0"]
    quad = ", ".join(v[:4])
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({quad}), prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', '{PUBLISHER}'),
      StringStruct('FileDescription', '{APP_NAME} - 保留排版的文档翻译工具'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{APP_NAME_EN}'),
      StringStruct('LegalCopyright', '{COPYRIGHT}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('Comments', '{HOMEPAGE}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def build(profile: str, do_zip: bool, sign_cmd: str | None,
          obfuscate: bool, overwrite: bool = False,
          keep_old: bool = False) -> Path:
    version = __version__
    # full 与 lite 是同一版本的两个产物，各自独立目录，互不覆盖
    out_root = RELEASE_DIR / f"v{version}-{profile}"
    app_dir = out_root / APP_NAME

    # 旧产物先改名暂存、等新产物自检通过才真删：中途失败不至于人财两空
    replaced: Path | None = None
    if out_root.exists():
        rel = out_root.relative_to(ROOT)
        if not overwrite:
            sys.exit(
                f"\n[×] {rel} 已存在，构建中止（不覆盖已有产物）。\n\n"
                f"    发布新版（推荐）：py build.py --set-version X.Y.Z "
                f"--profile {profile}\n"
                f"    重建这一版：      py build.py --profile {profile} "
                f"--overwrite\n"
                f"                      （旧产物自检通过后删除，记录留在台账）\n")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        replaced = RELEASE_DIR / f".replaced-{out_root.name}-{stamp}"
        shutil.move(str(out_root), str(replaced))
        log(f"重建 {rel}：旧产物暂存于 release/{replaced.name}，自检通过后删除；"
            f"构建失败则留在原地，可手工改回原名还原")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    meta = {
        "version": version, "profile": profile, "commit": git_commit(),
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "python": sys.version.split()[0],
        "dirty_worktree": git_dirty(),
    }
    (BUILD_DIR / "build_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if meta["dirty_worktree"]:
        log("[!] 工作区有未提交改动——发布版建议先提交，便于追溯来源")

    if obfuscate:
        log("[!] --obfuscate 已请求，但当前许可证为 AGPL-3.0：分发必须附源码，"
            "混淆无实际意义且可能违反许可。已跳过。")

    # 图标与版本信息按平台生成：Windows 用 .ico + VERSIONINFO；mac 用 .icns + bundle id
    if IS_MAC:
        icon = make_icns(BUILD_DIR / "app.icns")
        verfile = None
    else:
        icon = make_icon(BUILD_DIR / "app.ico")
        verfile = make_version_file(BUILD_DIR / "version_info.txt", version)

    # mac 的 .app 先落在中间目录，稍后连同文档一起归置到 out_root/APP_NAME/
    distpath = str(BUILD_DIR / "dist") if IS_MAC else str(out_root)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", APP_NAME, "--windowed",
           "--distpath", distpath, "--workpath", str(BUILD_DIR),
           "--specpath", str(BUILD_DIR)]
    if verfile:
        cmd += ["--version-file", str(verfile)]
    if IS_MAC:
        cmd += ["--osx-bundle-identifier", "com.morning-cml.paper-translator"]
    if icon:
        cmd += ["--icon", str(icon)]
    for src, dst in DATA_ITEMS:
        p = ROOT / src
        if p.exists():
            cmd += ["--add-data", f"{p}{os.pathsep}{dst}"]
    cmd += ["--add-data",
            f"{BUILD_DIR / 'build_info.json'}{os.pathsep}."]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    if profile == "lite":
        for mod in OCR_MODULES:
            cmd += ["--exclude-module", mod]
    else:
        for mod in OCR_COLLECT:
            cmd += ["--collect-all", mod]
    cmd += ["--exclude-module", "pytest", "--exclude-module", "matplotlib"]
    cmd.append(str(ENTRY))

    log(f"开始打包（profile={profile}，平台={PLATFORM_TAG}，PyInstaller）……")
    run(cmd)

    # mac：把 PyInstaller 产出的 .app 归置到 out_root/APP_NAME/（与文档同放，便于分发）
    if IS_MAC:
        app_dir.mkdir(parents=True, exist_ok=True)
        produced = Path(distpath) / f"{APP_NAME}.app"
        if not produced.is_dir():
            sys.exit(f"[×] 未找到 PyInstaller 产出的 {produced.name}")
        shutil.move(str(produced), str(app_dir / f"{APP_NAME}.app"))

    # 用户可见的文档与启动说明
    for src, name in DOC_ITEMS:
        if Path(src).exists():
            shutil.copy2(src, app_dir / name)
    if IS_MAC:
        mac_guide = ROOT / "docs" / "Mac使用说明.md"
        if mac_guide.exists():
            shutil.copy2(mac_guide, app_dir / "Mac使用说明.md")

    if IS_MAC:
        launch = (f"双击 {APP_NAME}.app 即可启动，无需安装 Python。\n"
                  "（第一次打开若被 macOS 拦下，请看《Mac使用说明.md》放行一次）")
        data_note = ("· 首次运行会把你的设置与翻译缓存保存到 .app 同级 data/；\n"
                     f"  该处不可写时改存到 ~/Library/Application Support/{APP_NAME}。")
    else:
        launch = f"双击 {APP_NAME}.exe 即可启动，无需安装 Python。"
        data_note = ("· 首次运行会在本文件夹下创建 data\\ 目录，保存你的设置与翻译缓存。\n"
                     f"  （若本程序放在 Program Files 等受保护位置，则改存到 %APPDATA%\\{APP_NAME}）")
    (app_dir / "首次使用请看我.txt").write_text(
        f"""{APP_NAME} v{version}

{launch}

{data_note}
· 详细使用方法见 使用说明.html（双击用浏览器打开）。
· 本软件为自由软件，依据 AGPL-3.0 发布，源代码：
  {HOMEPAGE}
  你有权获取、修改并再分发本程序的完整源代码，详见 LICENSE.txt。

{COPYRIGHT}
""", encoding="utf-8")

    if sign_cmd and IS_WIN:
        exe = app_dir / f"{APP_NAME}.exe"
        log(f"代码签名：{sign_cmd} {exe.name}")
        run(sign_cmd.split() + [str(exe)])
    elif sign_cmd and IS_MAC:
        log("[i] mac 代码签名/公证（codesign + notarytool）属层三，本脚本暂未内置，已跳过 --sign。")

    shutil.copy2(BUILD_DIR / "build_info.json", out_root / "build_info.json")

    if do_zip:
        if IS_MAC:
            import platform as _platform
            m = _platform.machine()
            arch = "arm64" if m == "arm64" else ("x64" if m in ("x86_64", "AMD64") else m)
            # 必须用 ditto：普通 zip 会破坏 .app 的可执行位与 ad-hoc 签名，用户端
            # 会「已损坏，无法打开」。ditto 完整保留 bundle 结构、权限与签名。
            # 架构进文件名：arm64=Apple 芯片、x64=Intel，两版并存不撞名。
            zip_path = out_root / f"{ZIP_SLUG}-v{version}-{profile}-mac-{arch}.zip"
            log(f"正在压缩（ditto，保留 .app 权限与签名；架构 {arch}）……")
            run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                 str(app_dir), str(zip_path)])
        else:
            zip_path = out_root / f"{ZIP_SLUG}-v{version}-{profile}.zip"
            log("正在压缩……")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                for f in app_dir.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(app_dir.parent))
        log(f"已生成 {zip_path.name}（{zip_path.stat().st_size / 1048576:.1f} MB）")

    verify_build(app_dir, profile)
    write_checksums(out_root)
    # 顺序要紧：先把磁盘上现存的（含即将被清理的）全部入账，再删，再复算一次
    # 把删掉的标为「已清理」。反过来做会让从未入账的旧产物连记录一起消失。
    update_index()
    _drop_replaced(replaced)
    if not keep_old:
        prune_old(profile, out_root)
    update_index()
    return out_root


def _verify_build_macos(app_dir: Path, profile: str) -> None:
    """mac 版产物自检（.app 内部布局随 PyInstaller 版本而异，用递归查找更稳）。"""
    bundle = app_dir / f"{APP_NAME}.app"
    if not bundle.is_dir():
        sys.exit(f"[×] 自检失败：缺少 {APP_NAME}.app 应用包")
    problems: list[str] = []
    if not (bundle / "Contents" / "MacOS" / APP_NAME).is_file():
        problems.append("缺少 .app 内可执行文件（Contents/MacOS）")
    if not any(p.parent.name == "web" for p in bundle.rglob("index.html")):
        problems.append("缺少前端资源 web/index.html（界面会打不开）")
    ocr_pkgs = [d for d in bundle.rglob("rapidocr_onnxruntime") if d.is_dir()]
    models = [p for p in bundle.rglob("*.onnx") if "rapidocr" in str(p).lower()]
    if profile == "full":
        if not ocr_pkgs:
            problems.append("full 版缺少 rapidocr_onnxruntime 包——扫描版 OCR 用不了")
        elif len(models) < 2:
            problems.append(f"full 版 rapidocr 模型不全（只找到 {len(models)} 个 .onnx）")
    else:
        if ocr_pkgs:
            problems.append("lite 版不该包含 rapidocr_onnxruntime")
    if problems:
        for p in problems:
            log(f"[×] 自检失败：{p}")
        sys.exit("\n产物自检未通过，构建视为失败（产物保留在原地供排查）。\n")
    detail = f"，OCR 模型 {len(models)} 个" if profile == "full" else "，无 OCR（符合 lite）"
    log(f"产物自检通过：.app + 前端资源齐全{detail}")


def verify_build(app_dir: Path, profile: str) -> None:
    """产物自检：确认这一档该有的东西真的进包了。

    存在的意义：v1.0.0 的 full 版曾静默漏掉 rapidocr——exe 能跑、界面正常、
    体积也比 lite 大（onnxruntime/cv2 被别的路径捎带进去了），唯独扫描版 PDF
    永远识别不出来。这种"看着对、实则少了招牌功能"的缺陷只能靠打包后检查内容
    发现，光看构建成功和体积都会漏。
    """
    if IS_MAC:
        _verify_build_macos(app_dir, profile)
        return
    internal = app_dir / "_internal"
    problems: list[str] = []

    exe = app_dir / f"{APP_NAME}.exe"
    if not exe.is_file():
        problems.append(f"缺少可执行文件 {exe.name}")
    if not (internal / "web" / "index.html").is_file():
        problems.append("缺少前端资源 web/index.html（界面会打不开）")

    ocr_pkg = internal / "rapidocr_onnxruntime"
    models = list(ocr_pkg.rglob("*.onnx")) if ocr_pkg.is_dir() else []
    if profile == "full":
        if not ocr_pkg.is_dir():
            problems.append("full 版缺少 rapidocr_onnxruntime 包——扫描版 OCR 用不了")
        elif len(models) < 2:
            problems.append(f"full 版 rapidocr 模型不全（只找到 {len(models)} 个 .onnx）")
    else:
        if ocr_pkg.exists():
            problems.append("lite 版不该包含 rapidocr_onnxruntime")

    if problems:
        for p in problems:
            log(f"[×] 自检失败：{p}")
        sys.exit("\n产物自检未通过，构建视为失败（产物保留在原地供排查）。\n")

    detail = f"，OCR 模型 {len(models)} 个" if profile == "full" else "，无 OCR（符合 lite）"
    log(f"产物自检通过：exe + 前端资源齐全{detail}")


def _scan(d: Path) -> dict | None:
    info = d / "build_info.json"
    if not d.is_dir() or not info.is_file():
        return None
    try:
        meta = json.loads(info.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    meta["_dir"] = d.name
    meta["_size"] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return meta


def _dir_size_mb(d: Path) -> float:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1048576


def _drop_replaced(replaced: Path | None) -> None:
    """删掉 --overwrite 暂存的旧产物。只在新产物自检通过后调用。"""
    if replaced and replaced.is_dir():
        log(f"旧产物已删除：release/{replaced.name}"
            f"（{_dir_size_mb(replaced):.1f} MB，记录留在台账）")
        shutil.rmtree(replaced)


def prune_old(profile: str, keep: Path) -> None:
    """删掉同 profile 更早版本的构建目录，本地只留最新一次。

    这些目录动辄几百 MB，且都能凭台账记的来源提交重新构建；对外发布的版本
    另在 GitHub Releases 永久托管。删前必须已调用过 update_index()——记录
    进了台账，删的才只是字节、不是历史。
    """
    for d in sorted(RELEASE_DIR.iterdir()):
        if not d.is_dir() or d == keep or d.name.startswith("."):
            continue
        meta = _scan(d)
        if not meta or meta.get("profile") != profile:
            continue
        log(f"清理旧构建：release/{d.name}"
            f"（{_dir_size_mb(d):.1f} MB，记录留在台账）")
        shutil.rmtree(d)


def _ledger_key(dir_name: str, built_at: str) -> str:
    """一次构建的唯一标识。同一目录名重建多次，各次都要各自留一条记录。"""
    return f"{dir_name}@{built_at}"


def _load_ledger() -> dict:
    """读回已有台账。读不出来就当空的——宁可少记，不能把文件写坏。"""
    if not LEDGER_FILE.is_file():
        return {}
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {_ledger_key(r["dir"], r.get("built_at", "")): r
            for r in data.get("builds", []) if r.get("dir")}


def _merge_ledger() -> list[dict]:
    """把磁盘现状并入已有台账，返回按构建时间倒序的记录（**不写盘**）。

    产物本体可以删（体积以 GB 计、能从来源提交重建、正式版另有 GitHub
    Releases 托管），但**记录只增不减**：台账是"还能不能重建"的唯一依据，
    必须活得比产物久。所以这里是把磁盘扫描结果**并入**已有台账，而不是
    照磁盘重新生成——目录没了的记录标为「已清理」留着，绝不因为产物被删
    就连记录一起抹掉。
    """
    ledger = _load_ledger()
    on_disk: dict[str, dict] = {}
    if RELEASE_DIR.is_dir():
        for d in sorted(RELEASE_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):  # 跳过 .replaced-* 暂存
                continue
            meta = _scan(d)
            if meta:
                on_disk[meta["_dir"]] = meta

    for name, m in on_disk.items():                    # 现存产物：登记/刷新
        built_at = m.get("built_at", "")
        ledger[_ledger_key(name, built_at)] = {
            "dir": name,
            "version": m.get("version", "?"),
            "profile": m.get("profile", "?"),
            "built_at": built_at,
            "commit": m.get("commit", "?"),
            "dirty_worktree": bool(m.get("dirty_worktree")),
            "size_mb": round(m["_size"] / 1048576, 1),
            "kept": True,
        }
    for key, r in ledger.items():                      # 字节还在不在
        name, _, built_at = key.partition("@")
        r["kept"] = (name in on_disk
                     and on_disk[name].get("built_at", "") == built_at)

    return sorted(ledger.values(), key=lambda x: x.get("built_at", ""),
                  reverse=True)


def update_index() -> None:
    """把台账落盘：release/releases.json（数据）+ RELEASES.md（人读）。"""
    if not RELEASE_DIR.is_dir():
        return
    rows = _merge_ledger()
    LEDGER_FILE.write_text(
        json.dumps({"builds": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    def table(items: list) -> list:
        out = ["| 版本 | profile | 构建时间 | 提交 | 大小 | 目录 | 本地产物 |",
               "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in items:
            dirty = " ⚠️脏工作区" if r.get("dirty_worktree") else ""
            out.append(
                f"| {r.get('version', '?')} | {r.get('profile', '?')} "
                f"| {r.get('built_at', '')[:19].replace('T', ' ')} "
                f"| `{r.get('commit', '?')}`{dirty} "
                f"| {r.get('size_mb', 0):.1f} MB | `{r['dir']}` "
                f"| {'在' if r.get('kept') else '已清理'} |")
        return out

    lines = [
        "# 版本台账（Releases）", "",
        "本文件由 `build.py` 从 `releases.json` 渲染，两者**都纳入 git**。",
        "二进制产物体积大不入库，但每一次构建的版本号/时间/来源提交/体积在此",
        "永久留档。对应的功能差异见 [`../../CHANGELOG.md`](../../CHANGELOG.md)。", "",
        "> **注意本文件只记录本机构建**。正式对外发布的版本由 GitHub Actions 在",
        "> 干净环境构建并挂在",
        f"> [Releases]({HOMEPAGE}/releases) —— 那里才是分发给用户的权威列表，",
        "> 发布流程是推送 `v*.*.*` tag 自动触发。本地构建多为开发验证用。", "",
        "> **留记录，不留字节**：本地只保留每个 profile 的最新一次构建，更早的",
        "> 目录在新构建自检通过后自动删除（`--keep-old` 可关闭）。「本地产物」列",
        "> 标「已清理」的表示目录已删、记录仍在——凭「提交」列可重新构建。标了",
        "> ⚠️脏工作区 的当时有未提交改动，只能等价重建、不保证与原产物字节一致。", "",
        "## 本机构建", "",
    ]
    lines += table(rows) if rows else ["_（暂无）_"]
    lines += ["", f"_最后更新：{datetime.now().astimezone().isoformat()[:19]}_",
              ""]
    INDEX_FILE.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    kept = sum(1 for r in rows if r.get("kept"))
    log(f"版本台账已更新：release/{INDEX_FILE.name}"
        f"（{len(rows)} 条记录，本地留存 {kept} 个）")


def write_checksums(out_root: Path) -> None:
    lines = []
    for f in sorted(out_root.rglob("*")):
        if f.is_file() and f.name != "SHA256SUMS.txt":
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{h}  {f.relative_to(out_root).as_posix()}")
    (out_root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")
    log(f"完整性清单：SHA256SUMS.txt（{len(lines)} 个文件）")


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} 打包工具")
    ap.add_argument("--profile", choices=["full", "lite"], default="full",
                    help="full=含扫描版 OCR；lite=不含（体积小很多）")
    ap.add_argument("--zip", action="store_true", help="额外打包 zip")
    ap.add_argument("--set-version", metavar="X.Y.Z", help="更新版本号后再构建")
    ap.add_argument("--sign", metavar="CMD",
                    help='代码签名命令，如 "signtool sign /fd SHA256 /a"')
    ap.add_argument("--obfuscate", action="store_true",
                    help="代码混淆（AGPL 下无意义，当前会被忽略）")
    ap.add_argument("--overwrite", action="store_true",
                    help="目标目录已存在时重建（旧产物自检通过后删除，记录留台账）")
    ap.add_argument("--keep-old", action="store_true",
                    help="保留同 profile 的更早构建，不自动清理（默认只留最新一次）")
    ap.add_argument("--clean", action="store_true", help="仅清理中间产物")
    ap.add_argument("--list", action="store_true", help="列出已构建版本")
    args = ap.parse_args()

    if args.list:
        list_releases()
        return 0
    if args.clean:
        for d in (BUILD_DIR,):
            if d.exists():
                shutil.rmtree(d)
                log(f"已删除 {d.name}/")
        return 0
    if args.set_version:
        set_version(args.set_version)
        import importlib

        import src.version as v
        importlib.reload(v)
        globals()["__version__"] = v.__version__

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("缺少打包组件，请先运行：py -m pip install pyinstaller")

    out = build(args.profile, args.zip, args.sign, args.obfuscate,
                args.overwrite, args.keep_old)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    # 这里不要用 ✔ 之类的非 ASCII 符号：Windows 控制台默认 GBK(cp936) 没有对应
    # 码位，会在**构建已经成功之后**抛 UnicodeEncodeError，脚本以 1 退出，看起来
    # 像构建失败（CI 设了 PYTHONUTF8=1 才碰不到，本地必踩）。
    print(f"\n构建完成 [OK]  {out.relative_to(ROOT)}  共 {size / 1048576:.1f} MB")
    print(f"给用户的文件夹：{(out / APP_NAME).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
