@echo off
REM Movie Mode - live monitor. Double-click this, or run it from anywhere.
REM
REM %~dp0 is THIS file's folder, so the script is found no matter what the
REM working directory is when it launches (double-clicking from Explorer sets
REM it to something else entirely).
REM
REM -ExecutionPolicy Bypass because watch.ps1 is unsigned, and the default
REM Windows policy silently refuses to run unsigned .ps1 files - which looks
REM exactly like the monitor being broken.

title Movie Mode - live

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watch.ps1"

REM Only reached if the watcher exits (Ctrl+C, or an error). Hold the window
REM open so an error message is readable instead of vanishing.
echo.
echo   Monitor closed.
pause
