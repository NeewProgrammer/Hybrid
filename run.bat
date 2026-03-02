@echo off
chcp 65001 >nul

echo Checking dependencies...
pip install -r "%~dp0requirements.txt" -q 2>nul
if errorlevel 1 (
    echo [WARNING] Dependency install had issues, trying to continue...
)

echo Starting application...
python "%~dp0src\main.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Application failed to start, please check the error message above.
)
echo.
pause
