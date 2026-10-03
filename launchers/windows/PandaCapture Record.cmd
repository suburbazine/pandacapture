@echo off
rem Records the bus to a capture in the captures folder (listen-only).
rem Keys while recording: M = marker, 1-9 = numbered marker, Q = stop and save.
title PandaCapture recording
cd /d "%~dp0"
"%~dp0pandacapture.exe"
echo.
pause
