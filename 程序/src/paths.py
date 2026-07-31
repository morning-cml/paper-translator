"""运行路径解析：源码运行与打包运行（PyInstaller）共用一套规则。

打包后目录结构变了，必须区分两类路径，否则程序一冻结就找不到文件：

* **资源目录**（只读，随程序分发）：web 前端、默认术语库、皮肤
  - 源码运行 → `程序/`
  - 打包运行 → PyInstaller 的临时解包目录 `sys._MEIPASS`（每次启动重建）

* **用户数据目录**（可写，跨版本保留）：config.json（含 API Key）、翻译缓存、
  版面模型、用户字体
  - 源码运行 → `程序/`（保持现状，开发时一切在眼前）
  - 打包运行 → exe 同级的 `data/`（便携优先）；若该处不可写（如装在
    Program Files、或 macOS 的 .app 包内），退回各平台用户目录下的
    `论文翻译工具/`（Win=%APPDATA%、mac=~/Library/Application Support、
    Linux=~/.local/share）

**关键**：用户数据绝不能放进 `_MEIPASS`——那是临时目录，程序一退出就被删，
API Key 和翻译缓存会凭空消失。
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

# 数据目录名。**故意不从 version.py 导入**：那个是随时可改的「产品名」，
# 这个是老用户硬盘上已经存在的目录名——两者绑死的话，将来再改一次名就会
# 又一次悄悄丢掉所有人的 Key 和缓存。改这里必须同时加一条迁移（见下）。
APP_NAME = "论文翻译工具"
_LEGACY_APP_NAMES = ("PDF翻译工具",)   # v1.1.0 及更早


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        return Path(sys._MEIPASS)          # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _adopt_legacy_dir(base: Path) -> Path:
    """程序改名后的一次性目录迁移（PDF翻译工具 → 论文翻译工具，2026-07-30）。

    老用户的 `config.json`（含 API Key）和 `cache/translations.json` 都躺在旧名
    目录里。不搬 = Key 要重填、**翻译缓存全部作废**——而缓存是真花过钱的，
    README 承诺的"同文档重跑几乎零成本"就是靠它。

    同盘 rename 是原子操作、不复制字节，几百 MB 的缓存也是瞬间完成。
    失败（无权限 / 目录被占用 / 跨卷）就**继续用旧目录**：名字不一致只是难看，
    丢数据是事故。
    """
    new = base / APP_NAME
    if new.exists():
        return new
    for legacy in _LEGACY_APP_NAMES:
        old = base / legacy
        if not old.is_dir():
            continue
        try:
            old.rename(new)
        except OSError:
            return old          # 搬不动就地用，绝不让用户数据失联
        return new
    return new


def _app_data_dir() -> Path:
    """便携目录不可写时的回退：各平台的用户级应用数据目录。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA")
                    or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:  # Linux / 其他
        base = Path(os.environ.get("XDG_DATA_HOME")
                    or Path.home() / ".local" / "share")
    return _adopt_legacy_dir(base)


_user_dir_cache: Path | None = None


def user_dir() -> Path:
    """可写的用户数据根目录（结果缓存，避免重复探测写权限）。"""
    global _user_dir_cache
    if _user_dir_cache is not None:
        return _user_dir_cache

    if not is_frozen():
        _user_dir_cache = Path(__file__).resolve().parent.parent
        return _user_dir_cache

    portable = Path(sys.executable).resolve().parent / "data"
    if _writable(portable):
        _user_dir_cache = portable
    else:
        fallback = _app_data_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        _user_dir_cache = fallback
    return _user_dir_cache


def resource(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)


def user_path(*parts: str) -> Path:
    return user_dir().joinpath(*parts)


def data_file(*parts: str) -> Path:
    """优先用用户目录下的副本（用户可自行编辑，如术语库），
    没有则回退到随程序分发的只读资源。"""
    p = user_path(*parts)
    if p.exists():
        return p
    return resource(*parts)


def build_info() -> dict:
    """读取打包时写入的版本信息；源码运行时返回开发态标记。"""
    import json
    f = resource("build_info.json")
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"version": "dev", "commit": "", "built_at": "", "profile": "source"}


# ---------------------------------------------------------------------------
# 原子写出 + 临时残留清理（避免中途失败留下半截损坏产物 / 垃圾文件）
# ---------------------------------------------------------------------------

def _silent_unlink(p) -> None:
    with contextlib.suppress(OSError):
        Path(p).unlink()


def _free_sibling(path: Path) -> Path:
    """在 path 同目录找一个不冲突的名字：X (1).pdf、X (2).pdf …"""
    for i in range(1, 100):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} ({os.getpid()}){path.suffix}")


class OutputError(RuntimeError):
    """最终产物无法写出（通常是目标文件被占用）。"""


class AtomicOutput:
    """atomic_output() 交给调用方的句柄：写 .tmp，退出时才原子落位。"""

    __slots__ = ("final", "tmp", "path")

    def __init__(self, final: Path):
        self.final = final
        self.tmp = str(final.with_name(final.name + ".part"))
        self.path = str(final)   # 实际落位路径（目标被占用时可能自动改名）


@contextlib.contextmanager
def atomic_output(final_path: str):
    """产物原子写出：先写同目录 .part，成功退出后再 os.replace 到最终名。

    - 翻译/生成中途抛异常或被取消 → 删掉 .part，**绝不在最终路径留下损坏文件**
      （否则用户看到的是一个打不开的半截 PDF，还以为"生成失败/被缓存搞坏了"）。
    - 最终文件被占用（正被 PDF 阅读器打开、Windows 拒绝覆盖）→ 自动换一个
      "X (1).pdf"的名字保住成果，并通过 .path 告知实际路径；实在写不出才报错。

    用法：
        with atomic_output(out) as h:
            backend.build_output(..., h.tmp, ...)
        actual_path = h.path
    """
    h = AtomicOutput(Path(final_path))
    h.final.parent.mkdir(parents=True, exist_ok=True)
    _silent_unlink(h.tmp)
    try:
        yield h
    except BaseException:
        _silent_unlink(h.tmp)
        raise
    try:
        os.replace(h.tmp, h.final)
    except OSError:
        alt = _free_sibling(h.final)
        try:
            os.replace(h.tmp, alt)
            h.path = str(alt)
        except OSError as e:
            _silent_unlink(h.tmp)
            raise OutputError(
                f"无法写出结果文件：“{h.final.name}”可能正被其他程序"
                "（如 PDF 阅读器）打开。请关闭后重试。") from e


def sweep_temp(*paths: str) -> None:
    """精准清理**本程序自己**产生的临时残留：给定文件的 .tmp / .part 兄弟。

    只删我们约定命名（.tmp 来自原子存配置/缓存，.part 来自 atomic_output）的
    文件，绝不递归、绝不按后缀通配，避免误伤用户文件。"""
    for base in paths:
        for suffix in (".tmp", ".part"):
            _silent_unlink(str(base) + suffix)
