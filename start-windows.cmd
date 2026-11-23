@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto folder_error

if not exist "app.py" goto incomplete_package
if not exist "requirements.txt" goto incomplete_package
if exist ".venv\Scripts\python.exe" goto check_venv
if exist ".venv" goto invalid_venv

set "QUERYLENS_PYTHON="
set "QUERYLENS_CHECK_VERSION=import sys; sys.exit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)"
py -3.12 -c "%QUERYLENS_CHECK_VERSION%" >nul 2>&1
if not errorlevel 1 set "QUERYLENS_PYTHON=py -3.12"
if defined QUERYLENS_PYTHON goto create_venv
py -3.11 -c "%QUERYLENS_CHECK_VERSION%" >nul 2>&1
if not errorlevel 1 set "QUERYLENS_PYTHON=py -3.11"
if defined QUERYLENS_PYTHON goto create_venv
python -c "%QUERYLENS_CHECK_VERSION%" >nul 2>&1
if not errorlevel 1 set "QUERYLENS_PYTHON=python"
if not defined QUERYLENS_PYTHON goto missing_python

:create_venv
echo Creating the QueryLens Python environment...
%QUERYLENS_PYTHON% -m venv ".venv"
if errorlevel 1 goto venv_error

:check_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)" >nul 2>&1
if errorlevel 1 goto invalid_venv
fc /b "requirements.txt" ".venv\querylens-requirements.txt" >nul 2>&1
if not errorlevel 1 goto start_app

echo Installing QueryLens dependencies. The first launch can take a few minutes...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto dependency_error
copy /y "requirements.txt" ".venv\querylens-requirements.txt" >nul

:start_app
echo.
echo Starting QueryLens at http://127.0.0.1:8501
echo Keep this window open while using QueryLens. Press Ctrl+C to stop it.
".venv\Scripts\python.exe" -m streamlit run "app.py" --server.address 127.0.0.1 --server.port 8501 --server.headless false
if errorlevel 1 goto start_error
popd
exit /b 0

:incomplete_package
echo Extract the full QueryLens ZIP before launching. This folder needs app.py and requirements.txt.
goto failure

:missing_python
echo Install Python 3.12 or 3.11 from https://www.python.org/downloads/windows/
echo Enable the Python launcher during installation, then run this file again.
goto failure

:venv_error
echo QueryLens could not create its Python environment. See the error above.
goto failure

:invalid_venv
echo The existing .venv folder cannot run a supported version of Python.
echo Rename .venv to .venv-old, then run this file again.
goto failure

:dependency_error
echo Dependency installation failed. Check the error above and your internet connection, then run this file again.
goto failure

:start_error
echo QueryLens stopped during startup. Check the error above.
echo If port 8501 is busy, close the other app using that port and try again.
goto failure

:folder_error
echo QueryLens could not open its folder. Extract the ZIP to a writable folder and try again.
pause
exit /b 1

:failure
popd
pause
exit /b 1
