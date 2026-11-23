@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
where py >nul 2>nul
if not errorlevel 1 (
  set "TASK_PYTHON=py -3"
) else (
  set "TASK_PYTHON=python"
)
%TASK_PYTHON% -c "import json,urllib.request,webbrowser; d=json.load(urllib.request.urlopen('http://127.0.0.1:8769/api/state',timeout=2)); assert 'native_picker' in d; webbrowser.open('http://127.0.0.1:8769')" >nul 2>nul
if not errorlevel 1 exit /b 0
%TASK_PYTHON% pointcloud_workbench\app.py --open
if errorlevel 1 (
  echo 启动失败。请先安装 Python 3，再运行：
  echo %TASK_PYTHON% -m pip install -r pointcloud_workbench\requirements.txt
  pause
)
