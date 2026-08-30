@echo off
rem Launcher: starts the interactive harness from any directory.
rem The caller's current folder becomes the agent workspace.
python -m mantra.console %*
if %ERRORLEVEL% NEQ 0 python "%~dp0..\src\mantra\console.py" %*
