"""启动图形界面（双击 启动论文翻译.bat 即可，无需命令行）。

出错时会弹出提示框并把详情写入 error.log，方便排查。
"""
import os
import sys
import traceback

# 确保以项目根目录为工作目录（双击 .bat 已 cd，此处双保险）
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        from src.gui import main as gui_main
        gui_main()
    except Exception:
        err = traceback.format_exc()
        try:
            with open("error.log", "w", encoding="utf-8") as f:
                f.write(err)
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                "程序启动出错，详情已写入 error.log。\n\n" + err[-1500:])
        except Exception:
            print(err)
        sys.exit(1)


if __name__ == "__main__":
    main()
