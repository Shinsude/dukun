@echo off
rem Windows launcher for the console. Delegates to the interpreter module.
python -m mantra.console %*
if %ERRORLEVEL% NEQ 0 python "%~dp0src\mantra\console.py" %*
