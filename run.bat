@echo off
rem Dork on Windows: double-click to set up and start (same as run.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
