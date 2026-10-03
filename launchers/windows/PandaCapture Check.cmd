@echo off
rem Shows whether the panda is connected and powered, and its firmware.
title PandaCapture check
cd /d "%~dp0"
"%~dp0pandacapture.exe" list
echo.
"%~dp0pandacapture.exe" info
echo.
pause
