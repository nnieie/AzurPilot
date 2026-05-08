@echo off
setlocal
cd /d "%~dp0\.."

set "SRC=dev_tools\usb_capture_lut_accel.c"
set "OUT=dev_tools\usb_capture_lut_accel.dll"
set "OBJ=dev_tools\usb_capture_lut_accel.obj"
set "EXP=dev_tools\usb_capture_lut_accel.exp"
set "OUTLIB=dev_tools\usb_capture_lut_accel.lib"

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
)
if defined VSINSTALL (
    if exist "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" (
        call "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" >nul
        if errorlevel 1 exit /b 1
    ) else if exist "%VSINSTALL%\Common7\Tools\VsDevCmd.bat" (
        call "%VSINSTALL%\Common7\Tools\VsDevCmd.bat" -arch=amd64 -host_arch=amd64 >nul
        if errorlevel 1 exit /b 1
    )
)

if not defined VSINSTALL (
    for /f "tokens=*" %%i in ('where cl.exe 2^>nul') do (
        if not defined CLPATH set "CLPATH=%%~dpi"
    )
    if defined CLPATH (
        for %%i in ("%CLPATH%..\..\..\..\Auxiliary\Build\vcvars64.bat") do (
            if exist "%%~fi" (
                call "%%~fi" >nul
                if errorlevel 1 exit /b 1
            )
        )
    )
)

where cl.exe >nul 2>nul
if errorlevel 1 goto no_cl

cl.exe /nologo /O2 /LD /TC "%SRC%" /Fo:"%OBJ%" /Fe:"%OUT%" /link /NOENTRY /NODEFAULTLIB /IMPLIB:"%OUTLIB%"
if errorlevel 1 exit /b 1

del "%OBJ%" "%EXP%" "%OUTLIB%" 2>nul
echo Built %OUT%
exit /b 0

:no_cl
echo Unable to find MSVC cl.exe. Install "Desktop development with C++" in Visual Studio Build Tools.
exit /b 1
