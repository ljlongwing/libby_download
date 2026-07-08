@echo off
REM start_chirp.bat

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Python could not be found. Please install it.
    pause
    exit /b
)

echo Checking requirements...
pip install -r requirements.txt --quiet

python -c "import playwright" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Installing playwright...
    pip install playwright
    playwright install chromium
)

python chirp_dl.py --out "./MyBooks" %*
