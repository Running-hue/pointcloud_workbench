# 目录与文件职责

## 目录总览

```text
pointcloud_workbench/
├── README.md                  # 启动说明与文档导航
├── requirements.txt           # Python 运行依赖
├── __init__.py                # 保留 pointcloud_workbench.* 的导入路径
├── app.py                     # 原工作台命令行入口，转入 backend/app.py
├── batch.py                   # 原批处理命令行入口，转入 backend/batch.py
├── 启动点云工作台.command      # macOS 入口，转入 scripts/
├── 启动点云工作台.bat          # Windows 入口，转入 scripts/
├── 打开点云预览.command        # 保留原有快捷入口，转入 scripts/
├── 生成已确认点云.command      # 保留原有批处理入口，转入 scripts/
├── backend/                   # Python 服务、几何重建及工作流实现
├── frontend/
│   ├── pages/                 # 由本机服务打开的在线页面
│   ├── assets/
│   │   ├── js/                # 在线页面脚本
│   │   └── css/               # 在线页面样式
│   └── templates/             # 写入新成果的离线预览模板
├── scripts/                   # 启动脚本实现和统一检查工具
├── tests/                     # Python、前端事件与预览运行测试
├── docs/                      # 手册、结构、架构、开发说明、变更日志
└── .state/                    # 原位置保留的本机设置与处理缓存，Git 忽略
```

根目录保留的入口文件仅负责转发，业务改动应在分类后的目录中进行。

## 后端

| 文件 | 职责 |
| --- | --- |
| `backend/app.py` | 本机 HTTP 服务、页面与资源路由、请求鉴权、API 转发、服务启动 |
| `backend/workspace.py` | 数据集载入、输入核对、采集筛选、后台生成任务、批量导出 |
| `backend/features.py` | 可见中心线、几何特征、局部法向序列、质量掩码与特征序列化 |
| `backend/core.py` | 毫米几何、投影/反投影、输入校验、基础重建和点云文件输出 |
| `backend/rgb.py` | RGB 分割对应的几何重建 |
| `backend/registration.py` | RGB 与深度的配准核对、遮挡和时序检查 |
| `backend/alignment.py` | 地面拟合、端点标注、猪体坐标、同次采集参考及对齐成果 |
| `backend/processing.py` | 原始点索引上的删除/恢复、撤销/重做、切片测量和复核 |
| `backend/viewer.py` | 相机坐标离线预览的显示抽样和 HTML 生成 |
| `backend/native_picker.py` | macOS/Windows 原生目录选择窗口及辅助进程 |
| `backend/batch.py` | 已有命令行批量重建流程 |
| `backend/paths.py` | 项目、前端资源、默认状态、输出和共享依赖路径 |

`backend` 是 Python 实现文件的物理位置。根 `__init__.py` 将它加入现有包的搜索路径，因此继续使用 `pointcloud_workbench.core`、`pointcloud_workbench.workspace` 等原导入名。现有科研脚本和测试中的模块补丁路径也可继续使用。开发时保持这一套导入名，避免再以 `pointcloud_workbench.backend.*` 加载同一份模块。

## 前端

| 文件 | 对应功能 |
| --- | --- |
| `frontend/pages/index.html`、`frontend/assets/js/workbench.js`、`frontend/assets/css/workbench.css` | 主工作台、采集列表、三步切换、采集筛选及导出弹窗 |
| `frontend/pages/alignment.html`、`frontend/assets/js/alignment.js`、`frontend/assets/css/alignment.css` | 地面/端点标注及坐标对齐 |
| `frontend/pages/processing.html`、`frontend/assets/js/processing.js`、`frontend/assets/css/processing.css` | 点云查看与清理、测量复核，以及导出的只读处理预览 |
| `frontend/templates/viewer.html` | 原始相机坐标离线预览模板 |
| `frontend/templates/body_view.html` | 猪体坐标与对齐预览模板 |

页面地址仍是 `/`、`/alignment`、`/processing`；脚本和样式地址仍是 `/workbench.js`、`/alignment.css` 等原地址。磁盘文件分类不改变浏览器路由。`frontend/templates` 中的模板由 Python 注入数据后写入新成果，不能直接当成已导出的预览打开。

## 原位置与新位置

| 整理前 | 整理后 |
| --- | --- |
| 根目录的 `core.py`、`rgb.py`、`registration.py`、`viewer.py`、`workspace.py`、`alignment.py`、`processing.py`、`native_picker.py` | `backend/` 下同名文件 |
| 根目录的 `app.py`、`batch.py` 实现 | `backend/` 下同名实现；根文件保留为入口 |
| 根目录的 `index.html`、`alignment.html`、`processing.html` | `frontend/pages/` |
| 根目录的三个 `.js` 文件 | `frontend/assets/js/` |
| 根目录的三个 `.css` 文件 | `frontend/assets/css/` |
| 根目录的 `viewer.html`、`body_view.html` | `frontend/templates/` |
| 根目录的 `.command`/`.bat` 实现 | `scripts/`；根目录同名文件保留为快捷入口 |
| 原详细 `README.md` | `docs/user-guide.md`；根 README 改为简明入口 |
| 原 `tests/` | 保留原位置；只更新前端测试读取的资源路径并补充迁移兼容测试 |

## 运行文件和数据放哪里

| 内容 | 位置及约定 |
| --- | --- |
| 本机目录设置、采集筛选、清理历史、对齐缓存 | 原项目根目录 `.state/`，不搬迁、不纳入 Git |
| 默认导出父目录 | 原科研工作区 `outputs/`，仍是项目的同级目录 |
| 原始采集、分割结果、用户选择的点云成果 | 继续使用工作台中选择的外部目录，不复制进源码目录 |
| 已有 `.venv` | 原项目根目录 `.venv/`，启动脚本仍优先使用 |
| 共享 OpenCV 等依赖 | 原同级 `segmentation_workbench/.deps/`，路径保持不变 |
| Python 缓存、系统目录信息 | `__pycache__/`、`.DS_Store`，由 `.gitignore` 忽略 |

新增业务模块放入 `backend`，新增页面/脚本/样式放入 `frontend` 对应子目录，维护脚本放入 `scripts`，文档与日志放入 `docs`。运行产物不放进这些源码目录。
