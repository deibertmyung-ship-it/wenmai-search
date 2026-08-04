@echo off
rem ============================================================================
rem  Wenmai Search local stack - no Docker required
rem
rem    run.bat             Start API (embedded Qdrant + worker) and Web
rem    run.bat stop        Stop API, its worker, and Web
rem    run.bat status      Show API, embedded worker, and Web status
rem    run.bat restart     Restart the complete local stack
rem    run.bat worker      Compatibility alias for restart
rem    run.bat reembed     Rebuild embedded Qdrant from stored SQLite chunks
rem
rem  ASCII + CRLF are intentional so cmd.exe can parse this file under every
rem  Windows code page.
rem ============================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "SVC_DIR=%ROOT%\knowledge-service"
set "WEB_DIR=%ROOT%\knowledge-web"
set "SVC_PY=%SVC_DIR%\.venv\Scripts\python.exe"
set "WEB_PY=%WEB_DIR%\.venv\Scripts\python.exe"

set "API_HOST=127.0.0.1"
set "API_PORT=8077"
set "WEB_PORT=5055"
set "API_URL=http://%API_HOST%:%API_PORT%"
set "WEB_URL=http://127.0.0.1:%WEB_PORT%"

rem Existing environment variables win, so embeddings and remote overrides remain
rem customizable. With no KB_QDRANT_URL, Settings uses embedded .kbdata\qdrant.
if not defined KB_PROFILE set "KB_PROFILE=local"
if not defined KB_DATA_DIR set "KB_DATA_DIR=%SVC_DIR%\.kbdata"
if not defined KB_API_WORKER_ENABLED set "KB_API_WORKER_ENABLED=true"
if not defined KB_DENSE_PROVIDER set "KB_DENSE_PROVIDER=fastembed"
if not defined KB_DENSE_MODEL set "KB_DENSE_MODEL=BAAI/bge-small-zh-v1.5"
if not defined KB_OPEN_BROWSER set "KB_OPEN_BROWSER=true"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if not defined KBWEB_API_BASE set "KBWEB_API_BASE=%API_URL%"
if not defined KBWEB_SECRET_KEY set "KBWEB_SECRET_KEY=local-manual-testing"
if not defined KBWEB_DEBUG_UI set "KBWEB_DEBUG_UI=true"
if not defined KBWEB_TIMEOUT set "KBWEB_TIMEOUT=60"

set "LOG_DIR=%ROOT%\logs"
set "SVC_LOG=%LOG_DIR%\kbsvc.log"
set "WEB_LOG=%LOG_DIR%\kbweb.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

set "MODEL_DIR=%KB_DATA_DIR%\models\fast-bge-small-zh-v1.5"
set "MODEL_ONNX=%MODEL_DIR%\model_optimized.onnx"

if /i "%~1"=="stop"    goto do_stop
if /i "%~1"=="status"  goto do_status
if /i "%~1"=="restart" goto do_restart
if /i "%~1"=="worker"  goto do_worker
if /i "%~1"=="reembed" goto do_reembed
if not "%~1"=="" goto usage

:start_stack
echo.
echo   Wenmai Search local stack - embedded mode
echo   ------------------------------------------------------------------

call :check_env
if errorlevel 1 goto fail

call :port_busy %API_PORT%
if not errorlevel 1 (
  echo   [ERROR] API port %API_PORT% is busy. Run: run.bat stop
  goto fail
)
call :port_busy %WEB_PORT%
if not errorlevel 1 (
  echo   [ERROR] Web port %WEB_PORT% is busy. Run: run.bat stop
  goto fail
)

call :ensure_database
if errorlevel 1 goto fail

