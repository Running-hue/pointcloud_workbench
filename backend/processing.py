"""Reversible point edits and explicitly reviewed visible-surface measurements.

All indices refer to the immutable, full-density points.npz. Preview sampling is
never used for selecting, filtering, measuring, or exporting points.
"""
from __future__ import annotations

import copy
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import tempfile

from .core import np, cv2, Image, read_json, write_json, sha256, display_ir, ply_bytes, input_mode
from .alignment import alignment_status, transform_points
from .paths import PAGES_DIR, JS_DIR, CSS_DIR
from . import features as feature_math
from .workflow import workflow_state

VERSION = 'reversible-point-edits-v1'
EDIT_FILES = {'pointcloud_cleaning.json', 'point_edits.npz'}
PROCESSING_FILES = EDIT_FILES | feature_math.FEATURE_FILES | {'cleaned_points.npz', 'cleaned_body_points.npz',
    'cloud_cleaned_mm.ply', 'cloud_cleaned_body_mm.ply', 'slice_measurements.csv',
    'measurement_summary.json', 'processing_preview.html'}


def now():
    return datetime.now(timezone.utc).isoformat()


def mask_digest(removed):
    return hashlib.sha256(np.packbits(removed).tobytes()).hexdigest()


MEASUREMENT_METHOD = 'local_normal_profiles'
# The confirmed alignment supplies both references. Capture screening handles
# unsuitable endpoints/ground; these are no longer separate user decisions.
REFERENCE_CONDITIONS = {'endpoints': 'complete', 'ground': 'reliable'}


def profile_review_summary(features):
    """Use the same evaluated profiles as the inspection panel and exports."""
    return {'measurement_method': MEASUREMENT_METHOD,
            'feature_version': feature_math.VERSION,
            'slice_count': len(features['profiles']),
            'usable_slices': features['quality']['reviewed_profiles'],
            'unusable_slices': sum(row['status'] == 'unusable' for row in features['profiles']),
            'measurement_ready': features['quality']['all_profiles_reviewed']}


def scalar(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}无效')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}无效') from None
    if not np.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label}应在 {low:g}–{high:g} 之间')
    return value


def detect_candidates(points, method='far', margin_mm=300., neighbors=8, neighbor_mm=50.):
    """Only propose candidates; do not alter points or label them as known errors."""
    p = np.asarray(points, np.float32)
    if len(p) < 3:
        raise ValueError('有效点过少，无法检测疑似飞点')
    if method not in {'far', 'neighbor', 'both'}:
        raise ValueError('请选择远点候选、邻域稀疏候选或两者合并')
    margin_mm = scalar(margin_mm, 50, 2000, '主体外扩距离 / mm')
    neighbors_value = scalar(neighbors, 3, 64, '邻居数量')
    if neighbors_value != int(neighbors_value):
        raise ValueError('邻居数量须为整数')
    neighbors = min(int(neighbors_value), len(p)-1)
    neighbor_mm = scalar(neighbor_mm, 5, 500, '邻域距离下限 / mm')
    far = np.zeros(len(p), bool)
    sparse = far.copy()
    detail = {'method': method, 'margin_mm': margin_mm, 'neighbors': neighbors,
              'neighbor_mm': neighbor_mm, 'candidate_only': True}
    if method in {'far', 'both'}:
        lo, hi = np.percentile(p, [1, 99], axis=0)
        far = np.any((p < lo-margin_mm) | (p > hi+margin_mm), axis=1)
        detail['robust_bounds_mm'] = [lo.tolist(), hi.tolist()]
    if method in {'neighbor', 'both'}:
        # Approximate kNN is used for proposals only, with a physical lower limit
        # and a robust global threshold. RGB grid density varies between routes.
        index = cv2.flann_Index(np.ascontiguousarray(p), dict(algorithm=1, trees=4))
        distances = np.empty(len(p), np.float64)
        try:
            for start in range(0, len(p), 30000):
                _, squared = index.knnSearch(p[start:start+30000], neighbors+1, params={'checks': 128})
                distances[start:start+len(squared)] = np.sqrt(np.maximum(squared[:, -1], 0))
        finally:
            index.release()
        med = float(np.median(distances))
        mad = float(np.median(np.abs(distances-med)))
        threshold = max(neighbor_mm, med+6*1.4826*mad)
        sparse = distances > threshold
        detail.update(neighbor_threshold_mm=threshold, approximate_knn=True)
    return far | sparse, {**detail, 'far_count': int(far.sum()), 'sparse_count': int(sparse.sum())}


def screen_selection(points, view, rectangle):
    """Select ALL depths through a screen rectangle, using full-density points."""
    rotation = np.asarray(view.get('rotation'), float)
    center = np.asarray(view.get('center'), float)
    size = np.asarray(view.get('size'), float)
    rect = np.asarray(rectangle, float)
    scale = scalar(view.get('scale'), 1e-8, 1e6, '视图比例')
    if (rotation.shape != (3, 3) or center.shape != (3,) or size.shape != (2,)
            or rect.shape != (4,) or not all(np.isfinite(a).all() for a in (rotation, center, size, rect))
            or np.max(np.abs(center)) > 1e9 or np.any(size <= 0) or np.any(size > 20000)
            or not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5)):
        raise ValueError('视图或框选范围无效，请重新框选')
    x0, x1 = sorted(rect[[0, 2]])
    y0, y1 = sorted(rect[[1, 3]])
    if x0 < 0 or y0 < 0 or x1 > size[0]+1 or y1 > size[1]+1:
        raise ValueError('请在点云画布内框选')
    q = (np.asarray(points)-center) @ rotation[:2].T
    q = q*scale + size/2
    return (q[:, 0] >= x0) & (q[:, 0] <= x1) & (q[:, 1] >= y0) & (q[:, 1] <= y1)


