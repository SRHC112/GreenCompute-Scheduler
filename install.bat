@echo off
rem GreenCompute-Scheduler - Windows bootstrap.
rem ASCII-only on purpose: cmd.exe parses batch files with the active console
rem code page; non-ASCII bytes - Chinese text - can be swallowed as DBCS lead
rem bytes and split a line into fragments that cmd then tries to execute.
rem All detailed Chinese messages are printed by install.py - UTF-8 - below.
rem Keep this file free of parentheses inside if-blocks: cmd mis-parses them.
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.10+ and tick "Add to PATH".
  echo [HINT]  Install Python 3.10+ with the "Add to PATH" option, then run this file again.
  exit /b 1
)
python "%~dp0install.py" %*
set "GCS_RC=%errorlevel%"
if not "%GCS_RC%"=="0" (
  echo.
  echo [ERROR] Installation did not finish - see the Chinese messages above.
)
exit /b %GCS_RC%
