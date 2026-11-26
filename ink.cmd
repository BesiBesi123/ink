@echo off
rem ink from this folder (install.ps1 also puts an `ink` command on your PATH)
if not defined INK_PY set "INK_PY=python"
where node >nul 2>nul
if errorlevel 1 goto plain
if not exist "%~dp0tui\node_modules" goto plain
node "%~dp0tui\ink-tui.mjs" %*
exit /b %errorlevel%
:plain
"%INK_PY%" -X utf8 "%~dp0ink.py" %*
