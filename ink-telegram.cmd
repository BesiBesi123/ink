@echo off
rem ink on Telegram: set telegram_token (and telegram_chat_id) in %USERPROFILE%\.ink\config.json first
if not defined INK_PY set "INK_PY=python"
"%INK_PY%" -X utf8 "%~dp0ink_telegram.py" %*
