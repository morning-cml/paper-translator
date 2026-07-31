@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title 论文翻译工具 · 经典界面（备用）

echo.
echo    这是「经典桌面界面」，仅在网页版启动不了时作为备用。
echo    功能已冻结在 2026-07-19，新功能只在网页版更新。
echo    正常请使用上层目录的：启动论文翻译.bat
echo.

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo    [x] 没有检测到 Python，请先安装： https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% run_gui.py
echo.
pause
