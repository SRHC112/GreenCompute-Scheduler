@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 Python，请安装 Python 3.10+ 并勾选 Add to PATH
  pause
  exit /b 1
)
python "%~dp0install.py" %*
if errorlevel 1 (
  echo.
  echo [ERROR] 安装未完成，请查看上方提示。
  pause
)
