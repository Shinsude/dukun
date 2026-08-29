@echo off
rem Compatibility shim - delegates to scripts\mantra.cmd
python -m mantra.console %*
if %ERRORLEVEL% NEQ 0 python "%~dp0src\mantra\console.py" %*
