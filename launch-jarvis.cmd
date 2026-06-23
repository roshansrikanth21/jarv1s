@echo off
title JARVIS
cd /d "%~dp0"

rem Build the UI once (or whenever it's missing), then launch the desktop app.
if not exist "dist\client\index.html" (
  echo Building the JARVIS interface, one moment...
  call npm run build
)

echo Launching JARVIS desktop app...
call "node_modules\.bin\electron.cmd" .
