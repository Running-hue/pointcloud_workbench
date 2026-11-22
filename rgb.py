"""Consume verified RGB masks on the saved aligned-depth grid."""
from datetime import datetime, timezone
import sys

from .core import (VERSION, Image, np, cv2, source_files, sha256, choose_mask, read_json,
                   backproject, project, discontinuities, camera_identity, export_metadata)
from .geometry import prepare_frame, validate_mask_region, RGB_VERSION
from .registration import rgb_timing, depth_to_color, audit_rgb_registration


def reconstruct_rgb(source, segmentation, meta, allow_draft, max_rgb_delta_us, visibility_mm):
    saved = meta.get('geometry', {})
    if saved.get('version') != RGB_VERSION or saved.get('status') != 'verified' or not saved.get('pointcloud_ready'):
        raise ValueError('该 RGB 分割未记录已核对的对齐深度几何；请用当前分割工作台新建 RGB 任务')
    mask_path, mask, reviewed = choose_mask(segmentation, meta, allow_draft)
    used = source_files(meta)
    # Native depth is used only as an independent check of the saved SDK align.
    # Include it in provenance and in the final immutability check when present.
    if (source/'depth_sensor_code_u16.npy').is_file():
        used.add('depth_sensor_code_u16.npy')
    hashes = {name:sha256(source/name) for name in used}
    mask_hash, meta_hash = sha256(mask_path), sha256(segmentation/'segmentation.json')
    expected = meta.get('source_sha256', {})
    if not {'color.png', 'reconstruction.json', 'depth_aligned_to_color_mm.npy'} <= expected.keys():
        raise ValueError('RGB 分割记录缺少源图像、对齐深度或标定的校验值')
    for name, digest in expected.items():
        if name not in hashes or hashes[name] != digest:
            raise ValueError(f'RGB 分割所用源数据已变化／无法核对：{name}')
    image, depth_mm, geometry, image_valid = prepare_frame(source, 'rgb')
    if saved.get('geometry_id') != geometry['geometry_id']:
        raise ValueError('RGB 分割的标定或对齐深度来源已变化，请重新核对分割成果')
    if list(mask.shape) != geometry['shape_hw'] or meta.get('shape_hw') != geometry['shape_hw']:
        raise ValueError('RGB 掩码尺寸与已保存对齐深度网格不一致，不能缩放掩码代替配准')
    validate_mask_region(mask, image_valid)
    codes = np.load(source/'depth_aligned_to_color_code_u16.npy', allow_pickle=False)
    scale = geometry['depth_scale_mm_per_code']
    expected_mm = np.clip(np.rint(codes.astype(float)*scale), 0, 65535).astype(np.uint16)
    if depth_mm.dtype != np.uint16 or not np.array_equal(depth_mm, expected_mm):
        raise ValueError('对齐深度码与分割使用的毫米数组不一致')
    record = read_json(source/'reconstruction.json')
    # The RGB mask selects geometry, so an unsynchronized RGB frame cannot be
    # handled by merely disabling colors (which is valid only for the IR path).
    dt = rgb_timing(record, max_rgb_delta_us)
    props = record.get('runtime', {}).get('device_properties_at_session_start', {})
    for name in ['depth_mirror', 'depth_flip', 'color_mirror', 'color_flip']:
        if props.get(name, {}).get('value') is not False:
            raise ValueError(f'当前需要无镜像／翻转的 RGB 数据，状态不明确：{name}')
    profile = geometry['depth_profile']
    color_xyz, valid, error = backproject(codes, scale, profile)
    fg = mask > 0
    selected = fg & valid & image_valid
    v, u = np.nonzero(selected)
    if not len(u):
        raise ValueError('RGB 掩码内没有有效对齐深度点')
    color_points = color_xyz[selected]
    # Preserve the workbench's original depth-camera origin. Alignment has already
    # occurred: do not apply depth_to_color to these color-frame points again.
    rotation, translation, orthogonality, determinant = depth_to_color(record)
    registration = audit_rgb_registration(source, record, geometry, mask, codes, scale)
    points = np.linalg.solve(rotation, (color_points-translation).T).T.astype(np.float32)
    if not np.isfinite(points).all():
        raise ValueError('转换到原始深度相机坐标产生无效点')
    rgb_uv = project(color_points, geometry['source_profile'])
    with Image.open(source/'color.png') as im:
        width, height = im.size
    rgb_valid = np.isfinite(rgb_uv).all(axis=1) & (rgb_uv[:,0] >= 0) & (rgb_uv[:,0] <= width-1) & (rgb_uv[:,1] >= 0) & (rgb_uv[:,1] <= height-1)
    rgb = image[selected].copy()
    rgb[~rgb_valid] = 0
    edges = discontinuities(codes.astype(float)*scale)[selected]
    warnings = ['单视角可见表面；未补全遮挡面或腹面；轴向跨度不等同于解剖体尺',
                '使用已保存的 RGB 对齐深度，采样密度由对齐网格决定，不代表独立深度测量数量',
                '毫米尺度来自记录的标定，实际尺寸误差仍需实物验证',
                '本路径未关联原始红外强度；IR 属性为缺失值，不生成 IR PLY']
    if not reviewed: warnings.insert(0, '未审核分割初稿，仅供流程预览，不作为合格躯干测量或训练样本')
    warnings.extend(registration['warnings'])
    warnings.append('配准交叉核对只验证采集记录的几何一致性，未用标定板重估内外参，不能替代实物尺寸精度验证')
    if orthogonality > .001: warnings.append('记录的相机旋转矩阵轻微非正交，使用原矩阵求逆并记录误差')
    if fg[0].any() or fg[-1].any() or fg[:,0].any() or fg[:,-1].any():
        warnings.append('掩码触及图像边界，检查出画／截断')
    payload = dict(xyz_mm=points, pixel_uv=np.column_stack([u,v]).astype(np.int32),
                   depth_code=codes[selected], infrared_raw=np.full(len(u),np.nan,np.float32),
                   infrared_valid=np.zeros(len(u),bool), rgb=rgb, rgb_valid=rgb_valid,
                   rgb_uv=rgb_uv.astype(np.float32), depth_discontinuity=edges)
    report = {'version':VERSION, 'created_at':datetime.now(timezone.utc).isoformat(),
              'software':{'python':sys.version.split()[0], 'numpy':np.__version__, 'opencv':cv2.__version__},
              'source_dir':str(source.resolve()), 'segmentation_dir':str(segmentation.resolve()),
              'pig_id':meta.get('pig_id'), 'capture_id':meta.get('capture_id'),
              'reviewed':reviewed, 'segmentation_status':meta.get('status'),
              'segmentation_revision':meta.get('revision'), 'mask_file':mask_path.name,
              'input_mode':'rgb', 'camera':camera_identity(record), 'geometry_id':geometry['geometry_id'],
              'rgb_registration':registration,
              'pixel_coordinate_space':'aligned_color', 'depth_value_frame':'color',
              'coordinate_system':'native_depth_camera', 'unit':'mm',
              'axes':{'X':'right','Y':'down','Z':'forward (axial depth)'},
              'coordinate_operations':'backproject saved aligned-depth grid in color camera; apply inverse recorded depth_to_color once; no centering or scaling',
              'color_to_depth_matrix':np.linalg.inv(np.block([[rotation,translation[:,None]],[np.zeros((1,3)),np.ones((1,1))]])).tolist(),
              'scale_mm_per_code':scale, 'depth_profile':profile,
              'ir_registration_check':'not_associated', 'infrared_available':False,
              'rgb':{'enabled':bool(rgb_valid.any()), 'color_minus_depth_device_us':dt,
                     'max_absolute_delta_us':max_rgb_delta_us, 'visibility_tolerance_mm':visibility_mm,
                     'visibility_method':'saved_aligned_depth_grid_and_recorded_color_remap',
                     'extrinsic_determinant':determinant, 'extrinsic_orthogonality_error_frobenius':orthogonality},
              'stats':{'mask_pixels':int(fg.sum()), 'nonzero_depth_pixels_in_mask':int((fg&(codes>0)).sum()),
                       'points':len(u), 'valid_depth_ratio':float(len(u)/fg.sum()),
                       'invalid_ray_pixels_in_mask':int((fg&(codes>0)&~valid).sum()),
                       'rgb_valid_ratio':float(rgb_valid.mean()), 'depth_discontinuity_ratio':float(edges.mean()),
                       'depth_reprojection_error_px_max':float(error[selected].max()),
                       'xyz_min_mm':points.min(axis=0).astype(float).tolist(),
                       'xyz_max_mm':points.max(axis=0).astype(float).tolist(),
                       'camera_axis_extent_mm':np.ptp(points,axis=0).astype(float).tolist()},
              'warnings':warnings, 'source_sha256':hashes, 'mask_sha256':mask_hash,
              'segmentation_json_sha256':meta_hash,
              'attributes':{'pixel_uv':'RGB aligned-depth grid (not native IR grid)',
                            'depth_code':'saved aligned color-camera axial depth code',
                            'infrared_raw':'NaN; not associated', 'infrared_valid':'false for this route',
                            'rgb_valid':'仅配色有效性，不代表几何有效性'},
              'filtering':'only valid masked aligned-depth pixels and converged rays; no smoothing or hole filling'}
    if hashes != {name:sha256(source/name) for name in used}:
        raise ValueError('处理期间 RGB 源数据发生变化，拒绝保存')
    if mask_hash != sha256(mask_path) or meta_hash != sha256(segmentation/'segmentation.json'):
        raise ValueError('处理期间 RGB 分割发生变化，请重新生成')
    return payload, export_metadata(report), mask
