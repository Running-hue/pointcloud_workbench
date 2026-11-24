"""Cross-check SDK aligned depth against independently projected native measurements.

This checks recorded geometry, not physical calibration accuracy. No parameters
are fitted from the animal silhouette, and no depth measurements are changed.
"""
from .core import np, cv2, Image, backproject, project, discontinuities


AUDIT_VERSION = 'native-to-rgb-crosscheck-v1'


def rgb_timing(record, maximum_us):
    if not np.isfinite(maximum_us) or maximum_us < 0:
        raise ValueError('RGB／深度最大时间差必须为非负有限数值')
    selected = record['selected_capture']
    delta = int(selected['timestamp_deltas']['color_minus_depth_device_us'])
    identities = [selected['streams'][name].get('identity', {}) for name in ('color', 'depth')]
    stamps = [identity.get('device_timestamp_us') for identity in identities]
    if all(value is not None for value in stamps):
        if int(stamps[0])-int(stamps[1]) != delta:
            raise ValueError('RGB／深度帧时间戳与记录的时间差不一致，无法确认掩码配对')
    if abs(delta) > maximum_us:
        raise ValueError(f'RGB／深度不同步（{delta} μs，允许 ±{maximum_us:g} μs）；'
                         'RGB 掩码不能用于该深度帧，请跳过此采集')
    return delta


def depth_to_color(record):
    ext = record['actual_profile_extrinsics']['depth_to_color']
    rotation = np.asarray(ext['rotation_row_major'], float)
    translation = np.asarray(ext['translation_mm'], float)
    if rotation.shape != (9,) or translation.shape != (3,):
        raise ValueError('深度到 RGB 外参尺寸或数值无效')
    rotation = rotation.reshape(3, 3)
    if not np.isfinite(rotation).all() or not np.isfinite(translation).all():
        raise ValueError('深度到 RGB 外参尺寸或数值无效')
    orthogonality = float(np.linalg.norm(rotation.T@rotation-np.eye(3)))
    determinant = float(np.linalg.det(rotation))
    if orthogonality > .05 or abs(determinant-1) > .05:
        raise ValueError('深度到 RGB 外参不是可用的相机旋转，无法保持原始深度相机坐标')
    return rotation, translation, orthogonality, determinant


