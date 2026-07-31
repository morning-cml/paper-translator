"""网页版前端 · 本地服务（零新依赖，标准库实现）。

用法：双击外层 启动论文翻译.bat，或 `py webui.py`。
只绑定 127.0.0.1（仅本机可访问）；翻译内核 100% 复用 src/pipeline。
API 约定见 docs/前端设计.md 第 4 节。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if not getattr(sys, "frozen", False):
    os.chdir(ROOT)

from src.paths import resource  # noqa: E402

WEB = resource("web")           # 打包后指向解包目录内的前端资源

from src.config import load_config, save_config  # noqa: E402
from src.pipeline import (SUPPORTED_EXTS, CancelledError,  # noqa: E402
                          check_connection, output_suffix, translate_document)

JOBS: dict = {}
JOBS_LOCK = threading.Lock()
PICK_LOCK = threading.Lock()

_MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".json": "application/json",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".woff2": "font/woff2"}


# ---------------------------------------------------------------------------
# 任务执行（后台线程；一次一个活动任务，与 tkinter 版一致）
# ---------------------------------------------------------------------------

def _out_path(src: str, mode: str, outdir: str = "") -> str:
    p = Path(src)
    ext = p.suffix.lower()
    base = Path(outdir) if outdir else p.parent
    return str(base / (p.stem + output_suffix(mode, ext) + ext))


def _run_job(job_id: str, files: list, overrides: dict, mock: bool,
             outdir: str = ""):
    job = JOBS[job_id]

    def log(msg: str, frac: float, base: float, span: float):
        job["percent"] = round((base + span * frac) * 100)
        job["message"] = msg
        job["log"].append(msg)

    try:
        cfg = load_config(**{k: v for k, v in overrides.items() if v is not None})
        # 翻译前**必做**：先检测翻译服务是否连得上、Key/模型是否有效。
        # 不做的话，Key 错/连不上会让每一批都失败、白跑一整篇还产出未翻译的文档。
        # 离线测试（mock）无需联网，跳过。
        if not mock and not job["cancel"].is_set():
            job["message"] = "正在检测翻译服务连接…"
            ok, msg = check_connection(cfg)
            job["log"].append(msg)
            if not ok:
                job["status"] = "error"
                job["message"] = msg
                return
        n = max(len(files), 1)
        for i, f in enumerate(files):
            if job["cancel"].is_set():
                raise CancelledError("已取消。")
            base, span = i / n, 1 / n
            job["log"].append(f"—— [{i + 1}/{n}] {Path(f).name} ——")
            out = _out_path(f, cfg.output_mode, outdir)
            res = translate_document(
                f, out, cfg, mock=mock,
                progress=lambda m, fr: log(m, fr, base, span),
                should_cancel=job["cancel"].is_set)
            job["outputs"].append({"file": res["output"],
                                   "pages": res["pages"],
                                   "backend": res["backend"]})
        job["status"] = "done"
        job["message"] = "全部完成 ✔"
        job["percent"] = 100
    except CancelledError:
        job["status"] = "cancelled"
        job["message"] = "已取消。"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["message"] = f"出错：{e}"
        job["log"].append(traceback.format_exc(limit=3))


def _pick(kind: str = "pdf") -> list:
    """本机选择框（串行化调用；浏览器无法提供本地路径，故由服务端弹窗）。
    kind: pdf=多选 PDF；csv=单选术语库；dir=选文件夹。"""
    with PICK_LOCK:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "csv":
                p = filedialog.askopenfilename(
                    title="选择术语库 CSV", filetypes=[("CSV", "*.csv")])
                return [p] if p else []
            if kind == "dir":
                d = filedialog.askdirectory(title="选择输出文件夹")
                return [d] if d else []
            paths = filedialog.askopenfilenames(
                title="选择要翻译的文档",
                filetypes=[("支持的文档",
                            "*.pdf;*.docx;*.pptx;*.md;*.markdown;*.txt;*.srt"),
                           ("PDF", "*.pdf"), ("Word 文档", "*.docx"),
                           ("PowerPoint", "*.pptx"),
                           ("Markdown", "*.md;*.markdown"),
                           ("纯文本", "*.txt"), ("字幕", "*.srt")])
            return [str(p) for p in paths]
        finally:
            root.destroy()


def _os_open(target: str) -> None:
    """用系统默认程序打开本机文件/文件夹（跨平台：Win / mac / Linux）。"""
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 — 本机打开产物
        return
    import subprocess
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静
        pass

    # ---- 基础 ----
    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _static(self, rel: str):
        p = (WEB / rel).resolve()
        if not str(p).startswith(str(WEB.resolve())) or not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(p.suffix.lower(),
                                                   "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/web/"):
            return self._static(unquote(path[5:]))
        if path == "/api/config":
            cfg = load_config()
            return self._json(cfg.to_dict())
        if path == "/api/themes":
            try:
                reg = json.loads((WEB / "themes" / "themes.json")
                                 .read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                reg = []
            return self._json(reg)
        if path == "/api/languages":
            from src import languages as L
            return self._json({"sources": L.sources(), "targets": L.targets(),
                               "recommend": L.all_recommend(), "note": L.RECO_NOTE})
        if path.startswith("/api/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            job = JOBS.get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json({k: job[k] for k in
                               ("status", "percent", "message", "log", "outputs")})
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/config":
            cfg = load_config(**{k: v for k, v in body.items() if v is not None})
            try:
                save_config(cfg)
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/pick":
            try:
                return self._json({"files": _pick(body.get("type", "pdf"))})
            except Exception as e:  # noqa: BLE001
                return self._json({"files": [], "error": str(e)})

        if path == "/api/test":
            cfg = load_config(**{k: v for k, v in body.items() if v is not None})
            ok, msg = check_connection(cfg)
            return self._json({"ok": ok, "message": msg})

        if path == "/api/jobs":
            files = [f for f in body.get("files", []) if Path(f).is_file()
                     and Path(f).suffix.lower() in SUPPORTED_EXTS]
            if not files:
                return self._json(
                    {"error": "没有有效的文档（支持 "
                              + "、".join(SUPPORTED_EXTS) + "）"}, 400)
            with JOBS_LOCK:
                if any(j["status"] == "running" for j in JOBS.values()):
                    return self._json({"error": "已有任务在进行中"}, 409)
                jid = uuid.uuid4().hex[:12]
                JOBS[jid] = {"status": "running", "percent": 0,
                             "message": "准备中…", "log": [], "outputs": [],
                             "cancel": threading.Event()}
            t = threading.Thread(
                target=_run_job,
                args=(jid, files, body.get("overrides", {}),
                      bool(body.get("mock")), body.get("outdir") or ""),
                daemon=True)
            t.start()
            return self._json({"job_id": jid})

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            jid = path.split("/")[3]
            job = JOBS.get(jid)
            if job:
                job["cancel"].set()
                return self._json({"ok": True})
            return self._json({"error": "no such job"}, 404)

        if path == "/api/open":
            target = body.get("path", "")
            if body.get("folder"):
                target = str(Path(target).parent)
            if target and Path(target).exists():
                _os_open(target)
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "路径不存在"}, 404)

        self.send_error(404)


def _free_port(start: int = 8763, end: int = 8793) -> int:
    for cand in range(start, end):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", cand)) != 0:
                return cand
    return start


def main(app_window: bool = True):
    """启动本地服务。

    app_window=True 时优先用原生应用窗口（pywebview，无地址栏），
    装不上或启动失败则自动回退到系统浏览器——两条路走的是同一套界面。
    """
    # 启动时精准清理上次可能被强杀残留的临时文件（只删本程序自己的 .tmp/.part）
    from src.paths import sweep_temp, user_path
    sweep_temp(str(user_path("config.json")),
               str(user_path("cache", "translations.json")))

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if app_window:
        try:
            import webview
            print("  正在打开应用窗口……（关闭窗口即退出）")
            webview.create_window("论文翻译工具", url, width=1080, height=860,
                                  min_size=(820, 640))
            webview.start()          # 阻塞至窗口关闭
            return
        except Exception as e:  # noqa: BLE001
            print(f"  [i] 应用窗口不可用（{e}），改用系统浏览器。")

    print("=" * 52)
    print("  论文翻译工具已启动")
    print(f"  地址：{url}（仅本机可访问）")
    print("  关闭本窗口即退出程序。")
    print("=" * 52)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # --browser 强制走系统浏览器（应用窗口异常时的退路，也便于调试）
    main(app_window="--browser" not in sys.argv)
