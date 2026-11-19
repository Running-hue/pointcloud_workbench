# 多模态毫米点云生成

把已有 iToF 红外分割、原始深度码、逐帧标定和 RGB 联合整理成**以原始深度相机为原点、单位毫米的可见表面点云**。点的坐标保持实际尺度，不居中、不对每头猪归一化。每次采集独立输出。

这里的“生成”是有标定约束的测量重建，没有新增或训练生成模型，没有补全遮挡身体。当前接入的分割数据集是 `outputs/itof_SAM2猪体分割`；不合并不同时间、不同姿态或不同相机的数据。

## 浏览器工作台（推荐）

macOS 双击 **启动点云工作台.command**（原来的 **打开点云预览.command** 也会进入新工作台）；Windows 双击 **启动点云工作台.bat**。程序启动本机服务并打开浏览器，使用期间保留终端窗口。默认地址是 `http://127.0.0.1:8769`。

1. 点击顶部“目录设置”（或路径摘要），在弹窗中选择**原始采集目录**，即包含 `pig_编号/采集时间/` 的根目录。
2. 选择**分割结果目录**，即包含 `dataset.json` 的分割成果根目录。点击“核对并载入目录”，页面显示匹配、确认和缺失文件情况。输入搬到其他磁盘或 Windows 后，按相对目录重新匹配，不依赖旧的绝对路径。
3. 选择**导出目录**，填写新的成果文件夹名。
4. 勾选采集，点击“生成并按文件夹导出”。默认只生成已确认分割；试跑初稿需明确勾选“允许初稿预览”。页面显示逐项进度、跳过及失败原因，可在完成当前采集后停止。
5. 对已有点云，点击“导出点云”或“导出当前采集”，可选择当前、已勾选或全部已生成采集；导出前显示目标路径和目录树，也可点击“更改目录或成果名”。

顶部只保留操作按钮和当前路径摘要，把空间留给点云。右侧在一屏内显示点数、有效率、毫米坐标范围和检查图，详细来源和提醒点击“复核提醒”查看；左侧采集列表独立滚动。服务重启后，目录选择等请求会自动更新会话并重试一次；目录或数据集已改变时仍会要求重新载入，避免使用旧采集状态生成。

所有目录按钮（包括“打开已有点云目录”）都通过本机服务打开操作系统原生目录窗口：macOS 使用 NSOpenPanel，Windows 使用 IFileOpenDialog 目录模式。取消选择会保留原路径。也可直接填写路径。换电脑后需在该电脑启动服务，窗口会出现在运行服务的电脑上。

导出是把完整成果写入选定的本地文件夹，保留 `pig_编号/采集时间/` 层级，不需要逐个下载文件。每次采集的 PLY、NPZ、检查图、JSON 和预览页一起保存。目标同名目录已存在时要求换名，避免覆盖。正式分割和原始采集保持只读。完成后可点击“在系统中打开成果文件夹”。

Windows 首次运行前安装 Python 3，并在科研目录执行：

```sh
py -3 -m pip install -r pointcloud_workbench/requirements.txt
```

也可手动启动：

```sh
python3 pointcloud_workbench/app.py --open
```

## 已有预览

首次启动工作台会自动载入已有示例，也可点击“打开已有点云目录”选择其他结果。导出目录中的 `index.html` 和逐帧 `preview.html` 可直接打开离线查看。

首批示例包含耳标 1000 的三次采集，以及 1514、5905 各一次，共五次。结果写在 `outputs/itof_多模态毫米点云预览`。这些掩码在运行时全部为 `reviewed=false`，所有结果均明确标为“未审核初稿 · 流程预览”，没有修改人工复核状态。

页面支持旋转、缩放、正视、侧视，切换 RGB／原始红外强度／深度／RGB 可用性配色。浏览器显示至多 24000 个固定随机种子均匀抽取的预览点，完整导出包含掩码内全部可反投影的有效点。显示为便于旋转使用中心视图，导出数据仍保留相机原点。

## 正式生成

先在 SAM 分割工作台修正边界，记录头侧方向，点击“确认合格并保存”。然后双击 **生成已确认点云.command**，默认只读取 `status=reviewed` 且 `reviewed=true` 对应的 `trunk_mask.png`。

