# 多模态毫米点云工作台

读取 RGB/IR 分割和逐帧相机标定，生成实际毫米尺度的可见表面点云。工作流程为 **点云清理 → 坐标对齐 → 测量复核**，支持三个阶段分别筛选、分别或合并导出；前序淘汰停止后序，未完成后序不影响前序成果。

“测量复核”可直接计算几何向量和固定剖面序列：沿拟合可见中心线测量局部法向宽度、高度与形状，新增可见横剖面弧长，共 10 个通道，保留缺失及人工复核掩码；支持 JSON/CSV/NPZ 和批量几何表导出。桌面复核面板加宽并将参数/整体特征与曲线/剖面并列显示。它提供体重估测的候选输入，尚不输出预测体重。

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

Windows 的命令行入口也可使用 `python app.py --open`，统一检查使用 `python scripts/check_project.py`；首次安装依赖使用 `python -m pip install -r requirements.txt`。根目录和 `scripts/` 下的 bat 均支持转发 `--port`、`--state` 等参数，兼容中文及带空格的项目路径。

Windows 启动时会核对服务版本：同一项目和状态目录的同版本服务直接复用，代码更新后仅重启空闲服务；任务或请求尚未完成时会保留原服务并显示原因。本项目包含独立的几何校验模块，运行不需要安装同级分割程序，也不会优先载入从 macOS 复制的 `.deps` 二进制依赖。

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
| [几何特征与剖面序列](docs/features.md) | 计算入口、G/T 字段、中心线与切片定义、缺失掩码、批量导出及限制 |
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

“保留／待定／淘汰”针对当前页签阶段，三个结论独立保存；逐片测量复核另行记录。导出窗口和 `workflow_stages.json` 明确各阶段结果，`current_pointcloud.json` 明确本次主文件、坐标及测量状态，具体约定见 [架构与数据约定](docs/architecture.md)。
