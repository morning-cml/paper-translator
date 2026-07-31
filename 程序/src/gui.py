"""图形界面（Tkinter）——【已冻结 · 备用界面】。

⚠️ 2026-07-19 起本界面停止演进：正式界面为网页版（webui.py + web/），
   本文件仅作"网页版起不来"时的应急备胎，入口 程序/备用-经典界面.bat。
   **新功能一律只加在网页版**；本文件功能冻结在 2026-07-19 的状态
   （服务预设/领域/试译/三种输出模式/两端对齐等 T1–T12 均已具备）。
   若确需改动，请同步核对 web/js/components.js，避免两端再次不一致。

设计目标——好用：
  · 分区清晰：① 选择文件 ② 翻译设置 ③ 开始与进度
  · 支持一次添加多个 PDF，批量翻译
  · API Key 可显示/隐藏、可记住、可「测试连接」提前发现问题
  · 实时进度条 + 状态 + 滚动日志；可随时「取消」
  · 完成后一键「打开 PDF」「打开文件夹」
  · 记住上次的设置（模型、模式、术语库、输出位置、是否记住 Key）
翻译在后台线程执行，通过队列把消息回传界面，界面不卡顿。
"""
from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .config import load_config, save_config
from .pipeline import translate_pdf, check_connection, CancelledError
from .translator import TranslatorError

MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"]
MODEL_HINT = "v4-pro：质量更高（推荐论文）  ·  v4-flash：更快更省"

BACKENDS = [("自动（推荐）", "auto"),
            ("PyMuPDF·精确抹除", "pymupdf"),
            ("reportlab·兼容覆盖", "reportlab")]
BACKEND_LABELS = [b[0] for b in BACKENDS]
BACKEND_VALUE = {label: v for label, v in BACKENDS}
BACKEND_LABEL = {v: label for label, v in BACKENDS}

# T9 服务预设：任何 OpenAI 兼容接口皆可；切换自动预填接口地址与模型（可改）。
# 各家申请入口：DeepSeek platform.deepseek.com；Kimi platform.kimi.com；
# 智谱 open.bigmodel.cn；豆包 console.volcengine.com/ark（模型需先在方舟开通）。
SERVICES = [
    ("DeepSeek 官方", "https://api.deepseek.com",
     ["deepseek-v4-pro", "deepseek-v4-flash"]),
    ("Kimi（月之暗面）", "https://api.moonshot.cn/v1",
     ["kimi-k2.6", "kimi-k3"]),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
     ["glm-5.2"]),
    ("豆包（火山方舟）", "https://ark.cn-beijing.volces.com/api/v3",
     ["doubao-seed-2.0-pro", "doubao-seed-2.0-lite"]),
    ("OpenAI", "https://api.openai.com/v1",
     ["gpt-4o-mini", "gpt-4o"]),
    ("Ollama 本地（免费离线）", "http://127.0.0.1:11434/v1",
     ["qwen2.5:7b", "llama3.1:8b"]),
    ("自定义", "", []),
]
SERVICE_LABELS = [s[0] for s in SERVICES]

