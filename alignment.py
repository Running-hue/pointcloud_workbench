"""Reviewed per-capture ground/body frames. Source clouds remain immutable."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
from html import escape
import io
import json
from pathlib import Path
import secrets
import shutil
import tempfile
from urllib.parse import quote

from .core import (np, cv2, Image, backproject, project, sha256, read_json,
                   write_json, independent_paths, within, display_ir, verify_ir_depth,
                   camera_identity, input_mode, sample_rgb)
from .registration import rgb_timing, depth_to_color

VERSION = 'ground-body-mm-v1'
SIDE_FILES = {'alignment.json', 'body_points.npz', 'cloud_body_mm.ply',
              'body_preview.html', 'alignment_check.png'}
RAW_FILES = ('reconstruction.json', 'depth_sensor_code_u16.npy')


def encoded_png(array):
    out = io.BytesIO()
    Image.fromarray(array).save(out, format='PNG')
    return out.getvalue()


def data_image(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')


def enhanced_infrared(values):
    """Lift dim scene texture for display only; never modify saved IR or depth."""
    values = np.asarray(values, float)
    if values.ndim != 2 or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError('原始红外强度无效')
    logged = np.log1p(values)
    low, high = np.percentile(logged, [1, 99.5])
    gray = np.clip((logged-low)/max(high-low, 1e-8)*255, 0, 255).astype(np.uint8)
    gray = cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8)).apply(gray)
    gray = np.rint(255*(gray.astype(float)/255)**.7).astype(np.uint8)
    gray[values == 0] = 0
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)


def rgb_scene_on_depth(source, record, report, xyz, valid):
    """Color the native depth grid using full-scene calibrated visibility tests."""
    color_file = source/'color.png'
    if not color_file.is_file():
        raise ValueError('缺少原始 color.png')
    expected = report.get('source_sha256', {}).get('color.png')
    if not expected:
        raise ValueError('原点云未记录 RGB 图像校验值，无法核对来源')
    digest = sha256(color_file)
    if digest != expected:
        raise ValueError('原始 RGB 已变化，请核对点云来源')
    streams = record['selected_capture']['streams']
    for name in ('depth', 'color'):
        identity = streams[name].get('identity', {})
        if not identity.get('present') or identity.get('device_timestamp_us') is None:
            raise ValueError('缺少 RGB／深度帧身份，无法核对配对关系')
    rgb_info = report.get('rgb', {})
    delta = rgb_timing(record, float(rgb_info.get('max_absolute_delta_us', 10000)))
    props = record['runtime']['device_properties_at_session_start']
    for name in ('depth_mirror', 'depth_flip', 'color_mirror', 'color_flip'):
        if props.get(name, {}).get('value') is not False:
            raise ValueError(f'RGB／深度镜像或翻转状态不明确：{name}')
    depth_to_color(record)
    with Image.open(color_file) as im:
        color = np.array(im.convert('RGB'))
    vu = np.column_stack(np.nonzero(valid))
    tolerance = float(rgb_info.get('visibility_tolerance_mm', 20))
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError('RGB 可见性容差无效')
    colors, visible, _, detail = sample_rgb(
        xyz, valid, vu, color, streams['color']['profile'],
        record['actual_profile_extrinsics']['depth_to_color'], tolerance)
    if not visible.any():
        raise ValueError('原始深度与 RGB 没有可用的共同可见区域')
    yy, xx = np.indices(valid.shape)
    checker = np.where((xx//12+yy//12) % 2, 204, 224).astype(np.uint8)
    image = np.repeat(checker[..., None], 3, axis=2)
    image[vu[visible, 0], vu[visible, 1]] = colors[visible]
    return image, {'available': True, 'color_sha256': digest,
                   'color_minus_depth_device_us': delta,
                   'mapped_depth_pixels': int(visible.sum()),
                   'valid_depth_coverage': float(visible.mean()),
                   'pixel_coordinate_space': 'depth_native', **detail}


def foreground_overlay(foreground):
    """A separately switchable translucent range with a clear boundary."""
    mask = foreground.astype(np.uint8)
    interior = cv2.erode(mask, np.ones((3, 3), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    rgba = np.zeros((*mask.shape, 4), np.uint8)
    rgba[foreground] = [35, 200, 115, 38]
    rgba[foreground & ~interior] = [20, 235, 125, 240]
    return rgba


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def capture_root(folder, relative):
    """Recover a relocated dataset root only when its full relative suffix matches."""
    folder, rel = Path(folder).resolve(), Path(relative)
    if folder.parts[-len(rel.parts):] != rel.parts:
        return folder
    for _ in rel.parts:
        folder = folder.parent
    return folder


def fit_ground(points, threshold_mm=10.):
    """RANSAC, then refit on inliers; distances and support are in mm."""
    p = np.asarray(points, float)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 100 or not np.isfinite(p).all():
        raise ValueError('地面区域至少需要 100 个有效深度点，请扩大圈选范围')
    if not np.isfinite(threshold_mm) or not 1 <= threshold_mm <= 30:
        raise ValueError('地面拟合容差需在 1–30 mm 之间')
    rng = np.random.default_rng(29)
    sample = p[rng.choice(len(p), min(5000, len(p)), replace=False)]
    triples = sample[rng.integers(0, len(sample), (320, 3))]
    normals = np.cross(triples[:, 1]-triples[:, 0], triples[:, 2]-triples[:, 0])
    good = np.linalg.norm(normals, axis=1) > 1e-6
    if not good.any():
        raise ValueError('圈选地面点退化，无法确定平面')
    normals = normals[good]
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    offsets = -np.sum(normals*triples[good, 0], axis=1)
    counts = (np.abs(sample @ normals.T + offsets) <= threshold_mm).sum(axis=0)
    best = int(np.argmax(counts))
    n, d = normals[best], float(offsets[best])
    for _ in range(3):
        inside = np.abs(p @ n + d) <= threshold_mm
        if inside.sum() < 100:
            raise ValueError('可拟合的地面点不足，请避开栏杆、猪体和深度空洞')
        center = p[inside].mean(axis=0)
        _, _, axes = np.linalg.svd(p[inside]-center, full_matrices=False)
        n = axes[-1]
        d = -float(n @ center)
    residuals = np.abs(p @ n+d)
    inside = residuals <= threshold_mm
    if inside.mean() < .6:
        raise ValueError('地面区域内一致平面不足 60%，请重新圈选同一块地面')
    uv = p[inside] @ axes[:2].T
    spans = np.ptp(np.percentile(uv, [5, 95], axis=0), axis=0)
    if min(spans) < 80:
        raise ValueError('地面点分布过窄，请在两个方向均圈选更大范围的地面（至少约 80 mm）')
    return n, d, inside, {
        'candidate_points': len(p), 'inlier_points': int(inside.sum()),
        'inlier_ratio': float(inside.mean()), 'threshold_mm': float(threshold_mm),
        'inlier_rmse_mm': float(np.sqrt(np.mean(residuals[inside]**2))),
        'inlier_residual_p95_mm': float(np.percentile(residuals[inside], 95)),
        'support_span_p05_p95_mm': spans.tolist(),
    }


def body_frame(normal, offset, tail, neck):
    n = np.asarray(normal, float)
    norm = np.linalg.norm(n)
    if n.shape != (3,) or not np.isfinite(n).all() or norm < 1e-8:
        raise ValueError('地面法线无效')
    n, d = n/norm, float(offset)/norm
    tail, neck = np.asarray(tail, float), np.asarray(neck, float)
    if tail.shape != (3,) or neck.shape != (3,) or not np.isfinite([tail, neck]).all():
        raise ValueError('颈根或尾根坐标无效')
    if (float(n @ tail+d)+float(n @ neck+d))/2 < 0:
        n, d = -n, -d
    if min(n @ tail+d, n @ neck+d) <= 20:
        raise ValueError('端点过于接近拟合平面，请核对是否误选了地面或猪体平面')
    v = neck-tail
    horizontal = v-(v @ n)*n
    length = float(np.linalg.norm(horizontal))
    if length < 50:
        raise ValueError('颈根与尾根的地面投影间距不足 50 mm，无法确定长轴')
    x = horizontal/length
    y = np.cross(n, x)
    origin = tail-(n @ tail+d)*n
    rotation = np.stack([x, y, n])
    matrix = np.eye(4)
    matrix[:3, :3], matrix[:3, 3] = rotation, -rotation @ origin
    inverse = np.eye(4)
    inverse[:3, :3], inverse[:3, 3] = rotation.T, origin
    return {'plane_normal_camera': n.tolist(), 'plane_offset_mm': d,
            'origin_camera_mm': origin.tolist(), 'rotation_camera_to_body': rotation.tolist(),
            'camera_to_body_4x4': matrix.tolist(), 'body_to_camera_4x4': inverse.tolist(),
            'neck_tail_horizontal_span_mm': length}


def transform_points(points, frame):
    m = np.asarray(frame['camera_to_body_4x4'], float)
    return np.asarray(points) @ m[:3, :3].T + m[:3, 3]


def alignment_status(folder):
    """Only advertise a complete sidecar bound to this exact camera cloud."""
    folder = Path(folder)
    if not all((folder/n).is_file() and not (folder/n).is_symlink() for n in SIDE_FILES):
        return False
    try:
        r = read_json(folder/'alignment.json')
        return (r.get('version') == VERSION and r.get('coordinate_system') == 'ground_body'
                and r.get('confirmed') is True
                and r['input_cloud_sha256'] == sha256(folder/'points.npz')
                and r['body_points_sha256'] == sha256(folder/'body_points.npz'))
    except (KeyError, ValueError, OSError, TypeError):
        return False


def render_body_view(payload, record):
    points = payload['xyz_mm']
    idx = np.sort(np.random.default_rng(7).choice(len(points), min(len(points), 16000), replace=False))
    rgb = payload['rgb'][idx].copy()
    good = payload['rgb_valid'][idx]
    if record.get('input_mode') == 'infrared':
        rgb = display_ir(payload['infrared_raw'])[idx]
    else:
        rgb[~good] = [110, 120, 140]
    data = {'xyz': points[idx].round(3).tolist(), 'rgb': rgb.tolist(),
            'record': record, 'count': len(points)}
    template = Path(__file__).with_name('body_view.html').read_text(encoding='utf-8')
    return template.replace('__DATA__', json.dumps(data, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c'))


class AlignmentService:
    def __init__(self, workspace):
        self.ws = workspace
        self.contexts = {}

    def _workspace_result(self, payload):
        self.ws.idle()
        if payload.get('dataset_id') != self.ws.dataset_id:
            raise ValueError('目录已切换，请重新打开坐标对齐')
        row = self.ws.row(payload.get('id'))
        result = self.ws.results.get(row['relative_dir'])
        if not result:
            raise ValueError('请先生成或打开当前采集的点云')
        return row, result

    def _raw(self, payload, row, report):
        roots = [payload.get('source_root')] if payload.get('source_root') else [
            self.ws.settings.get('alignment_sources', {}).get(str(self.ws.results_root)),
            self.ws.source, self.ws.settings.get('source')]
        candidates = [within(Path(root).expanduser().resolve(), row['relative_dir']) for root in roots if root]
        if not payload.get('source_root') and report.get('source_dir'):
            candidates.append(Path(report['source_dir']).expanduser().resolve())
        for source in candidates:
            if all((source/n).is_file() and not (source/n).is_symlink() for n in RAW_FILES):
                hashes = {n: sha256(source/n) for n in RAW_FILES}
                expected = report.get('source_sha256', {})
                if any(n not in expected for n in RAW_FILES):
                    raise ValueError('该成果缺少原始深度校验值，请重新生成点云后再建立测量坐标')
                if all(hashes[n] == expected[n] for n in RAW_FILES):
                    return source, hashes
        raise ValueError('找不到与当前点云匹配的原始深度；请选择原始采集根目录（保留相对目录层级）')

    def context(self, payload):
        with self.ws.lock:
            row, result = self._workspace_result(payload)
            report = read_json(result['folder']/'pointcloud.json')
            source, hashes = self._raw(payload, row, report)
            record = read_json(source/'reconstruction.json')
            report.update(input_mode=input_mode(report), camera=report.get('camera') or camera_identity(record))
            stream = record['selected_capture']['streams']['depth']
            codes = np.load(source/'depth_sensor_code_u16.npy', allow_pickle=False)
            xyz, valid, _ = backproject(codes, float(stream['value_scale_mm_per_code']), stream['profile'])
            if not valid.any():
                raise ValueError('原始场景没有有效深度，无法建立地面坐标')
            with np.load(result['folder']/'points.npz', allow_pickle=False) as f:
                cloud = {k: f[k] for k in f.files}
            if not len(cloud['xyz_mm']) or not np.isfinite(cloud['xyz_mm']).all():
                raise ValueError('当前点云为空或含无效坐标')
            uv = project(cloud['xyz_mm'], stream['profile'])
            h, w = codes.shape
            keep = np.isfinite(uv).all(axis=1) & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
            pixels = np.rint(uv[keep]).astype(int)
            pixels[:, 0] = np.clip(pixels[:, 0], 0, w-1)
            pixels[:, 1] = np.clip(pixels[:, 1], 0, h-1)
            fg = np.zeros((h, w), np.uint8)
            fg[pixels[:, 1], pixels[:, 0]] = 255
            fg = cv2.dilate(fg, np.ones((7, 7), np.uint8)) > 0
            fy, fx = np.nonzero(fg)
            foreground_bounds = [int(fx.min()), int(fy.min()), int(fx.max()+1), int(fy.max()+1)] if len(fx) else None
            z = codes.astype(float)*float(stream['value_scale_mm_per_code'])
            lo, hi = np.percentile(z[valid], [2, 98])
            gray = np.clip((z-lo)/max(hi-lo, 1)*255, 0, 255).astype(np.uint8)
            depth_image = cv2.cvtColor(cv2.applyColorMap(gray, cv2.COLORMAP_TURBO), cv2.COLOR_BGR2RGB)
            depth_image[~valid] = 0
            ir_file = source/'infrared.npy'
            ir_verified = False
            if ir_file.is_file():
                try:
                    verify_ir_depth(record, codes.shape)
                    ir_verified = True
                except (ValueError, KeyError, TypeError):
                    pass
            display_hashes = {}
            if ir_verified:
                display_hashes['infrared.npy'] = sha256(ir_file)
                expected_ir = report.get('source_sha256', {}).get('infrared.npy')
                if expected_ir and expected_ir != display_hashes['infrared.npy']:
                    raise ValueError('原始 IR 已变化，请重新核对点云来源')
                ir = np.load(ir_file, allow_pickle=False)
                if ir.shape != codes.shape:
                    raise ValueError('原始 IR 与深度网格不一致')
                enhanced = enhanced_infrared(ir)
                low, high = np.percentile(ir, [1, 99])
                ir_image = np.clip((ir.astype(float)-low)/max(high-low, 1)*255, 0, 255).astype(np.uint8)
            else:
                ir_image = gray
                enhanced = enhanced_infrared(gray)
            base = cv2.cvtColor(ir_image, cv2.COLOR_GRAY2RGB)
            backgrounds = {'ir': base, 'ir_enhanced': enhanced, 'depth': depth_image}
            try:
                rgb_image, rgb_info = rgb_scene_on_depth(source, record, report, xyz, valid)
                backgrounds['rgb'] = rgb_image
                display_hashes['color.png'] = rgb_info['color_sha256']
            except (ValueError, KeyError, TypeError, OSError, OverflowError, cv2.error) as exc:
                rgb_info = {'available': False, 'reason': str(exc)}
            default_mode = 'rgb' if rgb_info['available'] else 'ir_enhanced'
            source_key = digest_json(hashes)
            ref_file = self.ws.state_dir/'alignment_references'/f'{source_key}.json'
            try:
                reference = read_json(ref_file) if ref_file.is_file() and not ref_file.is_symlink() else None
            except (ValueError, OSError):
                reference = None
            if not reference and result.get('has_alignment'):
                reference = read_json(result['folder']/'alignment.json')
            if reference and (reference.get('raw_sha256') != hashes or reference.get('version') != VERSION):
                reference = None
            token = secrets.token_hex(16)
            ctx = dict(token=token, dataset_id=self.ws.dataset_id, frame_id=row['id'], row=row,
                       folder=result['folder'], report=report, source=source, raw_sha256=hashes,
                       display_sha256=display_hashes,
                       source_root=capture_root(source, row['relative_dir']),
                       source_key=source_key, reference=reference, xyz=xyz, valid=valid, fg=fg,
                       profile=stream['profile'], base=backgrounds[default_mode], cloud=cloud,
                       backgrounds=backgrounds, default_display_mode=default_mode,
                       input_cloud_sha256=sha256(result['folder']/'points.npz'),
                       input_report_sha256=sha256(result['folder']/'pointcloud.json'))
            self.contexts[token] = ctx
            while len(self.contexts) > 3:
                self.contexts.pop(next(iter(self.contexts)))
            if payload.get('source_root'):
                self.ws.settings.setdefault('alignment_sources', {})[str(self.ws.results_root)] = str(Path(payload['source_root']).expanduser().resolve())
                self.ws.remember()
            return {'context_id': token, 'width': w, 'height': h, 'source_dir': str(source),
                    'draft_binding': digest_json({'raw': hashes, 'cloud': ctx['input_cloud_sha256'], 'report': ctx['input_report_sha256']}),
                    'relative_dir': row['relative_dir'], 'pig_id': row['pig_id'],
                    'capture_id': row['capture_id'],
                    'camera': report.get('camera', {}), 'input_mode': report.get('input_mode', 'infrared'),
                    'reviewed': report.get('reviewed', False), 'reference': reference,
                    'saved_alignment': read_json(result['folder']/'alignment.json') if result.get('has_alignment') else None,
                    'alignment_cached': result.get('alignment_cached', False),
                    'saved_check_image': data_image((result['folder']/'alignment_check.png').read_bytes()) if result.get('has_alignment') else None,
                    'infrared_verified': ir_verified,
                    'default_display_mode': default_mode, 'rgb_display': rgb_info,
                    'foreground_overlay': data_image(encoded_png(foreground_overlay(fg))),
                    'foreground_bounds': foreground_bounds,
                    'infrared_image': data_image(encoded_png(base)),
                    'infrared_enhanced_image': data_image(encoded_png(enhanced)),
                    'rgb_image': data_image(encoded_png(backgrounds['rgb'])) if rgb_info['available'] else None,
                    'depth_image': data_image(encoded_png(depth_image))}

    def _context(self, payload):
        self._workspace_result(payload)
        ctx = self.contexts.get(payload.get('context_id'))
        if not ctx or ctx['dataset_id'] != self.ws.dataset_id or ctx['frame_id'] != payload.get('id'):
            raise ValueError('对齐页面已过期，请重新载入原始场景')
        for n, expected in {**ctx['raw_sha256'], **ctx['display_sha256']}.items():
            if sha256(ctx['source']/n) != expected:
                raise ValueError('原始数据已变化，请重新生成点云并载入')
        if (sha256(ctx['folder']/'points.npz') != ctx['input_cloud_sha256']
                or sha256(ctx['folder']/'pointcloud.json') != ctx['input_report_sha256']):
            raise ValueError('当前点云已变化，请重新打开坐标对齐')
        return ctx

    def _anchor(self, ctx, value, label):
        a = np.asarray(value, float)
        h, w = ctx['valid'].shape
        if a.shape != (2,) or not np.isfinite(a).all() or not (0 <= a[0] < w and 0 <= a[1] < h):
            raise ValueError(f'{label}像素位置无效')
        u, v = np.rint(a).astype(int)
        u, v = min(u, w-1), min(v, h-1)
        if not ctx['valid'][v, u]:
            raise ValueError(f'{label}没有有效深度，请在同一部位重新选点')
        if not ctx['fg'][v, u]:
            raise ValueError(f'{label}不在当前点云可见范围，请核对分割或重新选点')
        distance = np.linalg.norm(ctx['cloud']['xyz_mm']-ctx['xyz'][v, u], axis=1).min()
        if distance > 35:
            raise ValueError(f'{label}深度与当前猪体点云相距超过 35 mm，请避开轮廓边缘、地面和遮挡物')
        return {'pixel_uv_native': a.tolist(), 'sample_pixel_uv_native': [int(u), int(v)],
                'xyz_camera_mm': ctx['xyz'][v, u].tolist()}

    def fit(self, payload):
        with self.ws.lock:
            ctx = self._context(payload)
            reused = payload.get('reuse_reference') is True
            if reused:
                ref = ctx['reference']
                if not ref:
                    raise ValueError('同次采集尚无已确认坐标参考')
                frame, annotations, stats = ref['frame'], ref['annotations'], ref['ground_quality']
            else:
                polygon = np.asarray(payload.get('ground_polygon'), float)
                h, w = ctx['valid'].shape
                if (polygon.ndim != 2 or polygon.shape[1:] != (2,) or not 3 <= len(polygon) <= 100
                        or not np.isfinite(polygon).all() or np.any(polygon < 0)
                        or np.any(polygon[:, 0] >= w) or np.any(polygon[:, 1] >= h)):
                    raise ValueError('请在原始图像内圈选至少 3 个地面顶点')
                region = np.zeros((h, w), np.uint8)
                cv2.fillPoly(region, [np.rint(polygon).astype(np.int32)], 1)
                selected = (region > 0) & ctx['valid'] & ~ctx['fg']
                n, d, inside, stats = fit_ground(ctx['xyz'][selected], float(payload.get('threshold_mm', 10)))
                tail = self._anchor(ctx, payload.get('tail_uv'), '尾根')
                neck = self._anchor(ctx, payload.get('neck_uv'), '颈根')
                frame = body_frame(n, d, tail['xyz_camera_mm'], neck['xyz_camera_mm'])
                annotations = {'pixel_coordinate_space': 'depth_native', 'ground_polygon': polygon.tolist(),
                               'tail': tail, 'neck': neck}
            body = transform_points(ctx['cloud']['xyz_mm'], frame).astype(np.float32)
            warnings = []
            if not ctx['report'].get('reviewed'):
                warnings.append('分割仍为初稿；坐标确认不会把分割改为已确认，后续体尺仍需复核躯干边界。')
            below = float((body[:, 2] < -20).mean())
            if below > .02:
                warnings.append(f'{below:.1%} 的点低于地面 20 mm，请检查地面拟合和分割背景。')
            display_mode = payload.get('display_mode', ctx['default_display_mode'])
            if display_mode not in ctx['backgrounds']:
                raise ValueError('所选场景底图不可用，请切换显示方式')
            brightness = float(payload.get('display_brightness', 1))
            if not np.isfinite(brightness) or not .5 <= brightness <= 2.5:
                raise ValueError('显示亮度需在 0.5–2.5 之间')
            record = {'version': VERSION, 'coordinate_system': 'ground_body', 'unit': 'mm',
                      'axes': {'X': 'tail_root_to_neck_root_on_ground', 'Y': 'pig_left_right_handed', 'Z': 'ground_normal_toward_pig'},
                      'origin': 'tail_root_projected_to_ground', 'frame': frame,
                      'annotations': annotations, 'ground_quality': stats,
                      'raw_sha256': ctx['raw_sha256'], 'source_key': ctx['source_key'],
                      'annotation_image_sha256': ctx['display_sha256'],
                      'check_image_display_mode': display_mode,
                      'check_image_brightness': brightness,
                      'input_cloud_sha256': ctx['input_cloud_sha256'], 'input_report_sha256': ctx['input_report_sha256'],
                      'input_mode': ctx['report'].get('input_mode', 'infrared'), 'camera': ctx['report'].get('camera', {}),
                      'pig_id': ctx['row']['pig_id'], 'capture_id': ctx['row']['capture_id'],
                      'relative_dir': ctx['row']['relative_dir'], 'source_dir': str(ctx['source']),
                      'segmentation_reviewed': ctx['report'].get('reviewed', False),
                      'below_ground_ratio_20mm': below, 'warnings': warnings,
                      'ground_reference': 'manually_selected_local_plane; vertical only if floor is level',
                      'reference_id': digest_json({'source': ctx['source_key'], 'frame': frame}),
                      'reused_reference': reused, 'confirmed': False}
            check = self._check_image(ctx, record)
            candidate_id = secrets.token_hex(16)
            ctx['candidate'] = {'id': candidate_id, 'record': record, 'body': body, 'check': check}
            return {'candidate_id': candidate_id, 'record': record, 'check_image': data_image(check)}

    def _check_image(self, ctx, record):
        image = np.clip(ctx['backgrounds'][record['check_image_display_mode']].astype(float)
                        * record['check_image_brightness'], 0, 255).astype(np.uint8)
        a, f = record['annotations'], record['frame']
        polygon = np.rint(a['ground_polygon']).astype(np.int32)
        cv2.polylines(image, [polygon], True, (220, 165, 30), 2)
        m = np.zeros(image.shape[:2], np.uint8)
        cv2.fillPoly(m, [polygon], 1)
        n = np.asarray(f['plane_normal_camera'])
        residual = np.abs(ctx['xyz'] @ n + f['plane_offset_mm'])
        good = (m > 0) & ctx['valid'] & ~ctx['fg'] & (residual <= record['ground_quality']['threshold_mm'])
        image[good] = (.5*image[good]+.5*np.array([30, 210, 120])).astype(np.uint8)
        origin = np.asarray(f['origin_camera_mm'])
        r = np.asarray(f['rotation_camera_to_body'])
        axes = np.vstack([origin, origin+250*r])
        uv = project(axes, ctx['profile'])
        if np.isfinite(uv).all() and np.all(axes[:, 2] > 0):
            p = [tuple(np.clip(np.rint(x), -10000, 10000).astype(int)) for x in uv]
            for i, color in enumerate([(220, 65, 60), (35, 155, 85), (60, 105, 230)]):
                cv2.arrowedLine(image, p[0], p[i+1], color, 2, tipLength=.12)
                cv2.putText(image, ['X','Y','Z'][i], p[i+1], cv2.FONT_HERSHEY_SIMPLEX, .65, color, 2)
        for key, color in [('tail', (255, 120, 30)), ('neck', (180, 60, 220))]:
            p = tuple(np.rint(a[key]['pixel_uv_native']).astype(int))
            cv2.circle(image, p, 5, color, 2)
            cv2.putText(image, key, (p[0]+7, p[1]-7), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1)
        return encoded_png(image)

    def preview(self, payload):
        with self.ws.lock:
            ctx = self._context(payload)
            candidate = ctx.get('candidate')
            if not candidate or candidate['id'] != payload.get('candidate_id'):
                raise ValueError('请先重新计算坐标对齐')
            return render_body_view({**ctx['cloud'], 'xyz_mm': candidate['body']}, candidate['record'])

    def save(self, payload):
        return self._save(payload, cached=False)

    def confirm(self, payload):
        """Confirm alignment and keep its cloud together until a batch export."""
        return self._save(payload, cached=True)

    def _save(self, payload, cached):
        from .workspace import (directory, folder_name, safe_copy_tree, atomic_json,
                                write_cloud_review, cloud_review_record)
        with self.ws.lock:
            ctx = self._context(payload)
            candidate = ctx.get('candidate')
            if not candidate or candidate['id'] != payload.get('candidate_id'):
                raise ValueError('对齐结果已改变，请重新预览后保存')
            legacy_confirmed = payload.get('floor_confirmed') is True and payload.get('anchors_confirmed') is True
            if cached:
                if payload.get('alignment_confirmed') is not True and not legacy_confirmed:
                    raise ValueError('请检查地面、端点与坐标方向，再点击“确认对齐并保留”')
            elif not legacy_confirmed:
                raise ValueError('请核对地面平面与颈根、尾根，再勾选两项确认')
            relative_dir = ctx['row']['relative_dir']
            result = self.ws.results[relative_dir]
            review = cloud_review_record(result, relative_dir, 'keep') if cached else result.get('cloud_review')
            if cached:
                parent = self.ws.state_dir/'alignment_cache'
                if parent.is_symlink():
                    raise ValueError('工作台暂存目录不能使用符号链接')
                parent.mkdir(parents=True, exist_ok=True)
                target = parent/secrets.token_hex(16)
            else:
                parent = directory(payload.get('parent'))
                target = parent/folder_name(payload.get('name'))
            protected = [ctx['source_root'], ctx['source'], ctx['folder'], self.ws.results_root, self.ws.source,
                         self.ws.segmentation, None if cached else self.ws.state_dir]
            protected.extend(r['folder'] for r in self.ws.results.values())
            for p in protected:
                if p: independent_paths(p, target)
            # Protect raw/mask roots recorded by a portable result as well.
            for name in ['source_dir', 'segmentation_dir']:
                if ctx['report'].get(name):
                    independent_paths(capture_root(Path(ctx['report'][name]), ctx['row']['relative_dir']), target)
            if target.exists() or target.is_symlink():
                raise ValueError('成果文件夹已存在，请换一个名称')
            stage = Path(tempfile.mkdtemp(prefix='.body-alignment-', dir=parent))
            record = {**candidate['record'], 'confirmed': True,
                      'created_at': datetime.now(timezone.utc).isoformat()}
            try:
                dest = within(stage, ctx['row']['relative_dir'])
                dest.parent.mkdir(parents=True, exist_ok=True)
                safe_copy_tree(ctx['folder'], dest)
                write_cloud_review(dest, {**result, 'cloud_review': review})
                # The copy may contain an older aligned view. Replace sidecars only in this new export.
                body_payload = {**ctx['cloud'], 'xyz_mm': candidate['body'],
                                'source_point_index': np.arange(len(candidate['body']), dtype=np.int64)}
                np.savez_compressed(dest/'body_points.npz', **body_payload)
                record['body_points_sha256'] = sha256(dest/'body_points.npz')
                color = ctx['cloud']['rgb'].copy()
                color[~ctx['cloud']['rgb_valid']] = [110, 120, 140]
                if record['input_mode'] == 'infrared': color = display_ir(ctx['cloud']['infrared_raw'])
                rows = np.empty(len(candidate['body']), dtype=[('x','<f4'),('y','<f4'),('z','<f4'),('red','u1'),('green','u1'),('blue','u1')])
                for i, k in enumerate(['x','y','z']): rows[k] = candidate['body'][:, i]
                for i, k in enumerate(['red','green','blue']): rows[k] = color[:, i]
                header = ('ply\nformat binary_little_endian 1.0\ncomment coordinates ground_body unit mm\n'
                          f'element vertex {len(rows)}\nproperty float x\nproperty float y\nproperty float z\n'
                          'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
                (dest/'cloud_body_mm.ply').write_bytes(header.encode()+rows.tobytes())
                write_json(dest/'alignment.json', record)
                (dest/'alignment_check.png').write_bytes(candidate['check'])
                (dest/'body_preview.html').write_text(render_body_view(body_payload, record), encoding='utf-8')
                write_json(stage/'pointcloud_dataset.json', {'schema_version': 1, 'coordinate_system': 'native_depth_camera',
                    'unit': 'mm', 'aligned_coordinate_system': 'ground_body',
                    'frames': [{**ctx['row'], 'has_alignment': True, 'alignment_reference_id': record['reference_id'],
                                'cloud_decision': review['decision'] if review else 'pending'}]})
                rel = quote(ctx['row']['relative_dir'], safe='/')
                (stage/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>坐标对齐成果</title>'
                    '<style>body{font:16px system-ui;margin:40px;line-height:2}a{color:#247767}</style>'
                    '<h1>地面与猪体坐标成果</h1><p>'+escape(ctx['row']['relative_dir'])+'</p>'
                    f'<p><a href="{rel}/body_preview.html">查看猪体坐标点云</a> · '
                    f'<a href="{rel}/preview.html">查看原相机坐标点云</a></p>'
                    '<p>相机点云和猪体坐标点云分别保存，也可在点云工作台打开本成果目录。</p>', encoding='utf-8')
                self._context(payload)
                if target.exists() or target.is_symlink(): raise ValueError('成果目录已存在，请换名重试')
                stage.rename(target)
                stage = None
            finally:
                if stage: shutil.rmtree(stage, ignore_errors=True)
            warning = ''
            if not cached:
                self.ws.last_output = target
                self.ws.settings.setdefault('alignment_sources', {})[str(target)] = str(ctx['source_root'])
                self.ws.settings['export_parent'] = str(parent)
            attached = cached or payload.get('attach_to_workspace') is True
            previous_settings = copy.deepcopy(self.ws.settings) if cached else None
            previous_result = self.ws.results[ctx['row']['relative_dir']]
            if attached:
                self.ws.attach_alignment(ctx, target, cached=cached)
            if cached:
                self.ws.settings.setdefault('cloud_reviews', {}).setdefault(str(self.ws.results_root), {})[relative_dir] = review
                self.ws.results[relative_dir]['cloud_review'] = review
            try:
                self.ws.remember()
            except OSError:
                if cached:
                    self.ws.settings = previous_settings
                    self.ws.results[ctx['row']['relative_dir']] = previous_result
                    shutil.rmtree(target, ignore_errors=True)
                    raise ValueError('本条确认未能持久保存，请检查工作台暂存目录后重试')
                warning = (warning+' ' if warning else '')+'成果已保存，但未能记住导出路径。'
            reference = {k: record[k] for k in ['version', 'source_key', 'raw_sha256', 'reference_id', 'frame', 'annotations', 'ground_quality', 'created_at']}
            try:
                atomic_json(self.ws.state_dir/'alignment_references'/f'{ctx["source_key"]}.json', reference)
                ctx['reference'] = reference
            except OSError:
                warning += ' 成果已保存，但未能缓存同次采集坐标参考；可从 alignment.json 恢复。'
            return {'output': str(target), 'relative_dir': ctx['row']['relative_dir'],
                    'reference_id': record['reference_id'], 'warning': warning, 'attached': attached, 'cached': cached,
                    'cloud_decision': review['decision'] if review else 'pending'}
