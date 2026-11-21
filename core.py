"""Reconstruct measured surfaces in the native depth camera frame, in mm.

Brown rational projection follows Orbbec CoordinateUtil and OpenCV calib3d.
No learned generation, shape completion, centering, or per-animal scaling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'segmentation_workbench' / '.deps'))
import cv2
import numpy as np
from PIL import Image

VERSION = 'metric-multimodal-v1'
EXPORT_POLICY = 'segmentation-modality-v1'
COMMON_RESULT_FILES = {'points.npz', 'pointcloud.json', 'preview.html',
                       'mask_depth_check.png', 'rgb_projection_check.png'}
IR_SOURCE_FILES = {'depth_sensor_code_u16.npy', 'infrared.npy', 'infrared.png',
                   'depth_mm.npy', 'reconstruction.json', 'color.png'}
RGB_SOURCE_FILES = {'color.png', 'reconstruction.json', 'depth_aligned_to_color_mm.npy',
                    'depth_aligned_to_color_code_u16.npy'}
cv2.setNumThreads(1)


def source_files(meta):
    names = set(RGB_SOURCE_FILES if meta.get('input_mode') == 'rgb' else IR_SOURCE_FILES)
    if 'calibration.json' in meta.get('source_sha256', {}) or 'calibration.json' in meta.get('geometry', {}).get('source_sha256', {}):
        names.add('calibration.json')
    return names


def input_mode(report):
    """Old IR reports omitted input_mode; never infer it from folder names."""
    mode = report.get('input_mode')
    if mode is None:
        mode = 'rgb' if report.get('pixel_coordinate_space') == 'aligned_color' else 'infrared'
    if mode not in {'infrared', 'rgb'}:
        raise ValueError(f'未知分割输入模式：{mode}')
    return mode


def primary_ply(report):
    return 'cloud_rgb_mm.ply' if input_mode(report) == 'rgb' else 'cloud_ir_mm.ply'


def required_result_files(report):
    files = COMMON_RESULT_FILES | {primary_ply(report)}
    if report.get('rgb_registration', {}).get('check_image') == 'rgb_registration_check.png':
        files.add('rgb_registration_check.png')
    return files


def camera_identity(record):
    device = record.get('runtime', {}).get('device', {})
    name = str(device.get('name', '未知相机'))
    # Labels only: projection always uses this capture's profiles and extrinsics.
    kind = 'stereo' if 'gemini 336' in name.lower() else ('itof' if 'femto bolt' in name.lower() else 'unknown')
    return {'type': kind, 'model': name, 'serial_number': device.get('serial_number')}


def export_metadata(report):
    report = dict(report)
    report.update(input_mode=input_mode(report), primary_ply=primary_ply(report), export_policy=EXPORT_POLICY)
    return report


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def independent_paths(*paths):
    resolved = [Path(p).resolve() for p in paths]
    for i, a in enumerate(resolved):
        for b in resolved[i+1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError('源数据、分割结果与点云输出必须相互独立，不能相同或互相包含')
    return resolved


def within(root, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('非法采集相对路径')
    result = (Path(root) / p).resolve()
    if Path(root).resolve() not in result.parents:
        raise ValueError('采集目录越出数据根目录')
    return result


def camera(profile):
    i, d = profile['intrinsic'], profile['distortion']
    vals = np.array([i[k] for k in ['fx', 'fy', 'cx', 'cy']], dtype=float)
    if not np.isfinite(vals).all() or min(vals[:2]) <= 0 or min(i['width'], i['height']) <= 0:
        raise ValueError('相机内参非法')
    model = d.get('model_name', '')
    if model not in {'OB_DISTORTION_NONE', 'OB_DISTORTION_BROWN_CONRADY',
                     'OB_DISTORTION_BROWN_CONRADY_K6'}:
        raise ValueError(f'尚未支持的畸变模型：{model}')
    coeff = np.array([d.get(k, 0) for k in ['k1', 'k2', 'p1', 'p2', 'k3', 'k4', 'k5', 'k6']], float)
    if not np.isfinite(coeff).all() or (model == 'OB_DISTORTION_NONE' and coeff.any()):
        raise ValueError('畸变系数非法')
    matrix = np.array([[i['fx'], 0, i['cx']], [0, i['fy'], i['cy']], [0, 0, 1]], float)
    return matrix, coeff


def project(xyz, profile):
    """Independent forward Brown rational model; xyz is in this camera frame."""
    k, d = camera(profile)
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        x, y = xyz[:, 0] / xyz[:, 2], xyz[:, 1] / xyz[:, 2]
        r2 = x*x + y*y
        radial = (1+d[0]*r2+d[1]*r2**2+d[4]*r2**3) / (1+d[5]*r2+d[6]*r2**2+d[7]*r2**3)
        xd = x*radial + 2*d[2]*x*y + d[3]*(r2+2*x*x)
        yd = y*radial + d[2]*(r2+2*y*y) + 2*d[3]*x*y
    return np.column_stack([xd*k[0, 0]+k[0, 2], yd*k[1, 1]+k[1, 2]])


@lru_cache(maxsize=8)
def ray_table(serialized_profile):
    p = json.loads(serialized_profile)
    k, d = camera(p)
    h, w = p['intrinsic']['height'], p['intrinsic']['width']
    v, u = np.indices((h, w))
    uv = np.column_stack([u.ravel(), v.ravel()]).astype(float)
    criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 60, 1e-10)
    if hasattr(cv2, 'undistortPointsIter'):  # OpenCV 4
        rays = cv2.undistortPointsIter(uv.reshape(-1, 1, 2), k, d, None, None, criteria)
    else:  # OpenCV 5 folds the iterative overload into undistortPoints.
        rays = cv2.undistortPoints(uv.reshape(-1, 1, 2), k, d, criteria=criteria)
    rays = rays.reshape(-1, 2)
    rays = np.column_stack([rays, np.ones(len(rays))])
    error = np.linalg.norm(project(rays, p)-uv, axis=1)
    valid = np.isfinite(rays).all(axis=1) & np.isfinite(error) & (error <= .05)
    return rays.reshape(h, w, 3), valid.reshape(h, w), error.reshape(h, w)


def rays_for(profile):
    # Only calibration affects the cache; frame timestamps do not.
    return ray_table(json.dumps({k: profile[k] for k in ['intrinsic', 'distortion']}, sort_keys=True))


def verify_ir_depth(record, shape):
    streams = record['selected_capture']['streams']
    dp, ip = streams['depth']['profile'], streams['infrared']['profile']
    camera(dp)
    camera(ip)
    if shape != (dp['intrinsic']['height'], dp['intrinsic']['width']):
        raise ValueError('原始深度尺寸与实际采集 profile 不一致')
    for k in ['intrinsic', 'distortion']:
        if dp[k] != ip[k]:
            raise ValueError('红外与深度标定不同，需要显式跨模态投影，拒绝直接套用掩码')
    ext = record['actual_profile_extrinsics']
    if ext['depth_to_color'] != ext['infrared_to_color']:
        raise ValueError('红外与深度外参不同，需要单独配准')
    di, ii = streams['depth']['identity'], streams['infrared']['identity']
    if not di.get('present') or not ii.get('present'):
        raise ValueError('缺少红外／深度帧身份')
    for k in ['sdk_frame_index', 'device_timestamp_us']:
        if di.get(k) is None or di[k] != ii.get(k):
            raise ValueError('红外与深度不是同一采集帧，拒绝直接套用掩码')
    props = record['runtime']['device_properties_at_session_start']
    for name in ['depth_mirror', 'infrared_mirror', 'depth_flip', 'infrared_flip', 'color_mirror', 'color_flip']:
        if props.get(name, {}).get('value') is not False:
            raise ValueError(f'当前需要无镜像／翻转的源图像，状态不明确：{name}')
    return dp


def backproject(codes, scale, profile):
    if codes.ndim != 2 or codes.dtype != np.uint16:
        raise ValueError('原始深度码必须是二维 uint16 数组')
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError('深度毫米换算系数非法')
    rays, ray_valid, error = rays_for(profile)
    if codes.shape != rays.shape[:2]:
        raise ValueError('原始深度尺寸与相机标定不一致')
    z = codes.astype(float)*scale
    xyz = rays*z[..., None]
    valid = (codes > 0) & ray_valid & np.isfinite(xyz).all(axis=2)
    return xyz, valid, error


def discontinuities(z, threshold=50.):
    """Flag large measured depth steps. A flag is not proof of a bad point."""
    flag = np.zeros(z.shape, bool)
    for axis in [0, 1]:
        a, b = [slice(None)]*2, [slice(None)]*2
        a[axis], b[axis] = slice(None, -1), slice(1, None)
        a, b = tuple(a), tuple(b)
        edge = (z[a] > 0) & (z[b] > 0) & (abs(z[a]-z[b]) > threshold)
        flag[a] |= edge
        flag[b] |= edge
    return flag


def sample_rgb(scene_xyz, scene_valid, target_vu, image, profile, extrinsic, tolerance_mm=20.):
    """Project the entire measured scene for visibility, then sample target points.

    Missing RGB never deletes geometry. Occlusion tests use measured full-scene
    depth; objects visible only to the color camera cannot be tested this way.
    """
    camera(profile)
    h, w = image.shape[:2]
    if image.shape != (profile['intrinsic']['height'], profile['intrinsic']['width'], 3):
        raise ValueError('RGB PNG 尺寸与实际彩色 profile 不一致')
    r = np.array(extrinsic['rotation_row_major'], float).reshape(3, 3)
    t = np.array(extrinsic['translation_mm'], float)
    if not np.isfinite(r).all() or not np.isfinite(t).all() or abs(np.linalg.det(r)) < .5:
        raise ValueError('深度到彩色外参非法')
    # Use recorded extrinsics verbatim; never alter stored depth-camera XYZ.
    pts = scene_xyz[scene_valid] @ r.T + t
    uv = project(pts, profile)
    inside = (pts[:, 2] > 0) & np.isfinite(uv).all(axis=1)
    inside &= (uv[:, 0] >= 0) & (uv[:, 0] <= w-1) & (uv[:, 1] >= 0) & (uv[:, 1] <= h-1)
    ij = np.zeros((len(pts), 2), np.int32)
    ij[inside] = np.rint(uv[inside]).astype(np.int32)
    zbuffer = np.full((h, w), np.inf)
    np.minimum.at(zbuffer, (ij[inside, 1], ij[inside, 0]), pts[inside, 2])
    # Conservative 3x3 footprint compensates for sparse depth-to-color rasterization.
    zbuffer = cv2.erode(zbuffer, np.ones((3, 3), np.uint8))
    visible = inside & (pts[:, 2] <= zbuffer[ij[:, 1], ij[:, 0]]+tolerance_mm)
    rgb_scene = np.zeros((*scene_valid.shape, 3), np.uint8)
    good_scene = np.zeros(scene_valid.shape, bool)
    uv_scene = np.full((*scene_valid.shape, 2), np.nan, np.float32)
    colors = np.zeros((len(pts), 3), np.uint8)
    colors[visible] = image[ij[visible, 1], ij[visible, 0]]
    rgb_scene[scene_valid], good_scene[scene_valid], uv_scene[scene_valid] = colors, visible, uv
    v, u = target_vu.T
    return rgb_scene[v, u], good_scene[v, u], uv_scene[v, u], {
        'extrinsic_determinant': float(np.linalg.det(r)),
        'extrinsic_orthogonality_error_frobenius': float(np.linalg.norm(r.T@r-np.eye(3))),
        'visibility_tolerance_mm': tolerance_mm,
        'visibility_method': 'full_measured_scene_zbuffer_3x3_minimum',
    }


def choose_mask(folder, meta, allow_draft):
    if meta.get('reviewed') is True and meta.get('status') == 'reviewed':
        name, reviewed = 'trunk_mask.png', True
    elif allow_draft:
        name = 'trunk_mask_working.png' if meta.get('status') == 'corrected' else 'trunk_mask_draft.png'
        reviewed = False
    else:
        raise ValueError('分割尚未人工确认；预览须显式添加 --allow-draft')
    path = Path(folder)/name
    with Image.open(path) as im:
        mask = np.array(im)
        if im.mode != 'L' or mask.ndim != 2 or not np.isin(mask, [0, 255]).all() or not mask.any():
            raise ValueError('掩码必须是非空单通道 0/255 PNG')
    return path, mask, reviewed


def reconstruct(source, segmentation, allow_draft=False, max_rgb_delta_us=10000, visibility_mm=20.):
    source, segmentation = Path(source), Path(segmentation)
    meta = read_json(segmentation/'segmentation.json')
    if meta.get('input_mode') == 'rgb':
        from .rgb import reconstruct_rgb
        return reconstruct_rgb(source, segmentation, meta, allow_draft, max_rgb_delta_us, visibility_mm)
    if meta.get('input_mode') not in {None, 'infrared'}:
        raise ValueError('不支持的分割输入模式，请选择红外或经过几何核对的 RGB 分割成果')
    mask_path, mask, reviewed = choose_mask(segmentation, meta, allow_draft)
    used = source_files(meta)
    hashes = {name: sha256(source/name) for name in used}
    mask_hash, meta_hash = sha256(mask_path), sha256(segmentation/'segmentation.json')
    for name, expected in meta.get('source_sha256', {}).items():
        if name not in hashes or hashes[name] != expected:
            raise ValueError(f'分割所用源数据已变化／无法核对：{name}')
    if not {'infrared.npy', 'infrared.png', 'depth_mm.npy'} <= meta.get('source_sha256', {}).keys():
        raise ValueError('分割记录缺少源文件校验值，无法验证掩码来源')
    record = read_json(source/'reconstruction.json')
    codes = np.load(source/'depth_sensor_code_u16.npy', allow_pickle=False)
    ir = np.load(source/'infrared.npy', allow_pickle=False)
    if ir.shape != codes.shape or mask.shape != codes.shape or not np.isfinite(ir).all():
        raise ValueError('红外、深度、掩码尺寸或数据非法')
    profile = verify_ir_depth(record, codes.shape)
    if meta.get('geometry'):
        from segmentation_workbench.geometry import prepare_frame
        _, _, geometry, _ = prepare_frame(source, 'infrared')
        if meta['geometry'].get('geometry_id') != geometry['geometry_id']:
            raise ValueError('IR 分割的标定或深度来源已变化，请重新核对分割成果')
    scale = float(record['selected_capture']['streams']['depth']['value_scale_mm_per_code'])
    legacy = np.load(source/'depth_mm.npy', allow_pickle=False)
    expected_mm = np.clip(np.rint(codes.astype(float)*scale), 0, 65535).astype(np.uint16)
    if not np.array_equal(legacy, expected_mm):
        raise ValueError('原始深度码与分割阶段使用的毫米数组不一致')
    xyz, valid, error = backproject(codes, scale, profile)
    fg = mask == 255
    selected = fg & valid
    vu = np.column_stack(np.nonzero(selected))
    if not len(vu):
        raise ValueError('掩码内没有可反投影的有效深度点')
    points = xyz[selected].astype(np.float32)
    rgb = np.zeros((len(points), 3), np.uint8)
    rgb_valid = np.zeros(len(points), bool)
    rgb_uv = np.full((len(points), 2), np.nan, np.float32)
    dt = int(record['selected_capture']['timestamp_deltas']['color_minus_depth_device_us'])
    rgb_info = {'enabled': False, 'color_minus_depth_device_us': dt, 'max_absolute_delta_us': max_rgb_delta_us}
    if abs(dt) <= max_rgb_delta_us:
        with Image.open(source/'color.png') as im:
            color = np.array(im.convert('RGB'))
        rgb, rgb_valid, rgb_uv, detail = sample_rgb(xyz, valid, vu, color,
            record['selected_capture']['streams']['color']['profile'],
            record['actual_profile_extrinsics']['depth_to_color'], visibility_mm)
        rgb_info.update(detail, enabled=True)
    else:
        rgb_info['disabled_reason'] = 'RGB 与深度的设备时间戳差超过阈值；保留几何，RGB 标为缺失'
    edges = discontinuities(codes.astype(float)*scale)[selected]
    stats = {'mask_pixels': int(fg.sum()), 'nonzero_depth_pixels_in_mask': int((fg & (codes > 0)).sum()),
             'points': len(points), 'valid_depth_ratio': float(selected.sum()/fg.sum()),
             'invalid_ray_pixels_in_mask': int((fg & (codes > 0) & ~valid).sum()),
             'rgb_valid_ratio': float(rgb_valid.mean()), 'depth_discontinuity_ratio': float(edges.mean()),
             'depth_reprojection_error_px_max': float(error[selected].max()),
             'xyz_min_mm': points.min(axis=0).astype(float).tolist(),
             'xyz_max_mm': points.max(axis=0).astype(float).tolist(),
             'camera_axis_extent_mm': np.ptp(points, axis=0).astype(float).tolist()}
    warnings = ['单视角可见表面；未补全遮挡面或腹面；轴向跨度不等同于解剖体长／体宽',
                '毫米尺度来自设备标定，尚未用已知尺寸实物验证测量误差',
                '红外与深度按记录的标定及帧身份核对，真实边界对应仍需人工检查',
                'RGB 可见性仅依据深度相机观测到的场景；彩色视角独有遮挡物可能无法识别']
    if not reviewed:
        warnings.insert(0, '未审核分割初稿，仅供流程预览，不作为合格躯干测量或训练样本')
    if rgb_info.get('extrinsic_orthogonality_error_frobenius', 0) > .001:
        warnings.append('记录的深度到彩色旋转矩阵非严格正交；按原值配色并记录误差，需检查 RGB 边界')
    if fg[0].any() or fg[-1].any() or fg[:, 0].any() or fg[:, -1].any():
        warnings.append('掩码触及图像边界，检查出画／截断')
    payload = dict(xyz_mm=points, pixel_uv=vu[:, ::-1].astype(np.int32),
                   depth_code=codes[selected], infrared_raw=ir[selected], infrared_valid=np.ones(len(points), bool), rgb=rgb,
                   rgb_valid=rgb_valid, rgb_uv=rgb_uv, depth_discontinuity=edges)
    report = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
              'software': {'python': sys.version.split()[0], 'numpy': np.__version__, 'opencv': cv2.__version__},
              'source_dir': str(source.resolve()), 'segmentation_dir': str(segmentation.resolve()),
              'pig_id': meta.get('pig_id'), 'capture_id': meta.get('capture_id'),
              'reviewed': reviewed, 'segmentation_status': meta.get('status'),
              'segmentation_revision': meta.get('revision'), 'mask_file': mask_path.name,
              'input_mode': 'infrared', 'camera': camera_identity(record), 'infrared_available': True,
              'pixel_coordinate_space': 'depth_native', 'depth_value_frame': 'depth',
              'coordinate_system': 'native_depth_camera', 'unit': 'mm',
              'axes': {'X': 'right', 'Y': 'down', 'Z': 'forward (axial depth)'},
              'coordinate_operations': 'none; no centering, rotation, or rescaling',
              'scale_mm_per_code': scale, 'depth_profile': profile,
              'ir_registration_check': 'equal_intrinsics_distortion_extrinsics_and_frame_identity',
              'rgb': rgb_info, 'stats': stats, 'warnings': warnings,
              'source_sha256': hashes, 'mask_sha256': mask_hash, 'segmentation_json_sha256': meta_hash,
              'attributes': {'infrared_raw': '原始 IR 像素值；未解释为深度置信度',
                             'depth_discontinuity': '4邻域深度差超过50mm的边缘标记；未删除',
                             'rgb_valid': '仅配色有效性，不代表几何有效性'},
              'filtering': '仅排除零深度及不收敛射线；未平滑、补洞、采样或自动删除跳变点'}
    if hashes != {name: sha256(source/name) for name in used}:
        raise ValueError('处理期间源数据发生变化，拒绝保存')
    if mask_hash != sha256(mask_path) or meta_hash != sha256(segmentation/'segmentation.json'):
        raise ValueError('处理期间分割发生变化，请重新生成')
    return payload, export_metadata(report), mask


def ply_bytes(payload, colors):
    p = payload['xyz_mm']
    dtype = np.dtype([('x', '<f4'), ('y', '<f4'), ('z', '<f4'),
                      ('red', 'u1'), ('green', 'u1'), ('blue', 'u1'),
                      ('u', '<i4'), ('v', '<i4'), ('infrared', '<f4'), ('rgb_valid', 'u1')])
    rows = np.empty(len(p), dtype=dtype)
    for j, key in enumerate(['x', 'y', 'z']): rows[key] = p[:, j]
    for j, key in enumerate(['red', 'green', 'blue']): rows[key] = colors[:, j]
    rows['u'], rows['v'] = payload['pixel_uv'].T
    rows['infrared'], rows['rgb_valid'] = payload['infrared_raw'], payload['rgb_valid']
    header = ('ply\nformat binary_little_endian 1.0\ncomment coordinates native_depth_camera unit mm\n'
              f'element vertex {len(p)}\nproperty float x\nproperty float y\nproperty float z\n'
              'property uchar red\nproperty uchar green\nproperty uchar blue\n'
              'property int u\nproperty int v\nproperty float infrared\nproperty uchar rgb_valid\nend_header\n')
    return header.encode('ascii')+rows.tobytes()


def display_ir(values):
    finite = np.isfinite(values)
    colors = np.full((len(values), 3), [110, 120, 140], dtype=np.uint8)
    if finite.any():
        lo, hi = np.percentile(values[finite], [1, 99])
        gray = (np.clip((values[finite].astype(float)-lo)/max(hi-lo, 1), 0, 1)**.65*255).astype(np.uint8)
        colors[finite] = gray[:, None]
    return colors


def export_frame(destination, payload, report, mask):
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(dest/'points.npz', **payload)
    rgb_colors = payload['rgb'].copy()
    rgb_colors[~payload['rgb_valid']] = [110, 120, 140]
    colors = rgb_colors if input_mode(report) == 'rgb' else display_ir(payload['infrared_raw'])
    (dest/primary_ply(report)).write_bytes(ply_bytes(payload, colors))
    write_json(dest/'pointcloud.json', export_metadata(report))
    if report.get('input_mode') == 'rgb':
        from segmentation_workbench.geometry import prepare_frame
        overlay, _, geometry, _ = prepare_frame(Path(report['source_dir']), 'rgb')
        if geometry['geometry_id'] != report['geometry_id']:
            raise ValueError('导出检查图时标定或深度已变化，请重新生成')
    else:
        with Image.open(Path(report['source_dir'])/'infrared.png') as im:
            overlay = np.array(im.convert('RGB'))
    overlay[mask > 0] = (overlay[mask > 0]*.6+np.array([40, 225, 170])*.4).astype(np.uint8)
    # Show exactly the source pixels that survived into the cloud.
    keep = np.zeros(mask.shape, bool)
    u, v = payload['pixel_uv'].T
    keep[v, u] = True
    overlay[(mask > 0) & ~keep] = [255, 60, 90]
    Image.fromarray(overlay).save(dest/'mask_depth_check.png')
    with Image.open(Path(report['source_dir'])/'color.png') as im:
        color_check = np.array(im.convert('RGB'))
    uv = payload['rgb_uv'][payload['rgb_valid']]
    if len(uv):
        u, v = np.rint(uv).astype(int).T
        color_check[v, u] = (color_check[v, u]*.5+np.array([40, 225, 170])*.5).astype(np.uint8)
    Image.fromarray(color_check).save(dest/'rgb_projection_check.png')
    if report.get('rgb_registration', {}).get('check_image') == 'rgb_registration_check.png':
        from .registration import registration_image
        source = Path(report['source_dir'])
        for name, digest in report['source_sha256'].items():
            if sha256(source/name) != digest:
                raise ValueError('导出配准检查图时源数据已变化，请重新生成')
        check = registration_image(source, read_json(source/'reconstruction.json'), geometry, mask)
        Image.fromarray(check).save(dest/'rgb_registration_check.png')
        if any(sha256(source/name) != digest for name, digest in report['source_sha256'].items()):
            raise ValueError('导出配准检查图期间源数据已变化，请重新生成')
    return dest
