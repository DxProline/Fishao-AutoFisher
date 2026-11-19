@echo off
setlocal
set "FISHAO_PYTHON=%USERPROFILE%\PycharmProjects\PythonProject6\.venv\Scripts\python.exe"
if not exist "%FISHAO_PYTHON%" (
    echo Fishao: Puvodni projekt nebo Python nebyl nalezen.
    pause
    exit /b 1
)
"%FISHAO_PYTHON%" "%~dp0install_update.py" "%~dp0Fishao_1_1_0.patch" "1.1.0"
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