也可从科研工作区执行（下述 `python3` 需具有 NumPy、Pillow、OpenCV）：

```sh
python3 pointcloud_workbench/batch.py \
  --segmentation outputs/itof_SAM2猪体分割 \
  --output outputs/itof_多模态毫米点云
```

当前本机启动脚本自动优先使用 Codex 自带 Python，OpenCV 沿用 `segmentation_workbench/.deps`。换电脑时可安装本目录 `requirements.txt`。

当前没有已确认帧时，正式命令会如实报告跳过数量，生成空的结果索引，不把初稿自动当成合格结果。该步骤不要求先审核完全部图片，确认一张即可生成一张。

只想试跑未审核初稿时，须显式添加 `--allow-draft`，并使用独立预览目录：

```sh
python3 pointcloud_workbench/batch.py \
  --segmentation outputs/itof_SAM2猪体分割 \
  --output outputs/itof_多模态毫米点云预览 \
  --allow-draft --pig 1000
```

`--pig` 与 `--capture` 可重复指定；`--limit` 限制匹配后的采集数量。已有且输入一致的结果会复用。分割修订或原始文件改变时拒绝复用，需指定新的输出目录，以免覆盖先前结果。`summary.json` 是本次运行的统计；`index.html` 会列出该目录内的数据集已生成结果。

## 模态与坐标

### RGB 分割输入（含 Gemini 336L 双目）

工作台也支持当前分割工作台生成的 `input_mode=rgb`、`rgb-aligned-depth-v1` 成果。
程序核对掩码对应的几何标识、源文件校验值、所选帧身份和实际对齐输出 profile，
使用 `depth_aligned_to_color_code_u16.npy` 及其毫米换算系数反投影，
而不是将 1280×720 的 RGB 掩码套在 848×480 的原始深度上或缩放掩码。

RGB 图会按分割时相同的规则映射到保存的对齐深度网格，配色与掩码使用同一网格。
先得到彩色相机坐标，再应用记录的 `depth_to_color` 的逆变换一次，
因此最终 XYZ 仍以原始深度相机为原点、单位毫米。不会再次将已对齐深度执行正向对齐。
`pointcloud.json` 记录变换矩阵和像素坐标系；RGB 路径的 `pixel_uv` 是对齐彩色网格坐标，
`depth_code` 是该网格的彩色相机轴向深度码，不能将其直接解释为导出 XYZ 的 Z。

旧 RGB 分割若没有通过几何核对，程序会明确要求新建 RGB 分割任务。
初稿预览只允许未审核掩码，不会绕过文件、标定、深度单位或几何一致性检查。

本 RGB 路径尚未关联原始红外强度，因此 NPZ 的 `infrared_raw` 为 NaN、
`infrared_valid` 为 false；预览禁用红外配色。为兼容完整文件夹导出，仍保存灰色占位的
`cloud_ir_mm.ply`，其灰色不能解释为测得的红外值。RGB PLY 和 NPZ 保存完整有效几何。

以下表格介绍原生红外分割路径：

|来源|用途|
|---|---|
|`trunk_mask.png` 或显式允许的初稿|选择红外原始像素坐标中的目标区域|
|`depth_sensor_code_u16.npy`|按对应帧 `value_scale_mm_per_code` 还原毫米轴向深度|
|`reconstruction.json` 的实际深度 profile|使用内参及 Brown rational K6 畸变求逆射线；`XYZ=(x_ray Z,y_ray Z,Z)`|
|`infrared.npy`|按原始像素关联 IR 属性；没有称为回波置信度|
|`color.png`、实际彩色 profile、深度到彩色外参|投影到原始 RGB 图配色，保留独立的配色有效标记|

导出的深度相机坐标：X 向右，Y 向下，Z 向前；单位都是 mm。RGB 配色时把点临时变换到彩色相机坐标，不改变导出的 XYZ。该流程使用原始深度，不再对已对齐深度重复应用外参。

原始 Femto Bolt 深度存在畸变，因此没有直接把原始像素套入无畸变针孔公式。使用 OpenCV 迭代求逆，并以独立的 Brown K6 正向投影检查射线，重投影误差超过 0.05 像素时不输出该点。检查收敛仅验证数学一致性，不能替代已知尺寸实物的测量精度验证。

