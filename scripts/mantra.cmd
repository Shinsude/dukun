@echo off
rem Launcher: starts the interactive harness from any directory.
rem The caller's current folder becomes the agent workspace.
setlocal
set "MANTRA_SRC=%~dp0..\src"
python -m mantra.console %*
if %ERRORLEVEL% EQU 0 goto :done
rem The package is not installed: put the source tree on the path and
rem retry, rather than running the module as a script (which cannot
rem resolve the package's own imports).
set "PYTHONPATH=%MANTRA_SRC%;%PYTHONPATH%"
python -m mantra.console %*
:done
endlocal
