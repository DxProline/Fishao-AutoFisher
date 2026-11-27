@echo off
setlocal
set "FISHAO_PROJECT=%USERPROFILE%\PycharmProjects\PythonProject6"
set "FISHAO_PYTHON=%FISHAO_PROJECT%\.venv\Scripts\python.exe"
if not exist "%FISHAO_PYTHON%" (
    echo Fishao: Puvodni projekt nebo Python nebyl nalezen.
    pause
    exit /b 1
)
set "FISHAO_PATCH=%~dp0Fishao_1_1_2.patch"
findstr /C:"1.1.0" "%FISHAO_PROJECT%\version.py" >nul 2>&1
if errorlevel 1 set "FISHAO_PATCH=%~dp0Fishao_1_1_2_z_1_1_0.patch"
"%FISHAO_PYTHON%" "%~dp0install_update.py" "%FISHAO_PATCH%" "1.1.2"
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
