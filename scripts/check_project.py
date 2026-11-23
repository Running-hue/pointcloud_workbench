#!/usr/bin/env python3
"""Run the existing Python/frontend checks from any working directory."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--backend-only', action='store_true', help='只运行 Python 测试')
    scope.add_argument('--frontend-only', action='store_true', help='只运行 JS 语法检查和前端测试')
    args = parser.parse_args()
    commands = []
    if not args.frontend_only:
        commands.append([sys.executable, '-m', 'unittest', 'discover', '-s',
                         str(PROJECT_ROOT/'tests'), '-q'])
    if not args.backend_only:
        node = shutil.which('node')
        if not node:
            parser.error('前端检查需要 Node.js；也可用 --backend-only 只运行 Python 测试')
        commands.extend([node, '--check', str(script)] for script in sorted(
            (PROJECT_ROOT/'frontend'/'assets'/'js').glob('*.js')))
        commands.append([node, '--test', str(PROJECT_ROOT/'tests'/'test_workbench.cjs')])
    for command in commands:
        print('运行：'+' '.join(command), flush=True)
        result = subprocess.run(command, cwd=PROJECT_ROOT.parent, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
