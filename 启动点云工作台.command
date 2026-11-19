#!/bin/zsh
cd "$(dirname "$0")/.." || exit 1
task_python="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$task_python" ]]; then
  task_python="$(command -v python3)"
fi
if [[ -z "$task_python" ]]; then
  echo '请先安装 Python 3，并安装 pointcloud_workbench/requirements.txt 中的依赖。'
  read -k 1 '?按任意键关闭'
  exit 1
fi
if curl --silent --fail --max-time 2 http://127.0.0.1:8769/api/state >/dev/null; then
  open http://127.0.0.1:8769
  exit 0
fi
"$task_python" pointcloud_workbench/app.py --open
task_result=$?
if [[ $task_result -ne 0 ]]; then
  echo '启动失败，请检查上方错误。换电脑时需安装 requirements.txt 中的依赖。'
  read -k 1 '?按任意键关闭'
fi
exit $task_result
