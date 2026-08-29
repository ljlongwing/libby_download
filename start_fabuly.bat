@echo off
REM start_fabuly.bat -- run the Fabuly downloader from source.

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Python could not be found. Please install it.
    pause
    exit /b
)

python -c "import mutagen" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Installing mutagen...
    pip install mutagen --quiet
)

python fabuly_dl.py --out "./MyBooks" %*