# T10 领域预设（可手填其他学科）
DOMAINS = ["计算机科学", "通用学术", "生物医学", "物理学", "数学", "电子工程", "化学"]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.worker = None
        self.conn_thread = None
        self.cancel_event = threading.Event()
        self.running = False
        self.outputs: list[str] = []
        cfg = load_config()

        root.title("论文翻译工具 · 经典界面（仅英译中）")
        root.minsize(720, 760)
        try:
            ttk.Style().theme_use("clam")
        except Exception:
            pass

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        # 顶部标题
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="论文翻译工具 · 经典界面（仅英译中）",
                  font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(head, text="基于 DeepSeek · 保留图片 / 公式 / 排版 · 术语库增强",
                  foreground="#666").pack(anchor="w")

        # ① 选择文件
        sec1 = ttk.LabelFrame(outer, text="① 选择 PDF（可添加多个，批量翻译）", padding=10)
        sec1.grid(row=1, column=0, sticky="ew", pady=6)
        sec1.columnconfigure(0, weight=1)
        list_wrap = ttk.Frame(sec1)
        list_wrap.grid(row=0, column=0, sticky="ew")
        list_wrap.columnconfigure(0, weight=1)
        self.filelist = tk.Listbox(list_wrap, height=5, activestyle="none")
        self.filelist.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.filelist.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.filelist.config(yscrollcommand=sb.set)
        btnrow = ttk.Frame(sec1)
        btnrow.grid(row=0, column=1, sticky="n", padx=(8, 0))
        ttk.Button(btnrow, text="添加…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btnrow, text="移除所选", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btnrow, text="清空", command=self.clear_files).pack(fill="x", pady=2)

        # ② 翻译设置
        sec2 = ttk.LabelFrame(outer, text="② 翻译设置", padding=10)
        sec2.grid(row=2, column=0, sticky="ew", pady=6)
        sec2.columnconfigure(1, weight=1)
        p = {"padx": 6, "pady": 4}

        ttk.Label(sec2, text="API Key：").grid(row=0, column=0, sticky="w", **p)
        keyf = ttk.Frame(sec2)
        keyf.grid(row=0, column=1, columnspan=2, sticky="ew", **p)
        keyf.columnconfigure(0, weight=1)
        self.key_var = tk.StringVar(value=cfg.api_key)
        self.key_entry = ttk.Entry(keyf, textvariable=self.key_var, show="*")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(keyf, text="显示", variable=self.show_key,
                        command=self.toggle_key).grid(row=0, column=1, padx=6)
        self.remember_var = tk.BooleanVar(value=bool(cfg.api_key))
        ttk.Checkbutton(keyf, text="记住", variable=self.remember_var).grid(row=0, column=2, padx=6)
        self.test_btn = ttk.Button(keyf, text="测试连接", command=self.test_connection)
        self.test_btn.grid(row=0, column=3, padx=(6, 0))

        # T9 服务预设行：切换自动预填接口地址与模型（均可再编辑）
        ttk.Label(sec2, text="服务：").grid(row=1, column=0, sticky="w", **p)
        srow = ttk.Frame(sec2)
        srow.grid(row=1, column=1, columnspan=2, sticky="ew", **p)
        srow.columnconfigure(2, weight=1)
        init_svc = next((n for n, b, _m in SERVICES
                         if b and (cfg.base_url or "").rstrip("/") == b), "自定义")
        self.service_var = tk.StringVar(value=init_svc)
        svc_combo = ttk.Combobox(srow, textvariable=self.service_var,
                                 values=SERVICE_LABELS, state="readonly", width=18)
        svc_combo.grid(row=0, column=0)
        svc_combo.bind("<<ComboboxSelected>>", self.on_service)
        ttk.Label(srow, text="  接口：").grid(row=0, column=1)
        self.baseurl_var = tk.StringVar(value=cfg.base_url)
        ttk.Entry(srow, textvariable=self.baseurl_var).grid(row=0, column=2, sticky="ew")

        ttk.Label(sec2, text="模型：").grid(row=2, column=0, sticky="w", **p)
        self.model_var = tk.StringVar(value=cfg.model or MODELS[0])
        self.workers_var = tk.StringVar(value=str(cfg.max_workers))
        mrow = ttk.Frame(sec2)
        mrow.grid(row=2, column=1, columnspan=2, sticky="w", **p)
        self.model_combo = ttk.Combobox(mrow, textvariable=self.model_var,
                                        values=MODELS, width=20)
        self.model_combo.pack(side="left")
        ttk.Label(mrow, text="    并发数：").pack(side="left")
        ttk.Spinbox(mrow, from_=1, to=32, width=4, textvariable=self.workers_var).pack(side="left")
        ttk.Label(mrow, text="    回填：").pack(side="left")
        self.backend_var = tk.StringVar(
            value=BACKEND_LABEL.get(getattr(cfg, "render_backend", "auto"),
                                    BACKEND_LABELS[0]))
        ttk.Combobox(mrow, textvariable=self.backend_var, values=BACKEND_LABELS,
                     state="readonly", width=17).pack(side="left")

        ttk.Label(sec2, text="输出模式：").grid(row=3, column=0, sticky="w", **p)
        self.mode_var = tk.StringVar(value=cfg.output_mode)
        mf = ttk.Frame(sec2)
        mf.grid(row=3, column=1, columnspan=2, sticky="w", **p)
        ttk.Radiobutton(mf, text="纯中文", value="translated",
                        variable=self.mode_var).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(mf, text="双语·前后页", value="bilingual",
                        variable=self.mode_var).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(mf, text="双语·左右对照", value="sidebyside",
                        variable=self.mode_var).pack(side="left", padx=(0, 14))
        # T3 试译模式：只翻前 N 页，便宜预览
        ttk.Label(mf, text="试译前").pack(side="left")
        self.trial_var = tk.StringVar(value=str(getattr(cfg, "max_pages", 0) or 0))
        ttk.Spinbox(mf, from_=0, to=999, width=4,
                    textvariable=self.trial_var).pack(side="left")
        ttk.Label(mf, text="页（0=整篇）").pack(side="left")

        ttk.Label(sec2, text="领域：").grid(row=4, column=0, sticky="w", **p)
        drow = ttk.Frame(sec2)
        drow.grid(row=4, column=1, columnspan=2, sticky="w", **p)
        self.domain_var = tk.StringVar(value=getattr(cfg, "domain", "计算机科学"))
        ttk.Combobox(drow, textvariable=self.domain_var, values=DOMAINS,
                     width=20).pack(side="left")
        ttk.Label(drow, text="   影响翻译口径，可手填其他学科",
                  foreground="#888").pack(side="left")

        ttk.Label(sec2, text="术语库：").grid(row=5, column=0, sticky="w", **p)
        gf = ttk.Frame(sec2)
        gf.grid(row=5, column=1, columnspan=2, sticky="ew", **p)
        gf.columnconfigure(0, weight=1)
        self.gloss_var = tk.StringVar(value=cfg.glossary_path)
        ttk.Entry(gf, textvariable=self.gloss_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(gf, text="浏览…", command=self.pick_glossary).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(sec2, text="输出到：").grid(row=6, column=0, sticky="w", **p)
        of = ttk.Frame(sec2)
        of.grid(row=6, column=1, columnspan=2, sticky="ew", **p)
        of.columnconfigure(1, weight=1)
        self.outloc_var = tk.StringVar(value="same")
        ttk.Radiobutton(of, text="原文件同目录", value="same",
                        variable=self.outloc_var).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(of, text="指定：", value="custom",
                        variable=self.outloc_var).grid(row=1, column=0, sticky="w")
        self.outdir_var = tk.StringVar()
        ttk.Entry(of, textvariable=self.outdir_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(of, text="浏览…", command=self.pick_output_dir).grid(row=1, column=2)

        self.mock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec2, text="离线测试模式（不联网，用占位译文检查排版）",
                        variable=self.mock_var).grid(row=7, column=1, columnspan=2, sticky="w", **p)

        self.direct_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec2, text="直连模式（忽略系统代理/VPN；出现 SSL 连接错误时勾选）",
                        variable=self.direct_var).grid(row=8, column=1, columnspan=2, sticky="w", **p)

        self.thinking_var = tk.BooleanVar(value=cfg.thinking)
        ttk.Checkbutton(sec2, text="思考模式（质量更高，但更慢；只求速度可取消）",
                        variable=self.thinking_var).grid(row=9, column=1, columnspan=2, sticky="w", **p)

        # ③ 开始与进度
        sec3 = ttk.LabelFrame(outer, text="③ 开始翻译", padding=10)
        sec3.grid(row=3, column=0, sticky="nsew", pady=6)
        outer.rowconfigure(3, weight=1)
        sec3.columnconfigure(0, weight=1)

        actions = ttk.Frame(sec3)
        actions.grid(row=0, column=0, sticky="ew")
        self.start_btn = ttk.Button(actions, text="▶ 开始翻译", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text="取消", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.open_pdf_btn = ttk.Button(actions, text="打开 PDF", command=self.open_pdf,
                                       state="disabled")
        self.open_pdf_btn.pack(side="left", padx=6)
        self.open_dir_btn = ttk.Button(actions, text="打开文件夹", command=self.open_folder,
                                       state="disabled")
        self.open_dir_btn.pack(side="left")

        self.prog = ttk.Progressbar(sec3, mode="determinate", maximum=100)
        self.prog.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.status = tk.StringVar(value="就绪。请添加英文 PDF，填写 API Key。")
        ttk.Label(sec3, textvariable=self.status, foreground="#333").grid(
            row=2, column=0, sticky="w")
        self.logbox = ScrolledText(sec3, height=8, state="disabled", wrap="word",
                                   font=("Consolas", 9))
        self.logbox.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        sec3.rowconfigure(3, weight=1)

        self._center()
        self.root.after(120, self.pump)

    # ---------------- 文件列表 ----------------
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择英文 PDF", filetypes=[("PDF", "*.pdf")])
        existing = set(self.filelist.get(0, "end"))
        for p in paths:
            if p not in existing:
                self.filelist.insert("end", p)

    def remove_selected(self):
        for i in reversed(self.filelist.curselection()):
            self.filelist.delete(i)

    def clear_files(self):
        self.filelist.delete(0, "end")

    # ---------------- 各种选择 ----------------
    def toggle_key(self):
        self.key_entry.config(show="" if self.show_key.get() else "*")

    def pick_glossary(self):
        p = filedialog.askopenfilename(title="选择术语库 CSV", filetypes=[("CSV", "*.csv")])
        if p:
            self.gloss_var.set(p)

    def pick_output_dir(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.outdir_var.set(d)
            self.outloc_var.set("custom")

    def on_service(self, _evt=None):
        """T9：切换服务预设 → 预填接口地址与模型下拉（保持可编辑）。"""
        label = self.service_var.get()
        for name, base, models in SERVICES:
            if name == label:
                if base:
                    self.baseurl_var.set(base)
                if models:
                    self.model_combo.config(values=models)
                    if self.model_var.get() not in models:
                        self.model_var.set(models[0])
                break

    # ---------------- 配置 ----------------
    def _build_cfg(self):
        try:
            workers = max(1, min(64, int(float(self.workers_var.get()))))
        except Exception:
            workers = 8
        try:
            trial = max(0, int(float(self.trial_var.get())))
        except Exception:
            trial = 0
        return load_config(
            api_key=self.key_var.get().strip(),
            model=self.model_var.get().strip(),
            base_url=self.baseurl_var.get().strip() or None,
            output_mode=self.mode_var.get(),
            glossary_path=self.gloss_var.get().strip() or None,
            use_system_proxy=not self.direct_var.get(),
            max_workers=workers,
            thinking=self.thinking_var.get(),
            render_backend=BACKEND_VALUE.get(self.backend_var.get(), "auto"),
            domain=self.domain_var.get().strip() or None,
            max_pages=trial,
        )

    def _save_settings(self, cfg):
        # 仅在勾选“记住”时把 Key 存盘；注意保存副本，绝不修改正在使用的 cfg
        import dataclasses
        to_save = dataclasses.replace(cfg)
        if not self.remember_var.get():
            to_save.api_key = ""
        try:
            save_config(to_save)
        except Exception:
            pass

    def _out_path(self, f):
        stem = Path(f).stem
        suffix = {"bilingual": "_translation_bilingual",
                  "sidebyside": "_translation_sidebyside"}.get(
            self.mode_var.get(), "_translation")
        base = Path(f).parent if self.outloc_var.get() == "same" else Path(self.outdir_var.get())
        return str(base / (stem + suffix + ".pdf"))

    # ---------------- 测试连接 ----------------
    def test_connection(self):
        if self.conn_thread and self.conn_thread.is_alive():
            return
        if not self.key_var.get().strip():
            messagebox.showerror("错误", "请先填写 API Key。")
            return
        cfg = self._build_cfg()
        self.test_btn.config(state="disabled")
        self.status.set("正在测试连接…")

        def run():
            ok, msg = check_connection(cfg)
            self.q.put(("conn", ok, msg))

        self.conn_thread = threading.Thread(target=run, daemon=True)
        self.conn_thread.start()

    # ---------------- 翻译 ----------------
    def start(self):
        if self.running:
            return
        files = list(self.filelist.get(0, "end"))
        if not files:
            messagebox.showerror("错误", "请先添加至少一个 PDF。")
            return
        mock = self.mock_var.get()
        if not mock and not self.key_var.get().strip():
            messagebox.showerror("错误", "请填写 DeepSeek API Key（或勾选离线测试模式）。")
            return
        if self.outloc_var.get() == "custom":
            d = self.outdir_var.get().strip()
            if not d:
                messagebox.showerror("错误", "请选择输出文件夹，或改为「原文件同目录」。")
                return
            Path(d).mkdir(parents=True, exist_ok=True)

        cfg = self._build_cfg()
        self._save_settings(cfg)

        self.cancel_event.clear()
        self.outputs = []
        self.running = True
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_pdf_btn.config(state="disabled")
        self.open_dir_btn.config(state="disabled")
        self.prog["value"] = 0
        self._log_clear()

        n = len(files)

        def run():
            outs = []
            try:
                for i, f in enumerate(files):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    out = self._out_path(f)
                    self.q.put(("log", f"[{i+1}/{n}] 开始：{Path(f).name}"))

                    def prog(msg, frac, i=i):
                        self.q.put(("progress", (i + frac) / n,
                                    f"[{i+1}/{n}] {Path(f).name} — {msg}"))

                    res = translate_pdf(f, out, cfg, mock=mock, progress=prog,
                                        should_cancel=self.cancel_event.is_set)
                    outs.append(out)
                    self.q.put(("log",
                                f"[{i+1}/{n}] 完成：{Path(out).name}"
                                f"（{res['pages']} 页 / {res['blocks']} 段）"))
                self.q.put(("all_done", outs))
            except CancelledError:
                self.q.put(("cancelled", outs))
            except TranslatorError as e:
                self.q.put(("error", str(e), outs))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"{e}\n\n{traceback.format_exc()}", outs))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def cancel(self):
        if self.running:
            self.cancel_event.set()
            self.status.set("正在取消（当前批次完成后停止）…")
            self.cancel_btn.config(state="disabled")

    # ---------------- 消息泵（常驻，主线程更新界面）----------------
    def pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self.prog["value"] = item[1] * 100
                    self.status.set(item[2])
                elif kind == "log":
                    self._log(item[1])
                elif kind == "conn":
                    ok, msg = item[1], item[2]
                    self.test_btn.config(state="normal")
                    self.status.set("连接正常。" if ok else "连接失败。")
                    (messagebox.showinfo if ok else messagebox.showerror)(
                        "测试连接", msg)
                elif kind == "all_done":
                    self.outputs = item[1]
                    self._finish(f"全部完成：{len(self.outputs)} 个文件。")
                    messagebox.showinfo("完成", "翻译完成！\n" + "\n".join(
                        Path(o).name for o in self.outputs))
                elif kind == "cancelled":
                    self.outputs = item[1]
                    self._finish(f"已取消（已完成 {len(self.outputs)} 个）。")
                elif kind == "error":
                    self.outputs = item[2] if len(item) > 2 else []
                    self._finish("出错，已停止。")
                    messagebox.showerror("翻译失败", item[1])
        except queue.Empty:
            pass
        self.root.after(120, self.pump)

    def _finish(self, status_text):
        self.running = False
        self.prog["value"] = 100 if self.outputs else 0
        self.status.set(status_text)
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if self.outputs:
            self.open_pdf_btn.config(state="normal")
            self.open_dir_btn.config(state="normal")

    # ---------------- 日志 ----------------
    def _log(self, text):
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _log_clear(self):
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")

    # ---------------- 打开结果 ----------------
    def open_pdf(self):
        if self.outputs:
            self._open(self.outputs[0])

    def open_folder(self):
        if self.outputs:
            self._open(str(Path(self.outputs[0]).resolve().parent))

    def _open(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif os.name == "posix":
                import subprocess
                opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
                subprocess.Popen([opener, path])
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo("提示", f"路径：{path}\n（无法自动打开：{e}）")

    def _center(self):
        self.root.update_idletasks()
        w, h = 720, 700
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y = (sw - w) // 2, max((sh - h) // 3, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
