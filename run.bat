@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo 未找到 Python，请先安装并加入 PATH & pause & exit /b 1)
if not exist "bin\chrome-win64\chrome.exe" (
  echo 首次运行：正在下载便携版 Chrome...
  python setup_chrome.py
)
if not exist "bin\python\python.exe" (
  echo 首次运行：正在下载执行代码用的 Python 3.13.15（与 NVDA 一致）...
  python setup_python.py
)
python -m doubao_client
if errorlevel 1 pause
