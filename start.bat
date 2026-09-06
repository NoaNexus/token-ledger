@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONHOME="
set "PYTHONPATH="


if exist "%~dp0.venv\Scripts\python.exe" goto run_venv
if exist "%USERPROFILE%\anaconda\python.exe" goto run_anaconda
if exist "%USERPROFILE%\miniconda3\python.exe" goto run_miniconda

where py >nul 2>nul
if errorlevel 1 goto use_python

py -3.11 run.py
set "ledger_exit=%errorlevel%"
goto after_run

:run_venv
"%~dp0.venv\Scripts\python.exe" run.py
set "ledger_exit=%errorlevel%"
goto after_run

:run_anaconda
"%USERPROFILE%\anaconda\python.exe" run.py
set "ledger_exit=%errorlevel%"
goto after_run

:run_miniconda
"%USERPROFILE%\miniconda3\python.exe" run.py
set "ledger_exit=%errorlevel%"
goto after_run

:use_python
where python >nul 2>nul
if errorlevel 1 goto no_python

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto no_python
python run.py
set "ledger_exit=%errorlevel%"
goto after_run

:no_python
echo [Token Ledger] Python 3.10 or newer was not found.
echo Install Python 3.11 or newer, then run start.bat again.
set "ledger_exit=9009"

:after_run
if "%ledger_exit%"=="0" goto success
echo.
echo [Token Ledger] Startup failed with exit code %ledger_exit%.
echo Keep the error details above for troubleshooting.
echo.
pause
endlocal
exit /b %ledger_exit%

:success
endlocal
exit /b 0
