@echo off
setlocal
cd /d "%~dp0\.."
"%CD%\toolkit\python.exe" dev_tools\usb_capture_latency_benchmark.py %*
