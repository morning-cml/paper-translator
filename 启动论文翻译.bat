@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title 论文翻译工具

if not exist "%~dp0程序\webui.py" (
  echo.
  echo    [x] 未找到「程序」文件夹（应与本脚本在同一目录）。
  echo    请不要移动、重命名或删除「程序」文件夹。
  echo.
  pause
  exit /b 1
)
cd /d "%~dp0程序"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo    [x] 没有检测到 Python。
  echo    请先安装 Python 3.9 或更高版本，安装时务必勾选 "Add Python to PATH"。
  echo    下载地址： https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY% -c "import pdfplumber, reportlab, pypdf, requests, tkinter, docx, pptx" >nul 2>nul
if errorlevel 1 (
  echo.
  echo    首次运行，正在自动安装所需组件，请稍候几分钟……
  echo.
  %PY% -m pip install pdfplumber reportlab pypdf requests pymupdf fonttools rapidocr-onnxruntime python-docx python-pptx pywebview
  if errorlevel 1 (
    echo.
    echo    [x] 组件安装失败，请检查网络连接后重新双击本文件。
    echo.
    pause
    exit /b 1
  )
)

rem 补装可选增强组件（失败不阻塞）
%PY% -c "import fitz" >nul 2>nul
if errorlevel 1 %PY% -m pip install pymupdf fonttools
%PY% -c "import rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 %PY% -m pip install rapidocr-onnxruntime

rem 应用窗口组件（可选）：装上则用无边框原生窗口，否则回退系统浏览器
%PY% -c "import webview" >nul 2>nul
if errorlevel 1 %PY% -m pip install pywebview

%PY% -c "import webview" >nul 2>nul
if errorlevel 1 goto browser_mode

echo    正在打开应用窗口……
start "" %PY%w webui.py
exit /b 0

:browser_mode
echo.
echo    正在启动界面，浏览器将自动打开……
echo    【提示】关闭本窗口即退出程序。
echo.
%PY% webui.py --browser
exit /b 0
