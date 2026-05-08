@echo off
setlocal
cd /d "%~dp0\.."
set "CONFIG_NAME=%~1"
if "%CONFIG_NAME%"=="" set CONFIG_NAME=alas
shift
set "EXTRA_ARGS="
:collect_args
if "%~1"=="" goto run
set EXTRA_ARGS=%EXTRA_ARGS% "%~1"
shift
goto collect_args
:run
toolkit\python.exe dev_tools\usb_capture_color_calibrate.py --config-name "%CONFIG_NAME%" %EXTRA_ARGS%
pause
