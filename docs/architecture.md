# 架构与数据约定

## 请求与处理链

```text
frontend/pages + frontend/assets
                │ 本机 HTTP / API
         backend/app.py
                │
         backend/workspace.py
          ├── core / rgb / registration：毫米点云重建与输入核对
          ├── alignment：地面与猪体坐标参考
          ├── processing：原始点索引编辑、切片与测量复核
          ├── features：可见几何、中心线法向剖面与掩码
          └── viewer + frontend/templates：离线预览
```

`Workspace` 管理当前数据集、采集身份、设置和后台任务。对齐与清理服务共享同一工作台锁和数据集标识，请求校验当前采集及版本，避免把目录切换前的响应写入另一批数据。`app.py` 只将允许的路由映射到页面、资源或服务方法。

`backend/geometry.py` 将既有分割坐标约定内置到工作台：保留几何身份、源文件哈希、标定、同步及镜像校验；`core`、`rgb`、`registration` 和 RGB 清理底图共用它，不依赖同级分割应用。

`backend/startup.py` 标识当前项目、状态目录与加载时的代码版本。Windows 启动脚本经统一 Python 入口检查实例；同版本复用，旧版本仅在没有生成／导出任务、存活 worker 或其他 POST 请求时才可退出。Windows 连接重置不代表端口可用，必须确认旧监听已关闭再启动新实例。

## 文件路径

`backend/paths.py` 从源码位置计算项目根目录及前端资源位置。默认 `.state` 仍位于项目根目录，默认 `outputs` 仍位于科研工作区。前端页面和导出模板不再假定与 Python 文件相邻。服务启动、资源读取及离线导出都不依赖进程的当前工作目录。

## 三步工作流

1. **点云清理**：查看点云、检测候选、框选并确认删除或恢复原始点索引，保留历史。
2. **坐标对齐**：确认地面、尾根、颈根与方向，保存毫米猪体坐标参考。同次采集可复用参考。
3. **测量复核**：使用当前点集及已确认的猪体坐标按区域或逐片检查可见边界，记录可测/不可测、备注和测量汇总。

源图像、分割文件、原始 `points.npz` 和原始 PLY 不改写。清理只改变保留的点索引，不平滑、不补洞、不移动点、不改变毫米尺度。界面为交互显示抽样；编辑、测量和导出使用完整点集。

## 阶段筛选与测量验收

- 清理／对齐／测量各自记录保留、待定、淘汰。`cloud_review.json` 沿用 schema_version=2，增加 `stages` 字典；每项含 decision、version、reviewed_at。旧未带 stages 的记录按其 processing_version 推断为清理或对齐阶段，不将一条旧结论复制到三项。
- 清理版本绑定点集摘要；对齐版本另外绑定坐标参考；测量版本另外绑定参数、条件与逐片复核摘要。下游变化不影响上游结论；相关输入改变后本阶段待定，撤销至相同版本可恢复。
- 前序有效淘汰会阻止后序操作和输出；修改前序结论后恢复。未完成后序不阻止上游导出。`workflow_state` 在状态、筛选、操作检查、导出计划中统一计算依赖。
- 逐片可测／不可测与阶段筛选分别记录；`measurement_ready` 不从阶段保留推断。原始生成有效率不因删点修改。
- 测量阶段显式 `review_cloud(stage=review, decision=keep)` 在校验数据集、前序依赖和提交版本后，将当前计算结果中剩余 `pending` 剖面写为 `unusable`，保留已有可测／不可测标记。使用分离处理上下文计算，在一次 `settings.json` 原子替换中同时发布新处理缓存引用和绑定最终逐片版本的阶段保留记录，失败不改变旧缓存、编辑上下文或阶段状态。成功后旧编辑上下文失效，前端以新的 `editor_instance` 重新载入并忽略旧页面迟到消息；响应附 `measurement_finalization.auto_rejected_profiles`。没有待定剖面时不生成新处理 revision。其他阶段、待定／淘汰及历史读取不触发自动处理；改变逐片标记会使原阶段保留失效。导出保持 K 行及不可测掩码，不裁剪点云或测量区间。
- 工作台“已采集完毕”由三阶段当前状态共同决定：每项均 `has_output=true`、`reviewed=true`、`decision=keep`、非 stale 且无 blocked_by，并且采集已有点云。左栏顶部只按相机显示 `完成猪头/总猪头` 与 `完成采集/总采集`；猪头按 `pig_id` 分组，全部对应采集完成才计数。单选 iToF／双目时显示该行，全部相机时分行显示，不合并两相机。搜索、分割来源和阶段筛选不改变统计分母；统计为前端派生展示，不新增状态文件或导出字段。
- 搜索、相机、分割来源或阶段筛选发生变化时，前端把采集勾选集合替换为当前 `visibleRows()` 的全部 ID，不保留上一筛选范围的隐藏勾选；筛选收起只影响可见性，不改变当前勾选。

