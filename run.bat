@echo off
rem ============================================================================
rem  kbsvc + kbweb 一键启动
rem
rem    run.bat            启动后端与前端，预热索引，打开浏览器
rem    run.bat stop       停掉两个服务，释放嵌入式 Qdrant 的独占锁
rem    run.bat status     只看当前状态，不启动
rem    run.bat worker     停掉后端，排空导入队列，再把后端起回来
rem
rem  注意：嵌入式 Qdrant 对数据目录持独占锁，同一时刻只能有一个进程持有。
rem  所以 worker 不能和 API 同时跑，用 run.bat worker 来处理导入队列。
rem
rem  本文件必须保存为 GBK 编码：cmd.exe 用系统代码页逐字节解析 .bat，
rem  存成 UTF-8 会让中文被切碎并破坏语法。
rem ============================================================================

setlocal EnableDelayedExpansion

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

rem --- 后端环境 ---
set "KB_DATA_DIR=%SVC_DIR%\.kbdata"
set "KB_DENSE_PROVIDER=fastembed"
set "KB_DENSE_MODEL=BAAI/bge-small-zh-v1.5"
set "PYTHONIOENCODING=utf-8"

rem --- 前端环境 ---
set "KBWEB_API_BASE=%API_URL%"
set "KBWEB_SECRET_KEY=local-manual-testing"
set "KBWEB_DEBUG_UI=true"
set "KBWEB_TIMEOUT=60"

set "LOG_DIR=%ROOT%\logs"
set "SVC_LOG=%LOG_DIR%\kbsvc.log"
set "WEB_LOG=%LOG_DIR%\kbweb.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

set "MODEL_DIR=%KB_DATA_DIR%\models\fast-bge-small-zh-v1.5"
set "MODEL_ONNX=%MODEL_DIR%\model_optimized.onnx"

if /i "%~1"=="stop"   goto do_stop
if /i "%~1"=="status" goto do_status
if /i "%~1"=="worker" goto do_worker
if not "%~1"=="" goto usage

rem ============================================================================
rem  启动
rem ============================================================================
echo.
echo   kbsvc + kbweb
echo   ------------------------------------------------------------------

call :check_env
if errorlevel 1 goto fail

rem 端口被占 = 上次没停干净。放行的话新进程会抢不到 Qdrant 锁而静默失败。
call :port_busy %API_PORT%
if not errorlevel 1 (
  echo   [!] 端口 %API_PORT% 已被占用，请先执行:  run.bat stop
  goto fail
)
call :port_busy %WEB_PORT%
if not errorlevel 1 (
  echo   [!] 端口 %WEB_PORT% 已被占用，请先执行:  run.bat stop
  goto fail
)

echo   [1/4] 启动后端 kbsvc  %API_URL%
pushd "%SVC_DIR%"
start "kbsvc %API_PORT%" /min cmd /c ""%SVC_PY%" -m kbsvc.cli serve --host %API_HOST% --port %API_PORT% > "%SVC_LOG%" 2>&1"
popd

call :wait_http "%API_URL%/healthz" 60
if errorlevel 1 (
  echo   [!] 后端未在 120 秒内就绪，日志: %SVC_LOG%
  goto fail
)
echo         就绪

echo   [2/4] 启动前端 kbweb  %WEB_URL%
pushd "%WEB_DIR%"
start "kbweb %WEB_PORT%" /min cmd /c ""%WEB_PY%" -m waitress --host=127.0.0.1 --port=%WEB_PORT% wsgi:app > "%WEB_LOG%" 2>&1"
popd

call :wait_http "%WEB_URL%/" 30
if errorlevel 1 (
  echo   [!] 前端未就绪，日志: %WEB_LOG%
  goto fail
)
echo         就绪

rem 首次查询要加载 ONNX 模型并打开约 310MB 的嵌入式集合，约 8 秒。
rem 在这里先付掉，否则用户第一次点检索会以为卡死。
echo   [3/4] 预热索引，约 8 秒
curl -s -o nul --max-time 180 "%WEB_URL%/?f=1^&q=%%E8%%B4%%BC%%E5%%85%%8B^&rerank=1^&rewrite=1" >nul 2>&1
echo         完成

echo   [4/4] 打开浏览器
start "" "%WEB_URL%"

echo.
echo   ------------------------------------------------------------------
call :print_stats
echo.
echo    前端      %WEB_URL%
echo    API 文档  %API_URL%/docs
echo.
echo    停止服务          run.bat stop
echo    处理导入队列      run.bat worker
echo.
echo    提示: 混合检索约需 6 秒，稀疏检索在嵌入式 Qdrant 上是全量扫描。
echo          左栏切到"语义"单路只要约 0.4 秒。
echo   ------------------------------------------------------------------
echo.
goto end

