@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title 回填后端自测

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo    [x] 没有检测到 Python，请先运行 启动论文翻译.bat 完成安装。
  pause
  exit /b 1
)

%PY% -c "import fitz" >nul 2>nul
if errorlevel 1 (
  echo    正在安装 PyMuPDF（精确回填组件）……
  %PY% -m pip install pymupdf fonttools
)

%PY% selftest_backend.py
exit /b %errorlevel%