## 完整合并导出的当前主点云

| 当前情况 | 主 PLY | 主 NPZ | 坐标系 |
| --- | --- | --- | --- |
| 无处理记录、未对齐 | 生成时的 `cloud_ir_mm.ply` 或 `cloud_rgb_mm.ply` | `points.npz` | 相机 |
| 有处理记录、未对齐 | `cloud_cleaned_mm.ply` | `cleaned_points.npz` | 相机 |
| 无处理记录、已对齐 | `cloud_body_mm.ply` | `body_points.npz` | 猪体 |
| 有处理记录、已对齐 | `cloud_cleaned_body_mm.ply` | `cleaned_body_points.npz` | 猪体 |

清理和复核共用处理记录；尚未删点但保存了复核记录时，处理后文件仍包含全部原始点。导出弹窗独立选择 stages、采集范围、各阶段 decisions。每条采集只输出符合状态且未被前序淘汰阻断的阶段；主文件由本次实际输出阶段决定。仅清理使用相机坐标，包含对齐／测量成果使用猪体坐标。测量仅在显式保存参数／条件／剖面判断后纳入自动导出，不因确认坐标或删点自动开始。

原始文件以及所选阶段计算所需的处理／对齐依赖作为追溯资料保存。未输出测量时，副本不含测量产品且清理记录的 measurement 清空；未输出对齐／测量时，副本移除对齐文件。全部操作发生在新副本，源处理与缓存不改写。部分导出不替换工作台完整对齐关联。读取下游特征时应先看主文件清单，不能把目录中的任意一个 PLY 当作最新版本。

| 清单 | 含义 |
| --- | --- |
| 每次采集的 `pointcloud.json` | 原始点云生成数据、标定、来源和原始统计 |
| 每次采集的 `current_pointcloud.json` | 当前主 PLY/NPZ、坐标、点数、处理版本及测量复核状态 |
| 每次采集的 `cloud_review.json` | 各阶段独立筛选记录及版本；decision 为兼容摘要 |
| 每次采集的 `workflow_stages.json` | 本次实际导出阶段、各阶段状态和文件路径 |
| 根目录的 `stage_screening.json/csv` | 每条采集各所选阶段的结论、阻断来源与导出路径 |
| 每次采集的 `pointcloud_cleaning.json`、`point_edits.npz` | 清理索引、历史和复核记录 |
| 每次采集的 `alignment.json` | 坐标参考及变换矩阵 |
| 每次采集的 `measurement_summary.json`、`profile_sequence.csv/npz` | 测量汇总、逐片候选与确认值 |
| 每次采集的 `geometry_features.json`、`profile_sequence.csv/npz` | 当前保留完整点集的几何候选、局部法向序列、单位与缺失/复核掩码 |
| 根目录的 `geometry_features.csv` | 批量几何表、相机/模态、输入版本、条件与质量统计；真实体重留空待关联 |
| 根目录的 `pointcloud_dataset.json` | 导出采集及各条 `primary_ply`、`primary_npz`、坐标等信息 |
| 根目录的 `cloud_screening.json`、`cloud_screening.csv` | 筛选范围、分类、导出结果及测量复核状态 |

具体几何定义、数据数组及操作步骤见 [使用手册](user-guide.md)。

新剖面通过 `/api/processing/features` 计算及保存参数与独立复核，身份、版本和保存回滚遵循原处理服务。参数存于 `measurement.feature_settings`，标记存于 `feature_reviews`，不复用 `slice_reviews`；`measurement_ready`、左侧确认片数和导出汇总统一由当前剖面结果计算，不读取旧直线切片确认。仅计算不写文件，显式保存才写处理缓存；导出用同模块重新计算，原始点索引和主点云选择规则不变。

