"""本机选择框必须在主线程弹出（Issue #2/#3：Mac 上点"选择文件"即闪退）。

HTTP 请求在工作线程里处理，旧代码直接在那里 tk.Tk()——macOS 只许主线程
创建窗口，一碰输入法系统就触发线程检查、整个程序闪退。这里在 Windows/CI 上
也能跑：假装是 macOS，断言绝不会在工作线程里碰 tkinter。
"""
import enum
import queue
import sys
import threading
import types

import pytest

import webui


class _FakeWindow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, dialog_type, allow_multiple=False, file_types=()):
        self.calls.append((dialog_type, allow_multiple, tuple(file_types)))
        return self.result


@pytest.fixture
def fake_webview(monkeypatch):
    """CI 不装 pywebview，用最小替身提供对话框类型常量。"""
    mod = types.ModuleType("webview")

    class FileDialog(enum.IntEnum):
        OPEN = 10
        FOLDER = 20
        SAVE = 30

    mod.FileDialog = FileDialog
    monkeypatch.setitem(sys.modules, "webview", mod)
    return mod


@pytest.fixture
def no_tk(monkeypatch):
    def boom(kind):
        raise AssertionError("macOS 应用窗口模式下不得在工作线程里碰 tkinter")
    monkeypatch.setattr(webui, "_pick_tk", boom)


def _in_worker(fn):
    """像 HTTP 处理线程那样，在非主线程里调用。"""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert not t.is_alive(), "调用卡死"
    if "error" in box:
        raise box["error"]
    return box["result"]


def test_mac_app_window_uses_native_dialog(monkeypatch, fake_webview, no_tk):
    monkeypatch.setattr(sys, "platform", "darwin")
    win = _FakeWindow(("/Users/a/paper.pdf", "/Users/a/b.docx"))
    monkeypatch.setattr(webui, "_WINDOW", win)
    assert _in_worker(lambda: webui._pick("pdf")) == [
        "/Users/a/paper.pdf", "/Users/a/b.docx"]
    dialog_type, multiple, types_ = win.calls[0]
    assert dialog_type == fake_webview.FileDialog.OPEN and multiple
    assert any("*.pdf" in t and "*.pptx" in t for t in types_)


@pytest.mark.parametrize("kind,result,expected", [
    ("dir", "/Users/a/out", ["/Users/a/out"]),           # 文件夹可能返回单个字符串
    ("dir", ("/Users/a/out",), ["/Users/a/out"]),
    ("csv", None, []),                                   # 用户取消
    ("pdf", (), []),
])
def test_mac_dialog_result_shapes(monkeypatch, fake_webview, no_tk,
                                  kind, result, expected):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(webui, "_WINDOW", _FakeWindow(result))
    assert _in_worker(lambda: webui._pick(kind)) == expected


def test_file_types_accepted_by_real_pywebview(monkeypatch, fake_webview, no_tk):
    """file_types 写错格式，pywebview 会直接抛 ValueError——用真解析器校验。"""
    monkeypatch.delitem(sys.modules, "webview")
    try:
        from webview.util import parse_file_type
    except Exception:  # noqa: BLE001
        pytest.skip("未安装 pywebview")
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(sys, "platform", "darwin")
    for kind in ("pdf", "csv"):
        win = _FakeWindow(None)
        monkeypatch.setattr(webui, "_WINDOW", win)
        webui._pick(kind)
        for ft in win.calls[0][2]:
            parse_file_type(ft)


def test_browser_mode_dialog_runs_on_main_loop_thread(monkeypatch):
    """浏览器模式：选择框交给"主线程"（_serve_main_calls 所在线程）执行。"""
    calls = queue.Queue()
    stop = threading.Event()
    seen = []

    def fake_tk(kind):
        seen.append(threading.current_thread().name)
        return [f"picked-{kind}"]

    monkeypatch.setattr(webui, "_pick_tk", fake_tk)
    monkeypatch.setattr(webui, "_MAIN_CALLS", calls)
    monkeypatch.setattr(webui, "_WINDOW", None)
    loop = threading.Thread(target=webui._serve_main_calls, args=(calls, stop),
                            name="主线程替身")
    loop.start()
    try:
        assert _in_worker(lambda: webui._pick("csv")) == ["picked-csv"]
        assert seen == ["主线程替身"], "对话框必须在主线程循环里弹出"
    finally:
        stop.set()
        loop.join(5)


def test_browser_mode_error_reaches_caller(monkeypatch):
    calls = queue.Queue()
    stop = threading.Event()

    def broken(kind):
        raise OSError("no display")

    monkeypatch.setattr(webui, "_pick_tk", broken)
    monkeypatch.setattr(webui, "_MAIN_CALLS", calls)
    monkeypatch.setattr(webui, "_WINDOW", None)
    loop = threading.Thread(target=webui._serve_main_calls, args=(calls, stop))
    loop.start()
    try:
        with pytest.raises(OSError, match="no display"):
            _in_worker(lambda: webui._pick("pdf"))
    finally:
        stop.set()
        loop.join(5)


def test_tk_refuses_worker_thread_on_mac(monkeypatch):
    """兜底：万一走到了工作线程里的 tkinter，宁可报错也不能闪退。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="主线程"):
        _in_worker(lambda: webui._pick_tk("pdf"))