def measure_slices(points, length_mm, thickness_mm=15., min_points=20, reviews=None):
    """Visible lateral span per longitudinal band; no completion or smoothing."""
    length = scalar(length_mm, 50, 5000, '端点地面投影长度 / mm')
    thickness = scalar(thickness_mm, 5, 100, '切片厚度 / mm')
    min_value = scalar(min_points, 3, 500, '切片最少点数')
    if min_value != int(min_value):
        raise ValueError('切片最少点数须为整数')
    minimum = int(min_value)
    p = np.asarray(points, float)
    reviews = reviews or {}
    # Explicit thirds prevent a slice straddling two regions from contributing
    # its full width to both. The final band in each third can be thinner.
    edges = [0.]
    regions = []
    for region in range(3):
        start, end = length*region/3, length*(region+1)/3
        local = np.arange(start+thickness, end-1e-7, thickness).tolist()+[end]
        regions.extend([region]*len(local))
        edges.extend(local)
    edges = np.asarray(edges)
    in_range = (p[:, 0] >= 0) & (p[:, 0] <= length)
    bins = np.minimum(np.searchsorted(edges, p[:, 0], side='right')-1, len(edges)-2)
    rows = []
    for i, region in enumerate(regions):
        q = p[in_range & (bins == i)]
        sufficient = len(q) >= minimum
        status = reviews.get(str(i), 'pending')
        if status not in {'pending', 'usable', 'unusable'} or (status == 'usable' and not sufficient):
            status = 'pending'
        lo, hi = (float(q[:, 1].min()), float(q[:, 1].max())) if len(q) else (None, None)
        width = hi-lo if sufficient else None
        rows.append({'index': i, 'region': region, 'x_start_mm': float(edges[i]),
            'x_end_mm': float(edges[i+1]), 'points': len(q), 'y_min_mm': lo, 'y_max_mm': hi,
            'candidate_width_mm': width, 'status': status,
            'confirmed_width_mm': width if status == 'usable' else None,
            'point_support_sufficient': sufficient})
    summary = {'length_ground_projection_mm': length, 'thickness_mm': thickness,
        'min_points': minimum, 'definition': 'visible lateral envelope; not circumference or closed-body volume',
        'slice_count': len(rows), 'usable_slices': sum(r['status'] == 'usable' for r in rows),
        'unusable_slices': sum(r['status'] == 'unusable' for r in rows),
        'regions': [], 'fixed_widths': {}}
    for region in range(3):
        part = [r for r in rows if r['region'] == region]
        widths = [r['candidate_width_mm'] for r in part if r['candidate_width_mm'] is not None]
        complete = all(r['status'] == 'usable' for r in part)
        summary['regions'].append({'region': region,
            'candidate_max_visible_width_mm': max(widths) if widths else None,
            'confirmed_max_visible_width_mm': max(widths) if complete else None,
            'all_slices_usable': complete})
    for label, ratio in [('W25', .25), ('W50', .5), ('W75', .75)]:
        i = min(int(np.searchsorted(edges, ratio*length, side='right')-1), len(rows)-1)
        row = rows[i]
        summary['fixed_widths'][label] = {'position_mm': ratio*length, 'band_index': i,
            'candidate_width_mm': row['candidate_width_mm'], 'confirmed_width_mm': row['confirmed_width_mm'],
            'definition': 'width of the band containing the specified longitudinal position'}
    area = sum(r['candidate_width_mm']*(r['x_end_mm']-r['x_start_mm'])
               for r in rows if r['candidate_width_mm'] is not None)
    summary['candidate_envelope_area_mm2'] = area
    summary['confirmed_envelope_area_mm2'] = area if all(r['status'] == 'usable' for r in rows) else None
    summary['area_definition'] = 'sum of visible band widths times band lengths; not true surface area'
    return rows, summary


