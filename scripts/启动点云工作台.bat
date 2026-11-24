@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0.."
if errorlevel 1 exit /b 1
where py >nul 2>nul
if not errorlevel 1 (
  set "TASK_PYTHON=py -3"
) else (
  set "TASK_PYTHON=python"
)
%TASK_PYTHON% "%CD%\app.py" --open %*
set "TASK_EXIT_CODE=%errorlevel%"
if not "%TASK_EXIT_CODE%"=="0" (
  echo 启动失败，请查看上方具体原因。缺少依赖时可运行：
  echo %TASK_PYTHON% -m pip install -r "%CD%\requirements.txt"
  pause
)
popd
exit /b %TASK_EXIT_CODE%
