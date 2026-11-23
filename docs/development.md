# 开发与验证

## 环境和启动

运行依赖列在项目根 `requirements.txt`。前端使用原生 HTML/CSS/JavaScript，由 Python 服务直接加载。

从项目根目录执行：

```sh
python3 -m pip install -r requirements.txt
python3 app.py --open
```

从项目所在的科研工作区也可继续执行原命令：

```sh
python3 pointcloud_workbench/app.py --open
python3 -m pointcloud_workbench.app --open
python3 pointcloud_workbench/batch.py --help
```

根目录和 `scripts/` 中的启动脚本均可使用；根脚本仅转发，维护脚本逻辑时修改 `scripts/` 中的实现。macOS 脚本沿用原有 Python 选择顺序：项目 `.venv`、本机 Codex Python、系统 `python3`。Windows 脚本沿用 `py -3` 或 `python`。

自定义端口和独立状态目录适合开发验收：

```sh
python3 app.py --port 8770 --state /tmp/pointcloud-workbench-check
```

带写操作的验收使用临时合成数据和独立状态目录。真实采集只做只读检查，避免测试写入用户正在使用的设置和清理记录。

## 统一检查入口

从项目根目录执行（`python3` 应已安装项目依赖）：

```sh
python3 scripts/check_project.py
```

统一入口按顺序运行 Python 测试、三个在线 JS 的语法检查和前端工作台测试；任一失败返回非零。可通过 `--backend-only` 或 `--frontend-only` 选择范围。前端检查使用本机 Node.js，Python 测试中部分预览测试也会调用 Node.js。

原有单独检查方式仍有效，从科研工作区执行：

```sh
python3 -m unittest discover -s pointcloud_workbench/tests -q
node --test pointcloud_workbench/tests/test_workbench.cjs
node --check pointcloud_workbench/frontend/assets/js/workbench.js
node --check pointcloud_workbench/frontend/assets/js/alignment.js
node --check pointcloud_workbench/frontend/assets/js/processing.js
```

提交前还应在项目仓库执行：

```sh
git diff --check
```

本机默认测试包含短时监听回环端口的 HTTP 测试，需要允许临时本机监听。不会访问外部网络，也不会自动安装依赖。

## 按修改范围验证

| 修改范围 | 相关检查 |
| --- | --- |
| 几何、RGB、配准 | `test_geometry.py`、`test_rgb.py`、`test_registration.py`、`test_modalities.py` |
| 载入、输入核对、任务、筛选、导出 | `test_workspace.py`、`test_input_validation.py` |
| 坐标参考与对齐 | `test_alignment.py`、`body_view_runtime.cjs` |
| 清理、撤销、切片和复核 | `test_processing.py` |
| 几何特征、弯体法向、缺失掩码、特征缓存与导出 | `test_features.py` |
| 主工作台事件和状态 | `test_workbench.cjs` |
| 离线相机点云预览 | `test_viewer.py`、`viewer_runtime.cjs` |
| 目录、入口、默认路径和资源路由 | `test_project_layout.py` |

`viewer_runtime.cjs` 与 `body_view_runtime.cjs` 由 Python 测试传入生成后的 HTML 调用，不是独立读取源码模板的测试命令。

移动文件后检查原 CLI 和导入路径、所有页面与静态资源、离线预览的资源内嵌、默认 `.state`/`outputs` 路径。改界面后还应在浏览器中检查三步切换、当前筛选高亮、导出预览和横向溢出。后端更新时，先确认本机工作台没有运行/停止中的任务，再重启服务；刷新页面会更新会话。

## 文档和变更日志

完成一次功能、修复或整理后：

1. 在 [CHANGELOG.md](CHANGELOG.md) 顶部新增当日条目，注明做了什么、影响了哪里、怎样验证、兼容性或数据迁移情况。
2. 用户操作变化时更新 [使用手册](user-guide.md)；目录变化时更新 [目录与文件职责](project-structure.md)；状态或导出约定变化时更新 [架构与数据约定](architecture.md)。
3. 日志只记录实际完成和验证的内容；没有单独 Git 提交或确定版本号时如实注明，不补造发布版本或时间。

新增条目可沿用以下格式：

```markdown
## YYYY-MM-DD — 改动名称

- 改动：具体行为或目录变化。
- 影响：涉及的页面、模块或输出文件。
- 兼容：旧入口、已有状态或数据格式是否受影响。
- 验证：实际执行的检查和结果。
```
