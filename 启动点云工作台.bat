@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  set "TASK_PYTHON=py -3"
) else (
  set "TASK_PYTHON=python"
)
%TASK_PYTHON% app.py --open
if errorlevel 1 (
  echo 启动失败，请查看上方具体原因。缺少依赖时可运行：
  echo %TASK_PYTHON% -m pip install -r requirements.txt
  pause
)