rem ============================================================================
:do_stop
echo.
echo   停止 kbsvc / kbweb ...
call :kill_port %API_PORT%
call :kill_port %WEB_PORT%
rem 兜底：进程可能还没绑上端口就被要求停止
taskkill /F /FI "WINDOWTITLE eq kbsvc %API_PORT%*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq kbweb %WEB_PORT%*" >nul 2>&1
echo   已停止，Qdrant 锁已释放
echo.
goto end

rem ============================================================================
:do_status
echo.
call :port_busy %API_PORT%
if not errorlevel 1 (echo   后端 kbsvc   运行中   %API_URL%) else (echo   后端 kbsvc   未运行)
call :port_busy %WEB_PORT%
if not errorlevel 1 (echo   前端 kbweb   运行中   %WEB_URL%) else (echo   前端 kbweb   未运行)
call :port_busy %API_PORT%
if not errorlevel 1 call :print_stats
echo.
goto end

rem ============================================================================
:do_worker
echo.
echo   嵌入式 Qdrant 是独占锁，worker 必须独占数据目录，先停掉后端。
call :kill_port %API_PORT%
rem 给 Windows 一点时间真正释放 sqlite 句柄
timeout /t 3 /nobreak >nul

call :check_env
if errorlevel 1 goto fail

echo   排空导入队列 ...
pushd "%SVC_DIR%"
"%SVC_PY%" -m kbsvc.cli worker --once
popd

echo.
echo   队列已处理，重启后端 ...
pushd "%SVC_DIR%"
start "kbsvc %API_PORT%" /min cmd /c ""%SVC_PY%" -m kbsvc.cli serve --host %API_HOST% --port %API_PORT% > "%SVC_LOG%" 2>&1"
popd
call :wait_http "%API_URL%/healthz" 60
if errorlevel 1 (echo   [!] 后端未能恢复，执行 run.bat 重试) else (echo   后端已恢复)
echo.
goto end

rem ============================================================================
rem  子例程
rem ============================================================================

:check_env
if not exist "%SVC_PY%" (
  echo   [!] 找不到后端虚拟环境: %SVC_PY%
  echo       cd knowledge-service
  echo       uv venv --python 3.11 .venv
  echo       uv pip install --python .venv -e ".[dev,fastembed]"
  exit /b 1
)
if not exist "%WEB_PY%" (
  echo   [!] 找不到前端虚拟环境: %WEB_PY%
  echo       cd knowledge-web
  echo       uv venv --python 3.11 .venv
  echo       uv pip install --python .venv -e ".[dev,prod]"
  exit /b 1
)
if not exist "%KB_DATA_DIR%\kbsvc.db" (
  echo   [!] 索引不存在: %KB_DATA_DIR%
  echo       先建库并导入:
  echo         cd knowledge-service
  echo         .venv\Scripts\python.exe -m kbsvc.cli init
  echo         .venv\Scripts\python.exe -m kbsvc.cli ingest ..\book --source guji --patterns "*.txt,*.md"
  exit /b 1
)
if not exist "%MODEL_ONNX%" (
  rem 模型缺失时 fastembed 会去探 HuggingFace，在受限网络上会挂住几分钟而不是快速失败。
  echo   [!] 缺少语义模型权重:
  echo       %MODEL_ONNX%
  echo.
  echo       从 ModelScope 下载，比 HuggingFace 快很多:
  echo         curl -L -o "%MODEL_ONNX%" "https://modelscope.cn/api/v1/models/Maiteka/bge-small-zh-v1.5-onnx/repo?Revision=master&FilePath=model.onnx"
  echo.
  echo       或者先用无需模型的哈希嵌入跑起来，检索质量会下降:
  echo         set KB_DENSE_PROVIDER=hash
  exit /b 1
)
exit /b 0

:port_busy
netstat -ano 2>nul | findstr /r /c:":%~1 .*LISTENING" >nul 2>&1
exit /b %errorlevel%

:kill_port
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /r /c:":%~1 .*LISTENING"') do taskkill /F /PID %%P >nul 2>&1
exit /b 0

rem :wait_http <url> <最大尝试次数>  每次间隔 2 秒
:wait_http
set /a _tries=0
:wait_http_loop
curl -sf -o nul --max-time 5 "%~1" >nul 2>&1
if not errorlevel 1 exit /b 0
set /a _tries+=1
if !_tries! GEQ %~2 exit /b 1
timeout /t 2 /nobreak >nul
goto wait_http_loop

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
echo   用法:
echo     run.bat           启动前后端
echo     run.bat stop      停止
echo     run.bat status    查看状态
echo     run.bat worker    排空导入队列，会短暂重启后端
echo.
goto end

:fail
echo.
endlocal
exit /b 1

:end
endlocal
exit /b 0
