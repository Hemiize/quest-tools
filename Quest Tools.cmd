@echo off
rem Opens Quest Tools in its own terminal window (double-click).
chcp 65001 >nul
title Quest Tools
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m questtools %*
if errorlevel 1 (
  echo.
  pause
)