端点与地面由当前已确认对齐默认提供 `complete/reliable`，计算与导出一致，来源记录为 `provenance.reference_conditions_source=confirmed_alignment_default`。历史 `feature_quality` 仅保留作追溯，读取不重写；旧请求的 `conditions` 仍验证格式，但不改变有效条件或重置剖面标记。删点、参数或坐标参考变化仍使对应剖面标记失效，默认参考不绕过数值缺失或中心线缺支撑检查。

测量记录以 `centerline_version` 绑定中心线方法。新曲线以尾根、颈根地面投影为硬约束，对整段可见包络种子一次完成三次平滑样条拟合，弧长、切向与剖面使用同一曲线；显示高度只作端点高度插值。存在历史剖面标记而没有方法字段的记录继续按旧模型计算，恢复和导出保持一致。显式重算参数或重标坐标才更新至新模型并重置本条剖面标记；保存的方法字段纳入测量阶段摘要，避免旧阶段确认套到新剖面。

批量复核在同一路由提交 `profile_range: [first,last]` 与 `status: usable/unusable/pending`，使用从 0 开始的闭区间。一次请求校验全部范围，合并范围内标记后原子保存一次，区间外标记不变；失败回滚，旧 revision 拒绝写入。批量 `usable` 跳过宽度不可算的剖面并保留其原状态，返回 `review_update.updated/skipped`；单片确认仍拒绝不可算剖面，批量淘汰／待定可覆盖缺失片。详细公式和掩码见 [特征说明](features.md)。

## 精简阶段交付包

导出请求使用 `export_layout: split/merged`，省略时兼容原有 merged 行为；界面默认 split。新 split 标为 `package_kind: stage_products`，按“阶段中文名／该阶段状态／原采集路径”写该阶段产品，直接由当前完整源点集和已保存版本生成，不经完整副本复制。白名单如下：

| 阶段 | 本阶段成果 |
| --- | --- |
| 点云清理 | `cloud_cleaned_mm.ply`、`cleaned_points.npz`、`pointcloud_cleaning.json` 轻量清理摘要；相机坐标，NPZ 含原始行索引 |
| 坐标对齐 | `cloud_body_mm.ply`、`body_points.npz`、`alignment.json`；当前保留点的猪体坐标，NPZ 含原始行索引 |
| 测量复核 | `geometry_features.json`、`profile_sequence.csv/npz`、`measurement_summary.json`；无 PLY／NPZ 点云副本 |
| 各阶段共有轻量文件 | `stage_result.json` 记录来源哈希、当前版本和产品映射，`workflow_stages.json` 记录阶段结论与产品路径 |

阶段之间不复制原始点云、检查图、预览嵌入点集或撤销历史。批量几何 CSV 仅位于 `测量复核/geometry_features.csv`，根目录不再重复写一份。仅有筛选记录而没有实际产出的阶段只写轻量清单，不伪造点云或特征；复核阶段的 `primary_ply/primary_npz`、`current_ply/current_npz` 为空，不标记为可编辑点云。单条任一阶段写入失败会撤掉该条全部不完整阶段产物，其他采集继续导出。源数据和缓存不改写。

总清单仍为每次采集一条，以 `stage_storage_relative_dirs` 链接各阶段；`stage_screening` 是逐阶段判断和实际输出的清单。根目录及各阶段 `index.html` 提供文件链接。新精简包供分析／交付，载入工作台时明确提示打开原工程或 merged 完整导出继续编辑；原来未带 `package_kind=stage_products` 的完整 split 和 merged 保持载入兼容。

merged 按本次输出的最后一阶段状态分类，保留原始文件、完整处理历史、依赖和离线预览；根目录继续输出批量几何表。POST /api/review_cloud 由前端提交 stage 与 stage_version，防止旧界面确认已变化的数据；POST /api/export 提交 stages，后端重复校验阶段、依赖与状态。前端不再提供或提交 `aligned_only` 条件，后端仍接受旧调用的该参数。旧调用未传 stages 时仍兼容旧导出约定，已有阶段记录时默认按三阶段处理。

连续框选沿用 `/api/processing/select`，新增可选 `append: true` 与当前 `candidate_id`；省略 append 时保持旧的替换行为。候选编号校验防止旧选区追加，同操作按完整原始点索引取并集并去重，跨视角仍对应同一点集；删除与恢复切换时重建候选。确认一次对应一条可撤销历史；前端确认后保留框选模式，Esc 仅退出模式，clear 清空候选。未确认的候选仍不持久化。
