#!/bin/zsh
cd "$(dirname "$0")/.." || exit 1
task_python="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$task_python" ]]; then
  task_python="$(command -v python3)"
fi
"$task_python" pointcloud_workbench/batch.py --segmentation outputs/itof_SAM2猪体分割 --output outputs/itof_多模态毫米点云
task_result=$?
if [[ $task_result -eq 0 ]]; then
  open 'outputs/itof_多模态毫米点云/index.html'
else
  echo '生成遇到问题，请检查上方信息与输出目录的 summary.json。'
fi
read -k 1 '?按任意键关闭'
exit $task_result
