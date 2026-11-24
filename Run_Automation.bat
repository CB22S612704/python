@echo off
setlocal EnableExtensions
 title AR Untouched Automation

REM Public GitHub repository - no Git/GitHub CLI required
set "RAW_URL=https://raw.githubusercontent.com/CB22S612704/python/main/ar_untouched.py"
set "ROOT=%~dp0"
set "EXCEL_FILE=%ROOT%Untouch.xlsx"
set "LOG_DIR=%ROOT%Logs"

REM Put downloaded source in a hidden temporary folder, not beside the workbook
set "CODE_DIR=%TEMP%\AR_Untouched_Code_%RANDOM%_%RANDOM%"
set "SCRIPT=%CODE_DIR%\ar_untouched.py"

REM Automatically detect Python
set "PYTHON="
set "PY_ARGS="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=py"
        set "PY_ARGS=-3"
    )
)
if not defined PYTHON (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        "%%P" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON=%%P"
            goto PYTHON_FOUND
        )
    )
)
if not defined PYTHON (
    echo ERROR: Python was not found or could not start.
    pause
    exit /b 1
)

:PYTHON_FOUND
echo.
echo ==========================================
echo       AR UNTOUCHED AUTOMATION
echo ==========================================
echo.
echo Python found: %PYTHON% %PY_ARGS%
"%PYTHON%" %PY_ARGS% --version
if errorlevel 1 goto FAIL_BEFORE_RUN

if not exist "%EXCEL_FILE%" (
    echo ERROR: Untouch.xlsx is missing. Keep it beside this BAT file.
    goto FAIL_BEFORE_RUN
)

REM Create temporary folder and mark it hidden
mkdir "%CODE_DIR%" 2>nul
if not exist "%CODE_DIR%" (
    echo ERROR: Could not create temporary code folder.
    goto FAIL_BEFORE_RUN
)
attrib +h +s "%CODE_DIR%" 2>nul

echo.
echo Downloading latest automation code from public GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%RAW_URL%' -OutFile '%SCRIPT%' -UseBasicParsing -ErrorAction Stop; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: Could not download ar_untouched.py.
    goto CLEANUP_FAIL
)
if not exist "%SCRIPT%" (
    echo ERROR: Downloaded Python file was not created.
    goto CLEANUP_FAIL
)
for %%F in ("%SCRIPT%") do if %%~zF EQU 0 (
    echo ERROR: Downloaded Python file is empty.
    goto CLEANUP_FAIL
)

REM Keep workbook, Reports and Logs paths on this PC
set "AR_BASE_DIR=%ROOT%"

echo.
echo Starting automation...
"%PYTHON%" %PY_ARGS% "%SCRIPT%"
set "RESULT=%ERRORLEVEL%"
goto CLEANUP_AFTER_RUN

:FAIL_BEFORE_RUN
set "RESULT=1"
goto CLEANUP_AFTER_RUN

:CLEANUP_FAIL
set "RESULT=1"

:CLEANUP_AFTER_RUN
echo.
echo Cleaning temporary files...
if exist "%CODE_DIR%" (
    attrib -h -s "%CODE_DIR%" /s /d 2>nul
    rmdir /s /q "%CODE_DIR%" 2>nul
)
REM Delete the automation log folder too, as requested
if exist "%LOG_DIR%" (
    attrib -h -s "%LOG_DIR%" /s /d 2>nul
    rmdir /s /q "%LOG_DIR%" 2>nul
)

if "%RESULT%"=="0" (
    echo Automation completed. Temporary code and Logs deleted.
) else (
    echo Automation ended with an error. Temporary code and Logs deleted.
)
echo Untouch.xlsx and Reports folder are kept.
pause
exit /b %RESULT%