echo   [1/3] Starting API with embedded Qdrant and worker  %API_URL%
pushd "%SVC_DIR%"
start "kbsvc %API_PORT%" /min cmd /d /c ""%SVC_PY%" -m kbsvc.cli serve --host %API_HOST% --port %API_PORT% > "%SVC_LOG%" 2>&1"
popd
call :wait_http "%API_URL%/readyz" 60
if errorlevel 1 (
  echo   [ERROR] API did not become ready within 120 seconds. Log: %SVC_LOG%
  goto fail
)
call :wait_worker 15
if errorlevel 1 (
  echo   [ERROR] The API started but its embedded worker is not running.
  echo           Log: %SVC_LOG%
  goto fail
)
echo         ready - uploads will be processed automatically

echo   [2/3] Starting Web  %WEB_URL%
pushd "%WEB_DIR%"
start "kbweb %WEB_PORT%" /min cmd /d /c ""%WEB_PY%" -m waitress --host=127.0.0.1 --port=%WEB_PORT% wsgi:app > "%WEB_LOG%" 2>&1"
popd
call :wait_http "%WEB_URL%/" 30
if errorlevel 1 (
  echo   [ERROR] Web did not become ready. Log: %WEB_LOG%
  goto fail
)
echo         ready

echo   [3/3] Warming search and opening the browser
curl -s -o nul --max-time 180 "%WEB_URL%/?f=1^&q=%%E8%%B4%%BC%%E5%%85%%8B^&rerank=1^&rewrite=1" >nul 2>&1
if /i "%KB_OPEN_BROWSER%"=="true" start "" "%WEB_URL%"

echo.
echo   ------------------------------------------------------------------
call :print_stats
echo.
echo    Storage  SQLite + local files + embedded Qdrant
echo    Worker   built into the API process
echo    Web      %WEB_URL%
echo    API docs %API_URL%/docs
echo.
echo    No Docker or manual worker command is required.
echo    Stop everything with: run.bat stop
echo   ------------------------------------------------------------------
echo.
goto end

:do_stop
echo.
echo   Stopping API, embedded worker, and Web ...
call :stop_apps
echo   Stopped. SQLite, source files, and embedded Qdrant data are preserved.
echo.
goto end

:do_status
echo.
call :port_busy %API_PORT%
if not errorlevel 1 (echo   API             running   %API_URL%) else (echo   API             stopped)
call :worker_running
if not errorlevel 1 (echo   embedded worker running   inside API) else (echo   embedded worker stopped)
call :port_busy %WEB_PORT%
if not errorlevel 1 (echo   Web             running   %WEB_URL%) else (echo   Web             stopped)
call :port_busy %API_PORT%
if not errorlevel 1 call :print_stats
echo.
goto end

:do_restart
echo.
echo   Restarting the complete local stack ...
call :stop_apps
ping -n 3 127.0.0.1 >nul
goto start_stack

:do_worker
echo.
echo   The worker now lives inside the API process; restarting the local stack.
call :stop_apps
ping -n 3 127.0.0.1 >nul
goto start_stack

:do_reembed
echo.
echo   Rebuilding embedded Qdrant from stored SQLite chunks.
echo   API, worker, and Web will stop. SQLite and source files are preserved.
call :check_env
if errorlevel 1 goto fail
call :stop_apps
ping -n 4 127.0.0.1 >nul
call :ensure_database
if errorlevel 1 goto fail

pushd "%SVC_DIR%"
"%SVC_PY%" -m kbsvc.cli reembed --batch-size 256
set "REEMBED_RESULT=!errorlevel!"
popd
if not "!REEMBED_RESULT!"=="0" (
  echo   [ERROR] Re-embedding failed. Check the output above.
  goto fail
)
echo   Re-embedding finished. Starting the complete stack ...
goto start_stack

rem ============================================================================
rem  Subroutines
rem ============================================================================