class ProcessingService:
    def __init__(self, workspace):
        self.ws = workspace
        self.contexts = {}

    def restore(self):
        """Attach portable edits or state-cache edits only to identical inputs."""
        self.contexts.clear()
        links = self.ws.settings.get('point_processing', {}).get(str(self.ws.results_root), {})
        invalid = 0
        for rel, result in self.ws.results.items():
            saved = links.get(rel)
            folders = [Path(saved)] if isinstance(saved, str) else []
            folders.append(result['folder'])
            for index, folder in enumerate(folders):
                if not any((folder/name).exists() for name in EDIT_FILES):
                    if index == 0 and isinstance(saved, str):
                        invalid += 1
                    continue
                try:
                    record, removed, history = self._read(folder, result)
                    m = record.get('measurement', {})
                    # Rebuild display metadata from current profiles in memory.
                    # Historical straight-slice labels are never profile labels.
                    if result.get('has_alignment'):
                        alignment = read_json(result['folder']/'alignment.json')
                        valid = (m.get('reference_id') == alignment['reference_id']
                                 and m.get('cleaning_digest') == mask_digest(removed))
                        if valid and m.get('feature_reviews'):
                            with np.load(result['folder']/'points.npz', allow_pickle=False) as cloud:
                                body = transform_points(cloud['xyz_mm'][~removed], alignment['frame'])
                            features = feature_math.calculate(body, alignment['frame']['neck_tail_horizontal_span_mm'],
                                m.get('feature_settings'), REFERENCE_CONDITIONS, m.get('feature_reviews'))
                            record['measurement'] = {**m, 'review_summary': profile_review_summary(features)}
                        else:
                            record['measurement'] = {k: v for k, v in m.items() if k != 'review_summary'}
                    result['processing'] = {'folder': folder, 'record': record}
                    break
                except (ValueError, OSError, KeyError, TypeError, IndexError):
                    invalid += 1
        return invalid

    def keep_mask(self, result):
        if not result.get('processing'):
            return np.ones(result['report']['stats']['points'], bool)
        _, removed, _ = self._read(result['processing']['folder'], result)
        return ~removed

    def _read(self, folder, result):
        if folder.is_symlink() or any(not (folder/n).is_file() or (folder/n).is_symlink() for n in EDIT_FILES):
            raise ValueError('清理记录不完整或路径不合法')
        r = read_json(folder/'pointcloud_cleaning.json')
        if (r.get('version') != VERSION or r.get('input_points_sha256') != sha256(result['folder']/'points.npz')
                or r.get('input_report_sha256') != sha256(result['folder']/'pointcloud.json')
                or r.get('edits_sha256') != sha256(folder/'point_edits.npz')):
            raise ValueError('清理记录与原点云不匹配')
        with np.load(folder/'point_edits.npz', allow_pickle=False) as f:
            removed = f['removed'].astype(bool)
            flat, offsets = f['indices'], f['offsets']
        n = result['report']['stats']['points']
        ops = r['operations']
        cursor = r['history_cursor']
        if (removed.shape != (n,) or flat.ndim != 1 or flat.dtype.kind not in 'iu'
                or offsets.shape != (len(ops)+1,) or offsets.dtype.kind not in 'iu'
                or offsets[0] != 0 or offsets[-1] != len(flat) or np.any(np.diff(offsets) < 0)
                or (len(flat) and (flat.min() < 0 or flat.max() >= n))
                or type(cursor) is not int or not 0 <= cursor <= len(ops)):
            raise ValueError('清理索引无效')
        history = []
        replay = np.zeros(n, bool)
        for i, op in enumerate(ops):
            if type(op.get('removed')) is not bool:
                raise ValueError('清理操作无效')
            ids = flat[offsets[i]:offsets[i+1]].astype(np.int64)
            history.append({**op, 'indices': ids})
            if i < cursor:
                replay[ids] = op['removed']
        if (not np.array_equal(replay, removed) or mask_digest(removed) != r['cleaning_digest'] or removed.all()
                or r.get('original_points') != n or r.get('removed_points') != int(removed.sum())):
            raise ValueError('清理历史与当前版本不一致')
        return r, removed, history

    def _result(self, payload):
        self.ws.idle()
        if payload.get('dataset_id') != self.ws.dataset_id:
            raise ValueError('目录已切换，请重新打开清理与测量')
        row = self.ws.row(payload.get('id'))
        result = self.ws.results.get(row['relative_dir'])
        if not result:
            raise ValueError('请先生成或打开点云')
        return row, result

    def _ctx(self, payload, check_revision=True):
        row, result = self._result(payload)
        ctx = self.contexts.get(row['relative_dir'])
        if not ctx or payload.get('context_id') != ctx['id']:
            raise ValueError('清理页面已过期，请重新载入当前采集')
        if (ctx['points_hash'] != sha256(result['folder']/'points.npz')
                or ctx['report_hash'] != sha256(result['folder']/'pointcloud.json')):
            self.contexts.pop(row['relative_dir'], None)
            raise ValueError('原点云已变化，请重新打开成果目录')
        current = result.get('processing', {}).get('record', {}).get('revision', '')
        if check_revision and (payload.get('revision', '') != current or ctx['revision'] != current):
            raise ValueError('清理版本已变化，请刷新页面后重试')
        # A newly confirmed reference changes slice coordinates and invalidates
        # slice reviews; point removal indices remain valid.
        aligned = alignment_status(result['folder'])
        ctx['alignment'] = read_json(result['folder']/'alignment.json') if aligned else None
        return ctx, result

    def context(self, payload):
        with self.ws.lock:
            row, result = self._result(payload)
            points_hash = sha256(result['folder']/'points.npz')
            report_hash = sha256(result['folder']/'pointcloud.json')
            with np.load(result['folder']/'points.npz', allow_pickle=False) as saved:
                cloud = {k: saved[k].copy() for k in saved.files}
            p = cloud['xyz_mm']
            if p.ndim != 2 or p.shape[1] != 3 or not len(p) or not np.isfinite(p).all():
                raise ValueError('点云坐标无效')
            n = len(p)
            if n != result['report']['stats']['points']:
                raise ValueError('原点云点数与生成记录不一致，请重新核对成果')
            if any(value.ndim == 0 or len(value) != n for value in cloud.values()):
                raise ValueError('逐点属性长度不一致，无法安全清理')
            ctx = {'id': secrets.token_hex(16), 'row': row, 'cloud': cloud,
                'report': result['report'], 'points_hash': points_hash, 'report_hash': report_hash,
                'removed': np.zeros(n, bool), 'history': [], 'cursor': 0, 'revision': '',
                'measurement': {}, 'candidate': None,
                'alignment': read_json(result['folder']/'alignment.json') if alignment_status(result['folder']) else None}
            if result.get('processing'):
                r, ctx['removed'], ctx['history'] = self._read(result['processing']['folder'], result)
                ctx.update(cursor=r['history_cursor'], revision=r['revision'], measurement=r.get('measurement', {}))
            # Cache only a few full-density captures; all edits are durable.
            self.contexts.pop(row['relative_dir'], None)
            while len(self.contexts) >= 3:
                self.contexts.pop(next(iter(self.contexts)))
            self.contexts[row['relative_dir']] = ctx
            return self._response(ctx)

    def _measurement(self, ctx):
        saved = ctx['measurement']
        reference = ctx['alignment']['reference_id'] if ctx['alignment'] else None
        valid = saved.get('reference_id') == reference and saved.get('cleaning_digest') == mask_digest(ctx['removed'])
        return {**saved, 'slice_reviews': saved.get('slice_reviews', {}) if valid else {},
                'feature_reviews': saved.get('feature_reviews', {}) if valid else {},
                'feature_quality': dict(REFERENCE_CONDITIONS),
                'reference_id': reference, 'cleaning_digest': mask_digest(ctx['removed'])}

    def _coordinates(self, ctx, body=False):
        points = ctx['cloud']['xyz_mm']
        if body:
            if not ctx['alignment']:
                raise ValueError('请先在“坐标对齐”中确认地面和端点')
            return transform_points(points, ctx['alignment']['frame'])
        return points

    def _response(self, ctx):
        n = len(ctx['removed'])
        selected = ctx['candidate']['indices'] if ctx['candidate'] else np.empty(0, np.int64)
        sample = np.random.default_rng(7).choice(n, min(n, 22000), replace=False)
        # A few far points must remain visible even if uniform display sampling
        # misses them. All candidate operations still use their full index set.
        extra = selected if len(selected) <= 7000 else selected[np.linspace(0, len(selected)-1, 7000).astype(int)]
        removed_ids = np.flatnonzero(ctx['removed'])
        removed_sample = removed_ids if len(removed_ids) <= 5000 else removed_ids[np.linspace(0, len(removed_ids)-1, 5000).astype(int)]
        sample = np.unique(np.concatenate([sample, extra, removed_sample]))
        cloud = ctx['cloud']
        colors = cloud['rgb'][sample].copy()
        if input_mode(ctx['report']) == 'infrared':
            colors = display_ir(cloud['infrared_raw'])[sample]
        else:
            colors[~cloud['rgb_valid'][sample]] = [110, 120, 140]
        points = cloud['xyz_mm'][sample]
        measurement = self._measurement(ctx)
        return {'context_id': ctx['id'], 'revision': ctx['revision'],
            'total': n, 'removed_count': int(ctx['removed'].sum()), 'kept_count': int((~ctx['removed']).sum()),
            'can_undo': ctx['cursor'] > 0, 'can_redo': ctx['cursor'] < len(ctx['history']),
            'candidate_count': len(selected), 'candidate_action': ctx['candidate']['action'] if ctx['candidate'] else None,
            'candidate_id': ctx['candidate']['id'] if ctx['candidate'] else '',
            'candidate_detail': ctx['candidate'].get('detail', {}) if ctx['candidate'] else {},
            'xyz': points.round(3).tolist(), 'rgb': colors.tolist(), 'indices': sample.tolist(),
            'removed': ctx['removed'][sample].astype(int).tolist(),
            'selected': np.isin(sample, selected).astype(int).tolist(),
            'alignment': ctx['alignment'], 'measurement': measurement,
            'report': {'input_mode': input_mode(ctx['report']), 'pig_id': ctx['report'].get('pig_id'),
                'reviewed': ctx['report'].get('reviewed') is True,
                'capture_id': ctx['report'].get('capture_id'), 'camera': ctx['report'].get('camera'),
                'depth_profile': ctx['report'].get('depth_profile', {}),
                'original_valid_depth_ratio': ctx['report']['stats']['valid_depth_ratio']},
            'image_url': '/api/processing/image?context_id='+ctx['id']+'&dataset_id='+self.ws.dataset_id+'&id='+ctx['row']['id']}

    def detect(self, payload):
        with self.ws.lock:
            ctx, _ = self._ctx(payload)
            ids = np.flatnonzero(~ctx['removed'])
            candidates, detail = detect_candidates(ctx['cloud']['xyz_mm'][ids], payload.get('method', 'far'),
                payload.get('margin_mm', 300), payload.get('neighbors', 8), payload.get('neighbor_mm', 50))
            ctx['candidate'] = {'id': secrets.token_hex(12), 'indices': ids[candidates],
                'action': 'remove', 'detail': detail}
            return self._response(ctx)

    def select(self, payload):
        with self.ws.lock:
            ctx, _ = self._ctx(payload)
            if payload.get('action') not in {'remove', 'restore'}:
                raise ValueError('请选择删除或恢复点')
            append = payload.get('append', False)
            if not isinstance(append, bool):
                raise ValueError('连续框选参数无效')
            previous = ctx['candidate']
            if append and payload.get('candidate_id', '') != (previous['id'] if previous else ''):
                raise ValueError('选区已变化，请重新载入后框选')
            body = payload.get('coordinate') == 'body'
            selection = screen_selection(self._coordinates(ctx, body), payload.get('view', {}), payload.get('rectangle'))
            if payload.get('slice_only') is True:
                rows, _ = self._slices(ctx)
                i = int(scalar(payload.get('slice_index'), 0, len(rows)-1, '切片编号'))
                x = self._coordinates(ctx, True)[:, 0]
                selection &= (x >= rows[i]['x_start_mm']) & ((x <= rows[i]['x_end_mm']) if i == len(rows)-1 else (x < rows[i]['x_end_mm']))
            selection &= ctx['removed'] if payload['action'] == 'restore' else ~ctx['removed']
            same_action = append and previous and previous['action'] == payload['action']
            if same_action:
                selection[previous['indices']] = True
            ctx['candidate'] = {'id': secrets.token_hex(12), 'indices': np.flatnonzero(selection),
                'action': payload['action'], 'detail': {'method': 'screen_rectangle', 'through_all_depths': True,
                    'rectangle_count': (previous['detail'].get('rectangle_count', 0) if same_action else 0) + 1,
                    'coordinate': 'body' if body else 'camera', 'slice_only': payload.get('slice_only') is True}}
            return self._response(ctx)

    def clear(self, payload):
        with self.ws.lock:
            ctx, _ = self._ctx(payload)
            ctx['candidate'] = None
            return self._response(ctx)

    def _persist(self, ctx, result):
        from .workspace import atomic_json
        parent = self.ws.state_dir/'processing_cache'
        if parent.is_symlink():
            raise ValueError('清理暂存目录不能使用符号链接')
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.point-edits-', dir=parent))
        revision = secrets.token_hex(16)
        target = parent/revision
        try:
            if ctx['alignment']:
                ctx['measurement'] = {**self._measurement(ctx),
                    'review_summary': profile_review_summary(self._features(ctx))}
            ops = ctx['history']
            offsets = np.concatenate([[0], np.cumsum([len(op['indices']) for op in ops])]).astype(np.int64)
            indices = np.concatenate([op['indices'] for op in ops]) if ops else np.empty(0, np.int64)
            np.savez_compressed(stage/'point_edits.npz', removed=ctx['removed'], indices=indices, offsets=offsets)
            record = {'version': VERSION, 'revision': revision, 'created_at': now(), 'unit': 'mm',
                'relative_dir': ctx['row']['relative_dir'], 'input_points_sha256': ctx['points_hash'],
                'input_report_sha256': ctx['report_hash'], 'original_points': len(ctx['removed']),
                'removed_points': int(ctx['removed'].sum()), 'cleaning_digest': mask_digest(ctx['removed']),
                'operations': [{k: v for k, v in op.items() if k != 'indices'} for op in ops],
                'history_cursor': ctx['cursor'], 'edits_sha256': sha256(stage/'point_edits.npz'),
                'measurement': ctx['measurement'], 'source_point_index': 'row index in immutable points.npz',
                'processing': 'point exclusion only; no smoothing, filling, rescaling, or moving measured points'}
            write_json(stage/'pointcloud_cleaning.json', record)
            stage.rename(target)
            settings = copy.deepcopy(self.ws.settings)
            settings.setdefault('point_processing', {}).setdefault(str(self.ws.results_root), {})[ctx['row']['relative_dir']] = str(target)
            atomic_json(self.ws.state_dir/'settings.json', settings)
            self.ws.settings = settings
            result['processing'] = {'folder': target, 'record': record}
            ctx['revision'] = revision
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)
            raise

    def edit(self, payload):
        with self.ws.lock:
            ctx, result = self._ctx(payload)
            previous = {k: copy.deepcopy(ctx[k]) for k in ('removed', 'history', 'cursor', 'measurement', 'candidate', 'revision')}
            action = payload.get('operation')
            try:
                if action == 'apply':
                    candidate = ctx['candidate']
                    if not candidate or candidate['id'] != payload.get('candidate_id'):
                        raise ValueError('选点已变化，请重新检测或框选')
                    ids = candidate['indices']
                    if not len(ids):
                        raise ValueError('当前没有选中的点')
                    removed = candidate['action'] == 'remove'
                    if removed and len(ids) >= int((~ctx['removed']).sum()):
                        raise ValueError('不能删除全部点，请缩小范围')
                    op = {'indices': ids.copy(), 'removed': removed, 'time': now(), 'detail': candidate['detail']}
                    ctx['history'] = ctx['history'][:ctx['cursor']]+[op]
                    ctx['cursor'] += 1
                    ctx['removed'][ids] = removed
                elif action == 'undo':
                    if ctx['cursor'] == 0:
                        raise ValueError('没有可撤销的操作')
                    ctx['cursor'] -= 1
                    op = ctx['history'][ctx['cursor']]
                    ctx['removed'][op['indices']] = not op['removed']
                elif action == 'redo':
                    if ctx['cursor'] >= len(ctx['history']):
                        raise ValueError('没有可重做的操作')
                    op = ctx['history'][ctx['cursor']]
                    ctx['removed'][op['indices']] = op['removed']
                    ctx['cursor'] += 1
                elif action == 'reset':
                    ids = np.flatnonzero(ctx['removed'])
                    if not len(ids):
                        raise ValueError('当前没有已删除点')
                    ctx['history'] = ctx['history'][:ctx['cursor']]+[{'indices': ids, 'removed': False,
                        'time': now(), 'detail': {'method': 'restore_all'}}]
                    ctx['cursor'] += 1
                    ctx['removed'][ids] = False
                else:
                    raise ValueError('清理操作无效')
                ctx['candidate'] = None
                ctx['measurement'] = {**ctx['measurement'], 'slice_reviews': {},
                    'feature_reviews': {}, 'feature_quality': {},
                    'cleaning_digest': mask_digest(ctx['removed'])}
                self._persist(ctx, result)
            except Exception:
                ctx.update(previous)
                raise
            return self._response(ctx)

    def _slices(self, ctx):
        if not ctx['alignment']:
            raise ValueError('请先完成坐标对齐，再检查测量切片')
        m = self._measurement(ctx)
        return measure_slices(self._coordinates(ctx, True)[~ctx['removed']],
            ctx['alignment']['frame']['neck_tail_horizontal_span_mm'],
            m.get('thickness_mm', 15), m.get('min_points', 20), m.get('slice_reviews'))

    def slices(self, payload):
        with self.ws.lock:
            ctx, result = self._ctx(payload)
            self._require_review(result)
            rows, summary = self._slices(ctx)
            i_value = scalar(payload.get('slice_index', 0), 0, len(rows)-1, '切片编号')
            if i_value != int(i_value):
                raise ValueError('切片编号须为整数')
            i = int(i_value)
            row = rows[i]
            points = self._coordinates(ctx, True)
            selected = ~ctx['removed'] & (points[:, 0] >= row['x_start_mm'])
            selected &= (points[:, 0] <= row['x_end_mm']) if i == len(rows)-1 else (points[:, 0] < row['x_end_mm'])
            ids = np.flatnonzero(selected)
            idx = ids if len(ids) <= 6000 else ids[np.linspace(0, len(ids)-1, 6000).astype(int)]
            return {'rows': rows, 'summary': summary, 'current': row, 'slice_index': i,
                'yz': points[idx, 1:].round(3).tolist(), 'indices': idx.tolist(),
                'notes': self._measurement(ctx).get('notes', '')}

    def _require_review(self, result):
        if (result.get('cloud_review', {}).get('stages') is not None
                and workflow_state(result)['review']['blocked_by']):
            raise ValueError('前序阶段已淘汰，本采集不再进入测量复核；可返回前序阶段修改结论')

    def _features(self, ctx):
        if not ctx['alignment']:
            raise ValueError('请先确认地面、尾根与颈躯连接点，完成坐标对齐')
        m = self._measurement(ctx)
        provenance = {'reference_id': ctx['alignment']['reference_id'],
            'cleaning_digest': mask_digest(ctx['removed']), 'input_points_sha256': ctx['points_hash'],
            'input_report_sha256': ctx['report_hash'], 'relative_dir': ctx['row']['relative_dir'],
            'pig_id': ctx['report'].get('pig_id'), 'capture_id': ctx['report'].get('capture_id'),
            'input_mode': input_mode(ctx['report']), 'camera': ctx['report'].get('camera'),
            'original_valid_depth_ratio': ctx['report']['stats']['valid_depth_ratio'],
            'point_source': 'all retained immutable points; no preview sampling',
            'feature_version': feature_math.VERSION,
            'reference_conditions_source': 'confirmed_alignment_default'}
        key = json.dumps([provenance, m.get('feature_settings'), m.get('feature_quality'), m.get('feature_reviews')], sort_keys=True)
        if ctx.get('feature_cache', {}).get('key') != key:
            result = feature_math.calculate(self._coordinates(ctx, True)[~ctx['removed']],
                ctx['alignment']['frame']['neck_tail_horizontal_span_mm'], m.get('feature_settings'),
                m.get('feature_quality'), m.get('feature_reviews'), provenance)
            ctx['feature_cache'] = {'key': key, 'result': result}
        return ctx['feature_cache']['result']

    def features(self, payload):
        """Compute candidates; persist only explicit settings/quality/reviews."""
        with self.ws.lock:
            ctx, result = self._ctx(payload)
            self._require_review(result)
            old = copy.deepcopy(ctx['measurement'])
            m = self._measurement(ctx)
            changed = any(k in payload for k in ('settings', 'conditions', 'status'))
            try:
                if 'profile_range' in payload and 'status' not in payload:
                    raise ValueError('批量剖面复核须选择确认、淘汰或待定状态')
                if changed:
                    m['measurement_started'] = True
                if 'settings' in payload:
                    config = feature_math.settings(payload['settings'])
                    if config != feature_math.settings(m.get('feature_settings')):
                        m['feature_reviews'] = {}
                    m['feature_settings'] = config
                if 'conditions' in payload:
                    # Accept the historical request shape without reinstating
                    # the removed selectors or erasing existing profile labels.
                    feature_math.quality(payload['conditions'])
                ctx['measurement'] = m
                computed = self._features(ctx)
                review_update = None
                if 'status' in payload:
                    if not isinstance(payload['status'], str) or payload['status'] not in {'pending', 'usable', 'unusable'}:
                        raise ValueError('剖面复核状态无效')
                    count = len(computed['profiles'])
                    batch = 'profile_range' in payload
                    bounds = payload['profile_range'] if batch else [payload.get('profile_index')]*2
                    if not isinstance(bounds, list) or len(bounds) != 2:
                        raise ValueError('剖面范围须包含起始和结束编号')
                    bounds = [scalar(v, 0, count-1, '剖面编号') for v in bounds]
                    if any(v != int(v) for v in bounds):
                        raise ValueError('剖面编号须为整数')
                    first, last = map(int, bounds)
                    if first > last:
                        raise ValueError('起始剖面不能大于结束剖面')
                    indices = list(range(first, last+1))
                    skipped = [i for i in indices if payload['status'] == 'usable'
                               and computed['profiles'][i]['width_q02_q98_mm'] is None]
                    if skipped and not batch:
                        raise ValueError('该剖面点数或中心线支撑不足，不能确认为可测')
                    updates = {str(i): payload['status'] for i in indices if i not in skipped}
                    m['feature_reviews'] = {**m.get('feature_reviews', {}), **updates}
                    ctx['measurement'] = m
                    computed = self._features(ctx)
                    review_update = {'profile_range': [first, last], 'status': payload['status'],
                                     'updated': len(updates), 'skipped': skipped}
                i = scalar(payload.get('profile_index', 0), 0, len(computed['profiles'])-1, '剖面编号')
                if i != int(i):
                    raise ValueError('剖面编号须为整数')
                if 'format' in payload and payload['format'] not in {'csv', 'npz'}:
                    raise ValueError('请选择 CSV 或 NPZ 格式')
                if changed:
                    self._persist(ctx, result)
                if 'format' in payload:
                    form = payload['format']
                    if form == 'csv':
                        content, mime = feature_math.csv_bytes(computed), 'text/csv'
                    elif form == 'npz':
                        stream = io.BytesIO()
                        np.savez_compressed(stream, **feature_math.arrays(computed))
                        content, mime = stream.getvalue(), 'application/octet-stream'
                    else:
                        raise ValueError('请选择 CSV 或 NPZ 格式')
                    return {'download': {'filename': 'profile_sequence.'+form, 'mime': mime,
                        'base64': base64.b64encode(content).decode('ascii')}, 'revision': ctx['revision']}
                row = computed['profiles'][int(i)]
                points = self._coordinates(ctx, True)
                selected = feature_math.profile_selection(points, row, computed['settings'],
                    ctx['alignment']['frame']['neck_tail_horizontal_span_mm']) & ~ctx['removed']
                ids = np.flatnonzero(selected)
                ids = ids if len(ids) <= 6000 else ids[np.linspace(0, len(ids)-1, 6000).astype(int)]
                q = points[ids]
                across = (q[:, :2]-[row['center_x_mm'], row['center_y_mm']]) @ [-row['tangent_y'], row['tangent_x']]
                return {'features': computed, 'revision': ctx['revision'], 'profile_index': int(i),
                        'review_update': review_update,
                        'profile_yz': np.column_stack([across, q[:, 2]]).round(3).tolist()}
            except Exception:
                ctx['measurement'] = old
                raise

    def export_features_to(self, dest, result, source_folder=None):
        source = source_folder or dest
        for name in feature_math.FEATURE_FILES:
            if (dest/name).exists():
                (dest/name).unlink()
        if not alignment_status(source):
            return None
        alignment = read_json(source/'alignment.json')
        with np.load(source/'points.npz', allow_pickle=False) as saved:
            points = saved['xyz_mm'].copy()
        removed, m = np.zeros(len(points), bool), {}
        if result.get('processing'):
            record, removed, _ = self._read(result['processing']['folder'], result)
            m = record.get('measurement', {})
        digest = mask_digest(removed)
        valid = m.get('reference_id') == alignment['reference_id'] and m.get('cleaning_digest') == digest
        ctx = {'alignment': alignment, 'cloud': {'xyz_mm': points}, 'removed': removed,
            'measurement': {**m, 'feature_reviews': m.get('feature_reviews', {}) if valid else {},
                            'feature_quality': m.get('feature_quality', {}) if valid else {},
                            'reference_id': alignment['reference_id'], 'cleaning_digest': digest},
            'row': {'relative_dir': result['report'].get('relative_dir', alignment.get('relative_dir', ''))},
            'report': result['report'], 'points_hash': sha256(source/'points.npz'),
            'report_hash': sha256(source/'pointcloud.json')}
        computed = self._features(ctx)
        feature_math.write_products(dest, computed)
        write_json(dest/'measurement_summary.json', {**profile_review_summary(computed),
            'reference_id': alignment['reference_id'], 'cleaning_digest': digest,
            'settings': computed['settings'], 'conditions': computed['conditions']})
        return computed

    def export_stage_to(self, dest, result, stage, has_output):
        """Write delivery products directly, without copying editable dependencies."""
        if not has_output:
            return {}
        if stage == 'review':
            if self.export_features_to(dest, result, source_folder=result['folder']) is None:
                raise ValueError('测量复核缺少有效坐标对齐，无法导出')
            return {'geometry_json': 'geometry_features.json', 'sequence_csv': 'profile_sequence.csv',
                    'sequence_npz': 'profile_sequence.npz', 'summary': 'measurement_summary.json'}
        with np.load(result['folder']/'points.npz', allow_pickle=False) as saved:
            cloud = {key: saved[key].copy() for key in saved.files}
        record, removed = {}, np.zeros(len(cloud['xyz_mm']), bool)
        if result.get('processing'):
            record, removed, _ = self._read(result['processing']['folder'], result)
        ids = np.flatnonzero(~removed)
        current = {key: value[ids] for key, value in cloud.items()}
        current['source_point_index'] = ids
        color = display_ir(current['infrared_raw']) if input_mode(result['report']) == 'infrared' else current['rgb'].copy()
        if input_mode(result['report']) != 'infrared':
            color[~current['rgb_valid']] = [110, 120, 140]
        if stage == 'cleaning':
            files = {'primary_ply': 'cloud_cleaned_mm.ply', 'primary_npz': 'cleaned_points.npz',
                     'cleaning': 'pointcloud_cleaning.json'}
            write_json(dest/files['cleaning'], {'schema_version': 1, 'package_kind': 'stage_products',
                'version': VERSION, 'cleaning_digest': mask_digest(removed),
                'cleaning_revision': record.get('revision', 0),
                'input_points_sha256': sha256(result['folder']/'points.npz'),
                'original_points': len(removed), 'kept_points': len(ids), 'removed_points': int(removed.sum()),
                'source_point_index': 'indices in the immutable input points.npz'})
        elif stage == 'alignment':
            if not alignment_status(result['folder']):
                raise ValueError('坐标对齐成果不完整或已变化，无法导出')
            alignment = read_json(result['folder']/'alignment.json')
            current['xyz_mm'] = transform_points(current['xyz_mm'], alignment['frame']).astype(np.float32)
            files = {'primary_ply': 'cloud_body_mm.ply', 'primary_npz': 'body_points.npz',
                     'alignment': 'alignment.json'}
        else:
            raise ValueError('导出阶段无效')
        np.savez_compressed(dest/files['primary_npz'], **current)
        ply = ply_bytes(current, color)
        if stage == 'alignment':
            ply = ply.replace(b'coordinates native_depth_camera', b'coordinates ground_body', 1)
        (dest/files['primary_ply']).write_bytes(ply)
        if stage == 'alignment':
            write_json(dest/files['alignment'], {**alignment,
                'package_kind': 'stage_products', 'editable': False,
                'source_body_points_sha256': alignment['body_points_sha256'],
                'body_points_sha256': sha256(dest/files['primary_npz']),
                'cleaning_digest': mask_digest(removed), 'retained_points': len(ids)})
        return files

    def review(self, payload):
        with self.ws.lock:
            ctx, result = self._ctx(payload)
            old = copy.deepcopy(ctx['measurement'])
            m = self._measurement(ctx)
            try:
                thickness = scalar(payload.get('thickness_mm', m.get('thickness_mm', 15)), 5, 100, '切片厚度 / mm')
                minimum = scalar(payload.get('min_points', m.get('min_points', 20)), 3, 500, '切片最少点数')
                if minimum != int(minimum):
                    raise ValueError('切片最少点数须为整数')
                if thickness != m.get('thickness_mm', 15) or minimum != m.get('min_points', 20):
                    m['slice_reviews'] = {}
                m.update(thickness_mm=thickness, min_points=int(minimum))
                if 'notes' in payload:
                    if not isinstance(payload['notes'], str) or len(payload['notes']) > 2000:
                        raise ValueError('测量备注最多 2000 字')
                    m['notes'] = payload['notes']
                ctx['measurement'] = m
                if 'status' in payload:
                    if payload['status'] not in {'pending', 'usable', 'unusable'}:
                        raise ValueError('切片状态无效')
                    rows, _ = self._slices(ctx)
                    index = scalar(payload.get('slice_index'), 0, len(rows)-1, '切片编号')
                    if index != int(index):
                        raise ValueError('切片编号须为整数')
                    i = int(index)
                    if payload['status'] == 'usable' and not rows[i]['point_support_sufficient']:
                        raise ValueError('该切片点数不足，不能确认为可测')
                    m['slice_reviews'] = {**m.get('slice_reviews', {}), str(i): payload['status']}
                m['reviewed_at'] = now()
                self._persist(ctx, result)
            except Exception:
                ctx['measurement'] = old
                raise
            return self._response(ctx)

    def image(self, payload):
        """Use the segmentation coordinate grid; never substitute RGB projection."""
        with self.ws.lock:
            ctx, _ = self._ctx(payload, check_revision=False)
            report = ctx['report']
            seg = Path(report.get('segmentation_dir', ''))
            mask_file = report.get('mask_file', 'trunk_mask.png')
            if Path(mask_file).name != mask_file:
                raise ValueError('分割掩码路径无效')
            mask = seg/mask_file
            if (not report.get('mask_sha256') or not mask.is_file() or mask.is_symlink()
                    or sha256(mask) != report['mask_sha256']):
                raise ValueError('找不到与点云一致的分割掩码；三维清理仍可使用')
            source = Path(report.get('source_dir', ''))
            # Rebuild the correct image grid from hash-verified originals, so an
            # outdated overlay.png cannot silently show a different mask.
            expected = report.get('source_sha256', {})
            if input_mode(report) == 'rgb':
                from .geometry import prepare_frame
                required = ('color.png', 'reconstruction.json', 'depth_aligned_to_color_mm.npy')
                if any(not expected.get(n) or sha256(source/n) != expected[n] for n in required):
                    raise ValueError('原始 RGB／标定／深度已变化，不能显示定位底图')
                rgb, _, geometry, _ = prepare_frame(source, 'rgb')
                if report.get('geometry_id') != geometry.get('geometry_id'):
                    raise ValueError('分割底图坐标已变化')
                image = Image.fromarray(rgb)
            else:
                name = 'infrared.npy'
                if not expected.get(name) or sha256(source/name) != expected[name]:
                    raise ValueError('找不到经核对的原始 IR 底图')
                values = np.load(source/name, allow_pickle=False)
                image = Image.fromarray(display_ir(values.ravel()).reshape(*values.shape, 3))
            with Image.open(mask) as saved:
                mask_array = np.array(saved)
            if mask_array.shape != (image.height, image.width):
                raise ValueError('分割掩码与底图尺寸不一致')
            rgb = np.array(image.convert('RGB'))
            fg = mask_array > 0
            rgb[fg] = np.rint(rgb[fg]*.8+np.array([35, 200, 115])*.2).astype(np.uint8)
            image = Image.fromarray(rgb)
            image.thumbnail((1400, 1000))
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            return buf.getvalue()

    def image_points(self, payload):
        with self.ws.lock:
            ctx, _ = self._ctx(payload)
            if payload.get('kind') == 'slice':
                rows, _ = self._slices(ctx)
                i = int(scalar(payload.get('slice_index'), 0, len(rows)-1, '切片编号'))
                x = self._coordinates(ctx, True)[:, 0]
                selected = ~ctx['removed'] & (x >= rows[i]['x_start_mm'])
                selected &= (x <= rows[i]['x_end_mm']) if i == len(rows)-1 else (x < rows[i]['x_end_mm'])
                ids = np.flatnonzero(selected)
            elif payload.get('kind') == 'profile':
                computed = self._features(ctx)
                i = scalar(payload.get('profile_index'), 0, len(computed['profiles'])-1, '剖面编号')
                if i != int(i):
                    raise ValueError('剖面编号须为整数')
                selected = feature_math.profile_selection(self._coordinates(ctx, True), computed['profiles'][int(i)],
                    computed['settings'], ctx['alignment']['frame']['neck_tail_horizontal_span_mm'])
                ids = np.flatnonzero(selected & ~ctx['removed'])
            else:
                ids = ctx['candidate']['indices'] if ctx['candidate'] else np.empty(0, np.int64)
            # Draw points in mask coordinates, not original RGB coordinates.
            uv = ctx['cloud']['pixel_uv'][ids]
            if len(uv) > 40000:
                uv = uv[np.linspace(0, len(uv)-1, 40000).astype(int)]
            profile = ctx['report'].get('depth_profile', {})
            intrinsic = profile.get('intrinsic', {})
            width = intrinsic.get('width', profile.get('width'))
            height = intrinsic.get('height', profile.get('height'))
            if not width or not height:
                seg = Path(ctx['report'].get('segmentation_dir', ''))/ctx['report'].get('mask_file', 'trunk_mask.png')
                with Image.open(seg) as im:
                    width, height = im.size
            return {'uv': uv.tolist(), 'shape_hw': [height, width],
                    'count': len(ids), 'pixel_coordinate_space': ctx['report'].get('pixel_coordinate_space', 'depth_native')}

    def export_to(self, dest, result, include_measurement=True):
        """Add derived products to a new export; keep original products intact."""
        # Retire old derived tables only in the new exported copy.
        if (dest/'slice_measurements.csv').exists():
            (dest/'slice_measurements.csv').unlink()
        info = result.get('processing')
        if not info:
            # Invalid/unrestored sidecars must not masquerade as the current version.
            for name in PROCESSING_FILES:
                if (dest/name).exists():
                    (dest/name).unlink()
            if include_measurement:
                self.export_features_to(dest, result)
            return
        r, removed, _ = self._read(info['folder'], result)
        for name in EDIT_FILES:
            shutil.copy2(info['folder']/name, dest/name)
        if not include_measurement:
            write_json(dest/'pointcloud_cleaning.json', {**r, 'measurement': {}})
            for name in feature_math.FEATURE_FILES | {'measurement_summary.json'}:
                if (dest/name).exists():
                    (dest/name).unlink()
        with np.load(dest/'points.npz', allow_pickle=False) as saved:
            cloud = {k: saved[k].copy() for k in saved.files}
        ids = np.flatnonzero(~removed)
        cleaned = {k: v[ids] for k, v in cloud.items()}
        cleaned['source_point_index'] = ids
        np.savez_compressed(dest/'cleaned_points.npz', **cleaned)
        color = display_ir(cleaned['infrared_raw']) if input_mode(result['report']) == 'infrared' else cleaned['rgb'].copy()
        if input_mode(result['report']) != 'infrared':
            color[~cleaned['rgb_valid']] = [110, 120, 140]
        (dest/'cloud_cleaned_mm.ply').write_bytes(ply_bytes(cleaned, color))
        alignment = read_json(dest/'alignment.json') if alignment_status(dest) else None
        features = self.export_features_to(dest, result) if include_measurement else None
        summary = (read_json(dest/'measurement_summary.json') if features else
            {'measurement_method': MEASUREMENT_METHOD, 'slice_count': 0, 'usable_slices': 0,
             'measurement_ready': False, 'reason': '尚未建立猪体坐标'})
        if alignment:
            body = {**cleaned, 'xyz_mm': transform_points(cleaned['xyz_mm'], alignment['frame']).astype(np.float32)}
            np.savez_compressed(dest/'cleaned_body_points.npz', **body)
            (dest/'cloud_cleaned_body_mm.ply').write_bytes(ply_bytes(body, color).replace(
                b'coordinates native_depth_camera', b'coordinates ground_body', 1))
        else:
            for name in ('cleaned_body_points.npz', 'cloud_cleaned_body_mm.ply', 'slice_measurements.csv'):
                if (dest/name).exists():
                    (dest/name).unlink()
        summary.update(original_points=len(removed), kept_points=len(ids), removed_points=int(removed.sum()),
            original_valid_depth_ratio=result['report']['stats']['valid_depth_ratio'],
            removed_fraction_of_original_points=float(removed.mean()),
            cleaning_revision=r['revision'], input_points_sha256=r['input_points_sha256'])
        if include_measurement:
            write_json(dest/'measurement_summary.json', summary)
        # Exported previews are standalone and read-only; all measurements were
        # calculated on full-density points before this display-only sample.
        preview = body if alignment else cleaned
        sample = np.sort(np.random.default_rng(7).choice(len(ids), min(22000, len(ids)), replace=False))
        data = {'xyz': preview['xyz_mm'][sample].round(3).tolist(), 'rgb': color[sample].tolist(),
            'indices': ids[sample].tolist(), 'removed': [0]*len(sample), 'selected': [0]*len(sample),
            'total': len(removed), 'kept_count': len(ids), 'removed_count': int(removed.sum()),
            'alignment': alignment, 'offline': True, 'offline_body': bool(alignment), 'summary': summary,
            'features': features}
        html = (PAGES_DIR/'processing.html').read_text(encoding='utf-8')
        script = (JS_DIR/'processing.js').read_text(encoding='utf-8')
        style = (CSS_DIR/'processing.css').read_text(encoding='utf-8')
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
        html = html.replace('<link rel="stylesheet" href="/processing.css">', '<style>'+style+'</style>')
        html = html.replace("const APP_TOKEN='__TOKEN__';", 'const APP_TOKEN=""; const OFFLINE_DATA='+encoded+';')
        html = html.replace('<script src="/processing.js"></script>', '<script>'+script+'</script>')
        (dest/'processing_preview.html').write_text(html, encoding='utf-8')
