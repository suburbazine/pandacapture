@echo off
rem Live dashboard for the Kia Stinger (listen-only), recording the drive as it goes.
rem Keep this file next to pandacapture.exe. Edit the line below to change what it starts:
rem   --map NAME      another address map (see: pandacapture maps)
rem   --record        remove to watch without saving a capture
rem   --mode high     every sample; "normal" for 10 updates a second
rem   --app           a borderless window; remove for a normal browser tab
title PandaCapture dashboard
cd /d "%~dp0"
"%~dp0pandacapture.exe" dashboard --map kia-stinger-33t-pcan --record --mode high --app
if errorlevel 1 (
  echo.
  echo PandaCapture stopped with the error above.
  pause
)
