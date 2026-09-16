@echo off
rem Prefer the project runtime; PYTHON32 can override it on another host.
if not defined PYTHON32 set "PYTHON32=%LOCALAPPDATA%\Programs\Python\Python311-32\python.exe"
"%PYTHON32%" "%~dp0runner.py" --adapter j2534 %*
