@echo off
rem Quorum Web: double-click to open the browser UI. Close this window to stop it.
setlocal
cd /d "%~dp0"
title Quorum Web
rem The environment lives in a short path: Windows breaks on very long ones.
set "VENV=%USERPROFILE%\.quorum\web-venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo First run: setting up Quorum Web. This takes a minute, once.
  if not exist "frontend\dist" mkdir "frontend\dist"
  python -m pip install --quiet --disable-pip-version-check uv || goto :fail
  python -m uv venv --quiet "%VENV%" || goto :fail
  python -m uv pip install --quiet --python "%VENV%\Scripts\python.exe" -e . || goto :fail
)

where claude >nul 2>nul || echo [!] Claude Code is not installed: npm install -g @anthropic-ai/claude-code   then run: claude
where codex >nul 2>nul || echo [!] Codex is not installed: npm install -g @openai/codex   then run: codex login

"%VENV%\Scripts\python.exe" webui\server.py %*
if errorlevel 1 pause
exit /b

:fail
echo.
echo Setup failed. Quorum Web needs Python 3.11 or newer (python.org) on your PATH.
pause
exit /b 1