:check_env
if not exist "%SVC_PY%" (
  echo   [ERROR] Missing backend virtual environment: %SVC_PY%
  echo       cd knowledge-service
  echo       uv venv --python 3.11 .venv
  echo       uv pip install --python .venv -e ".[dev,fastembed]"
  exit /b 1
)
if not exist "%WEB_PY%" (
  echo   [ERROR] Missing Web virtual environment: %WEB_PY%
  echo       cd knowledge-web
  echo       uv venv --python 3.11 .venv
  echo       uv pip install --python .venv -e ".[dev,prod]"
  exit /b 1
)
"%SVC_PY%" -c "import importlib.util as u; assert all(u.find_spec(x) for x in ('docling','unstructured','marker'))" >nul 2>&1
if errorlevel 1 (
  echo   [ERROR] Docling, Unstructured, or Marker is missing.
  echo       cd knowledge-service
  echo       uv pip install --python .venv -e ".[dev,fastembed]"
  exit /b 1
)
if /i "%KB_DENSE_PROVIDER%"=="fastembed" if /i "%KB_DENSE_MODEL%"=="BAAI/bge-small-zh-v1.5" if not exist "%MODEL_ONNX%" (
  echo   [ERROR] Missing embedding weights: %MODEL_ONNX%
  echo       Download the model or set KB_DENSE_PROVIDER=hash temporarily.
  exit /b 1
)
exit /b 0

:ensure_database
if exist "%KB_DATA_DIR%\kbsvc.db" exit /b 0
echo   Initializing SQLite and embedded Qdrant ...
pushd "%SVC_DIR%"
"%SVC_PY%" -m kbsvc.cli init
set "INIT_RESULT=!errorlevel!"
popd
exit /b !INIT_RESULT!

:stop_apps
call :kill_port %API_PORT%
call :kill_port %WEB_PORT%
taskkill /F /T /FI "WINDOWTITLE eq kbsvc %API_PORT%*" >nul 2>&1
taskkill /F /T /FI "WINDOWTITLE eq kbweb %WEB_PORT%*" >nul 2>&1
exit /b 0

:worker_running
curl -sf --max-time 3 "%API_URL%/healthz" 2>nul | findstr /I /C:"worker" | findstr /I /C:"running" >nul 2>&1
exit /b %errorlevel%

:port_busy
netstat -ano 2>nul | findstr /r /c:":%~1 .*LISTENING" >nul 2>&1
exit /b %errorlevel%

:kill_port
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /r /c:":%~1 .*LISTENING"') do taskkill /F /T /PID %%P >nul 2>&1
exit /b 0

:wait_http
set /a _tries=0
:wait_http_loop
curl -sf -o nul --max-time 5 "%~1" >nul 2>&1
if not errorlevel 1 exit /b 0
set /a _tries+=1
if !_tries! GEQ %~2 exit /b 1
ping -n 3 127.0.0.1 >nul
goto wait_http_loop

:wait_worker
set /a _worker_tries=0
:wait_worker_loop
call :worker_running
if not errorlevel 1 exit /b 0
set /a _worker_tries+=1
if !_worker_tries! GEQ %~1 exit /b 1
ping -n 2 127.0.0.1 >nul
goto wait_worker_loop

:print_stats
set "STATS_PY=%TEMP%\kb_stats_%RANDOM%.py"
echo import json,sys>"%STATS_PY%"
echo d=json.load(sys.stdin)>>"%STATS_PY%"
echo print('   index: %%d docs / %%d chunks / %%d vectors' %% (d['documents'],d['chunks'],d['vector_points']))>>"%STATS_PY%"
curl -s --max-time 10 "%API_URL%/v1/stats" 2>nul | "%SVC_PY%" "%STATS_PY%" 2>nul
del "%STATS_PY%" >nul 2>&1
exit /b 0

:usage
echo.
echo   Usage:
echo     run.bat             Start API with embedded worker, plus Web
echo     run.bat stop        Stop the complete local stack
echo     run.bat status      Show API, embedded worker, and Web status
echo     run.bat restart     Restart the complete local stack
echo     run.bat worker      Compatibility alias for restart
echo     run.bat reembed     Rebuild embedded Qdrant from SQLite chunks
echo.
goto end

:fail
echo.
endlocal
exit /b 1

:end
endlocal
exit /b 0