程序检查 IR 与深度的内参、畸变、到彩色相机的外参，以及设备帧号和时间戳；这些记录一致后才允许直接套用掩码。实际图像边界配准仍应查看检查图。当前仅支持记录明确且没有镜像／翻转的图像和 Brown／无畸变模型，未知模型会明确报错。

RGB 与深度默认允许的设备时间戳绝对差为 10000 μs，超过时保留几何，所有 RGB 标为缺失。可用 `--max-rgb-delta-us` 更改阈值。空间可见性使用全场景深度投影的 z-buffer 和 3×3 近邻最小深度，默认 20 mm 容差（`--visibility-mm`）；这是稀疏采样下的可见性近似，不能保证识别仅彩色相机看见的遮挡物，也不能弥补猪体运动造成的不同步。

样本记录的深度到彩色矩阵有轻微非正交误差，程序按源记录原样使用并报告，不擅自修正标定。其影响主要需要检查 RGB 边界；原生深度相机 XYZ 没有使用这个矩阵。最终配准质量需结合实际图像验证。

## 输出

```text
输出目录/
├── index.html
├── pointcloud_dataset.json       # 浏览器生成/导出的采集清单
├── export.json                   # 浏览器任务结果；CLI 使用 summary.json
└── pig_编号/采集时间/
    ├── cloud_rgb_mm.ply           # 全密度 XYZ + RGB；缺失配色显示灰色
    ├── cloud_ir_mm.ply            # 同一份 XYZ，以 IR 强度配色
    ├── points.npz                # 完整点属性，无 pickle
    ├── pointcloud.json            # 坐标、单位、标定、来源校验值、质量记录
    ├── mask_depth_check.png       # 绿色为掩码，红色为无有效点的掩码像素
    ├── rgb_projection_check.png   # 绿色标出目标点映射到原始 RGB 的位置
    └── preview.html              # 离线交互预览，不需要外部网络
```

NPZ 数组按行一一对应：`xyz_mm[N,3]`、`pixel_uv[N,2]`（u 列、v 行）、`depth_code[N]`、`infrared_raw[N]`、`rgb[N,3]`、`rgb_valid[N]`、`rgb_uv[N,2]`、`depth_discontinuity[N]`。其中 `rgb_uv` 越界／缺失可以是 NaN，应结合 `rgb_valid` 使用。

PLY 为二进制 little-endian，坐标是 float32 毫米，还保存 u/v、IR 值及 RGB 有效标记。很多查看软件只显示 XYZ/RGB，需要在软件中按毫米解释尺度；用 NPZ 可保留全部属性。

掩码内零深度与求逆不收敛的射线会被排除。没有平滑、补洞、自动裁掉边缘、最远点采样或按每只猪缩放。4 邻域出现超过 50 mm 深度差的点仅标记为跳变候选，供后续检查，不直接认定为飞点删除。

质量统计中的有效率分母是原始掩码像素数量。X/Y/Z 轴向跨度是当前姿态的包围盒跨度，不能作为完整体长、体宽、体高或真实体积。单视角只保留可见表面。

源图像和分割目录只读。程序核对分割阶段的源文件 SHA-256，检查原始深度码与分割使用的毫米数组一致，处理前后再次核对读取文件、掩码及分割 JSON。点云输出与源数据／分割目录不得相同或互相包含。

## 验证

```sh
python3 -m unittest discover -s pointcloud_workbench/tests -v
```

测试涵盖真实尺度、非整数毫米换算、强畸变求逆、独立正向重投影、RGB 遮挡和越界、外参只应用一次、分割确认门槛、不同步 RGB 保留几何、源数据变更拒绝、二进制 PLY／NPZ 与路径保护。还需以你的实测标定物验证实际测量误差。

公式与设备行为参考：Orbbec 官方 `src/shared/utils/CoordinateUtil.cpp`、`docs/tutorial/orbbec_camera_distortion.md`，以及 OpenCV `calib3d` 文档。来源地址记录如下，供复核：

```text
https://github.com/orbbec/OrbbecSDK_v2/blob/main/src/shared/utils/CoordinateUtil.cpp
https://github.com/orbbec/OrbbecSDK_v2/blob/main/docs/tutorial/orbbec_camera_distortion.md
https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html
```