def native_projection(source, record, geometry):
    stream = record['selected_capture']['streams']['depth']
    codes = np.load(source/'depth_sensor_code_u16.npy', allow_pickle=False)
    scale = float(stream['value_scale_mm_per_code'])
    xyz, valid, _ = backproject(codes, scale, stream['profile'])
    rotation, translation, _, _ = depth_to_color(record)
    color_xyz = xyz[valid]@rotation.T+translation
    uv = project(color_xyz, geometry['depth_profile'])
    height, width = geometry['shape_hw']
    inside = ((color_xyz[:, 2] > 0) & np.isfinite(uv).all(axis=1)
              & (uv[:, 0] >= 0) & (uv[:, 0] <= width-1)
              & (uv[:, 1] >= 0) & (uv[:, 1] <= height-1))
    ij = np.rint(uv[inside]).astype(np.int32)
    # Reject both sides of native depth steps; dilation also avoids the holes
    # adjacent to them. Nearest-pixel comparisons at an occlusion are ambiguous.
    native_edges = discontinuities(codes.astype(float)*scale) | (codes == 0)
    native_edges = cv2.dilate(native_edges.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return color_xyz[inside], uv[inside], ij, native_edges[valid][inside], int(valid.sum())


def _residuals(z, observed, use):
    if not use.any():
        return {'compared_points': 0, 'agreement_ratio': None, 'absolute_z_residual_mm': None}
    error = np.abs(z[use]-observed[use])
    # Rasterization and SDK depth quantization prevent exact equality. These
    # diagnostic tolerances are not a guarantee of physical accuracy.
    tolerance = np.maximum(5., .01*z[use])
    return {'compared_points': int(use.sum()),
            'agreement_ratio': float((error <= tolerance).mean()),
            'absolute_z_residual_mm': dict(zip(('p50', 'p95', 'max'), np.percentile(error, [50, 95, 100]).tolist()))}


def audit_rgb_registration(source, record, geometry, mask, aligned_codes, scale):
    audit = {'version': AUDIT_VERSION, 'status': 'unavailable',
             'method': 'native_depth_unproject_then_recorded_depth_to_color_then_aligned_profile_project',
             'scope': 'recorded_geometry_consistency_only; not independent physical calibration',
             'tolerance': 'absolute color-camera Z residual <= max(5 mm, 1% predicted Z)',
             'diagnostic_agreement_ratio': .90,
             'physical_accuracy_verified': False, 'warnings': []}
    if not (source/'depth_sensor_code_u16.npy').is_file():
        audit['warnings'].append('缺少原始深度码，未完成原始深度与 RGB 对齐深度的交叉核对')
        return audit
    points, uv, ij, native_edges, total = native_projection(source, record, geometry)
    z = aligned_codes.astype(float)*scale
    observed = z[ij[:, 1], ij[:, 0]]
    aligned_edges = discontinuities(z) | (z <= 0)
    aligned_edges = cv2.dilate(aligned_edges.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    stable = ~native_edges & ~aligned_edges[ij[:, 1], ij[:, 0]] & (observed > 0)
    # A full-scene score alone could conceal problems on the animal itself.
    interior = cv2.erode((mask > 0).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    in_mask = interior[ij[:, 1], ij[:, 0]]
    scene = _residuals(points[:, 2], observed, stable)
    foreground = _residuals(points[:, 2], observed, stable & in_mask)
    original_uv = project(points, geometry['source_profile'])
    offsets = np.linalg.norm(original_uv-uv, axis=1)
    offsets = offsets[np.isfinite(offsets)]
    audit.update(native_valid_points=total, native_points_in_rgb=len(points),
                 scene=scene, masked_surface=foreground,
                 native_rgb_to_mask_grid_offset_px=(dict(zip(('p50', 'p95', 'max'), np.percentile(offsets, [50, 95, 100]).tolist())) if len(offsets) else None),
                 check_image='rgb_registration_check.png')
    enough = scene['compared_points'] >= 100 and foreground['compared_points'] >= 100
    consistent = enough and min(scene['agreement_ratio'], foreground['agreement_ratio']) >= .90
    audit['status'] = 'consistent' if consistent else 'review_required'
    if not enough:
        audit['warnings'].append('配准交叉核对的稳定深度点不足，请查看检查图，不能据此判断标定准确')
    elif not consistent:
        audit['warnings'].append('原始深度与 RGB 对齐深度的交叉核对存在较大差异，请查看配准检查图并按需标记跳过；该差异也可能包含遮挡和重采样影响')
    return audit


def registration_image(source, record, geometry, mask):
    """Original RGB: cyan native depth steps, yellow calibrated mask boundary."""
    with Image.open(source/'color.png') as im:
        image = np.array(im.convert('RGB'))
    points, _, _, edges, _ = native_projection(source, record, geometry)
    uv = project(points[edges], geometry['source_profile'])
    h, w = image.shape[:2]

    def paint(pixels, color):
        good = (np.isfinite(pixels).all(axis=1) & (pixels[:, 0] >= 0) & (pixels[:, 0] <= w-1)
                & (pixels[:, 1] >= 0) & (pixels[:, 1] <= h-1))
        ij = np.rint(pixels[good]).astype(int)
        layer = np.zeros((h, w), np.uint8)
        layer[ij[:, 1], ij[:, 0]] = 255
        image[cv2.dilate(layer, np.ones((3, 3), np.uint8)) > 0] = color

    paint(uv, [20, 220, 245])
    # The mask was drawn on the calibrated display grid. Map its boundary rays
    # back to the original distorted RGB image instead of resizing the mask.
    boundary = (mask > 0) & ~(cv2.erode(mask, np.ones((3, 3), np.uint8),
                                      borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0)
    yy, xx = np.nonzero(boundary)
    from .geometry import camera, normalized_rays
    matrix, distortion, _ = camera(geometry['depth_profile'])
    rays = normalized_rays(np.column_stack([xx, yy]), matrix, distortion)
    paint(project(np.column_stack([rays, np.ones(len(rays))]), geometry['source_profile']), [255, 215, 30])
    cv2.rectangle(image, (0, 0), (min(w-1, 650), 50), (10, 20, 30), -1)
    cv2.putText(image, 'CYAN: native depth edges | YELLOW: RGB mask', (10, 22), cv2.FONT_HERSHEY_SIMPLEX, .55, (235, 240, 245), 1)
    cv2.putText(image, 'Recorded calibration check; physical accuracy unverified', (10, 42), cv2.FONT_HERSHEY_SIMPLEX, .5, (235, 240, 245), 1)
    return image
