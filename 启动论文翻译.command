#!/bin/bash
# 论文翻译工具 · macOS / Linux 源码启动脚本
# （Windows 请双击 启动论文翻译.bat；打包好的 Mac 用户请直接双击 .app）
# 首次运行前，可能需要在「终端」执行一次：chmod +x "启动论文翻译.command"

cd "$(dirname "$0")/程序" || { echo "[x] 未找到「程序」文件夹（应与本脚本同目录）。"; read -r -p "按回车键退出…" _; exit 1; }

# 找 Python 3（mac/Linux 用 python3）
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "[x] 未检测到 Python 3。请先安装：https://www.python.org/downloads/"
  read -r -p "按回车键退出…" _; exit 1
fi

# 首次运行自动装核心组件（不含 tkinter——它随 Python 自带，无法用 pip 装）
if ! "$PY" -c "import pdfplumber, reportlab, pypdf, requests, docx, pptx" >/dev/null 2>&1; then
  echo "首次运行，正在安装所需组件，请稍候几分钟……"
  if ! "$PY" -m pip install pdfplumber reportlab pypdf requests pymupdf fonttools rapidocr-onnxruntime python-docx python-pptx pywebview; then
    echo "[x] 组件安装失败，请检查网络连接后重试。"
    read -r -p "按回车键退出…" _; exit 1
  fi
fi

# 可选增强组件（失败不阻塞）
"$PY" -c "import fitz" >/dev/null 2>&1 || "$PY" -m pip install pymupdf fonttools
"$PY" -c "import webview" >/dev/null 2>&1 || "$PY" -m pip install pywebview

echo "正在启动论文翻译工具……（装了应用窗口组件就是原生窗口，否则用系统浏览器；关闭即退出）"
exec "$PY" webui.py
