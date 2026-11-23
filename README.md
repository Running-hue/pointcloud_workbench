# 多模态毫米点云工作台

读取 RGB/IR 分割和逐帧相机标定，生成实际毫米尺度的可见表面点云。工作流程为 **点云清理 → 坐标对齐 → 测量复核**，支持采集筛选和当前处理版本导出。

## 启动

- macOS：双击根目录 **启动点云工作台.command**；原 **打开点云预览.command** 也可继续使用。
- Windows：双击根目录 **启动点云工作台.bat**。
- 使用期间保留服务终端，默认浏览器地址为 `http://127.0.0.1:8769`。

已安装依赖时，从项目根目录也可运行：

```sh
python3 app.py --open
```

首次配置环境时，在项目根目录安装：

```sh
python3 -m pip install -r requirements.txt
```

原来在科研工作区运行的 `python3 pointcloud_workbench/app.py --open` 和 `python3 pointcloud_workbench/batch.py ...` 保持有效。根目录 `app.py`、`batch.py` 及启动脚本负责转发，实现统一放在分类后的目录中。

## 文件分类

```text
backend/              Python 后端、点云重建、清理、对齐、测量和导出
frontend/pages/       主工作台、对齐和处理页面
frontend/assets/js/  页面 JavaScript
frontend/assets/css/ 页面样式
frontend/templates/  离线点云预览模板
scripts/              启动脚本实现、统一检查工具
tests/                Python 和前端测试
docs/                 使用说明、架构、目录表、开发说明和变更日志
.state/               原位置保留的本机设置与处理缓存（Git 忽略）
```

修改业务代码请进入 `backend/`，修改页面请进入 `frontend/`，修改启动脚本请进入 `scripts/`。根目录保留少量旧入口，便于继续使用已有快捷方式和命令。

## 文档

| 文档 | 内容 |
| --- | --- |
| [文档索引](docs/README.md) | 全部文档和维护约定 |
| [使用手册](docs/user-guide.md) | 原详细 README 内容：载入、生成、清理、对齐、复核及导出 |
| [目录与文件职责](docs/project-structure.md) | 每类文件放在哪里、逐项迁移表、入口兼容和数据位置 |
| [架构与数据约定](docs/architecture.md) | 模块关系、采集筛选、测量状态和主点云文件 |
| [开发与验证](docs/development.md) | 环境、启动、测试及每次改动的记录方法 |
| [变更日志](docs/CHANGELOG.md) | 当前及历史迭代、兼容说明、验证结果 |

## 检查

从项目根目录执行统一检查：

```sh
python3 scripts/check_project.py
```

该命令使用当前 Python 运行测试，再调用本机 Node.js 检查前端。单独检查方式和浏览器验收说明见 [开发与验证](docs/development.md)。

## 数据与已有记录

原始采集、分割和点云成果继续使用工作台中选择的目录；本次整理未将数据搬入代码目录。`.state` 中的目录设置、采集筛选、清理历史和对齐缓存保留原位置，默认导出父目录仍是科研工作区的 `outputs/`。

“保留”表示继续采用当前采集版本，测量是否已复核另行记录。导出窗口及每条采集的 `current_pointcloud.json` 明确当前主文件、坐标和测量状态，具体约定见 [架构与数据约定](docs/architecture.md)。
