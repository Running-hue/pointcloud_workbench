"""Browser workflow: independent inputs, background jobs and folder-tree exports."""
from __future__ import annotations

import copy
import csv
from datetime import datetime
import hashlib
from html import escape
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
from urllib.parse import quote

from .core import (independent_paths, within, read_json, write_json, sha256, reconstruct,
                   export_frame, source_files, input_mode, primary_ply, required_result_files,
                   COMMON_RESULT_FILES, export_metadata, EXPORT_POLICY, camera_identity, np)
from .viewer import write_viewer
from .alignment import AlignmentService, SIDE_FILES, alignment_status, render_body_view
from .processing import ProcessingService, PROCESSING_FILES, MEASUREMENT_METHOD
from . import features as feature_math
from .paths import DEFAULT_OUTPUT_ROOT
from .workflow import (STAGES, STAGE_LABELS, DECISIONS, stage_version, stage_records,
                       workflow_state, stage_export_plan, final_decision)

RESULT_FILES = {'points.npz', 'cloud_rgb_mm.ply', 'cloud_ir_mm.ply', 'pointcloud.json',
                'preview.html', 'mask_depth_check.png', 'rgb_projection_check.png',
                'rgb_registration_check.png', 'cloud_review.json', 'current_pointcloud.json',
                'workflow_stages.json'} | SIDE_FILES | PROCESSING_FILES
REVIEW_GROUPS = {'keep': '保留', 'pending': '待定', 'reject': '淘汰'}


def valid_cloud_review(record, folder, relative_dir, report):
    return (isinstance(record, dict) and record.get('schema_version') in {1, 2}
            and record.get('decision') in {'keep', 'pending', 'reject'}
            and record.get('relative_dir') == relative_dir
            and record.get('input_mode') == input_mode(report)
            and record.get('points_sha256') == sha256(folder/'points.npz')
            and ('stages' not in record or (isinstance(record['stages'], dict)
                and all(k in STAGES and isinstance(v, dict) and v.get('decision') in DECISIONS
                        and isinstance(v.get('version'), dict) for k, v in record['stages'].items()))))


def processing_version(result):
    """Point membership and body reference, independent of notes/save timestamps."""
    record = result.get('processing', {}).get('record', {})
    return {'cleaning_digest': record.get('cleaning_digest', '') if record.get('removed_points', 0) else '',
            'alignment_reference_id': result.get('alignment_reference_id', '') if result.get('has_alignment') else ''}


def cloud_review_state(result):
    record = result.get('cloud_review')
    if record and 'stages' in record:
        stages = {k: v for k, v in workflow_state(result).items()
                  if not v['blocked_by'] and (v['has_output'] or v['reviewed'])}
        return {'cloud_decision': final_decision(stages),
                'cloud_review_stale': any(v['stale'] for v in stages.values())}
    stale = bool(record and (record.get('schema_version') != 2
                            or record.get('processing_version') != processing_version(result)))
    return {'cloud_decision': record['decision'] if record and not stale else 'pending',
            'cloud_review_stale': stale}


def current_cloud(result):
    """The same full-density version used by processing and folder exports."""
    processed, aligned = bool(result.get('processing')), bool(result.get('has_alignment'))
    record = result.get('processing', {}).get('record', {})
    return {'current_ply': 'cloud_cleaned_body_mm.ply' if processed and aligned else
            'cloud_body_mm.ply' if aligned else 'cloud_cleaned_mm.ply' if processed else primary_ply(result['report']),
            'current_npz': 'cleaned_body_points.npz' if processed and aligned else
            'body_points.npz' if aligned else 'cleaned_points.npz' if processed else 'points.npz',
            'current_coordinates': 'ground_body' if aligned else 'native_depth_camera',
            'current_points': result['report']['stats']['points']-record.get('removed_points', 0)}


def measurement_state(result):
    record = result.get('processing', {}).get('record', {})
    m = record.get('measurement', {})
    version = processing_version(result)
    valid = (result.get('has_alignment') and m.get('reference_id') == version['alignment_reference_id']
             and m.get('cleaning_digest') == record.get('cleaning_digest'))
    summary = m.get('review_summary', {}) if valid else {}
    if (summary.get('measurement_method') != MEASUREMENT_METHOD
            or summary.get('feature_version') != feature_math.VERSION):
        summary = {}
    return {'measurement_ready': summary.get('measurement_ready') is True,
            'measurement_usable_slices': summary.get('usable_slices', 0),
            'measurement_slice_count': (summary.get('slice_count') or
                feature_math.settings(m.get('feature_settings'))['count']) if result.get('has_alignment') else 0}


def write_current_cloud(folder, result, decision):
    current = {**current_cloud(result), **measurement_state(result)}
    if (folder/'measurement_summary.json').is_file():
        summary = read_json(folder/'measurement_summary.json')
        if summary.get('measurement_method') == MEASUREMENT_METHOD:
            current.update(measurement_ready=summary.get('measurement_ready') is True,
                measurement_usable_slices=summary.get('usable_slices', 0),
                measurement_slice_count=summary.get('slice_count', 0))
    if not all((folder/current[k]).is_file() for k in ('current_ply', 'current_npz')):
        raise ValueError('当前处理版本的主点云文件缺失，未导出本采集')
    write_json(folder/'current_pointcloud.json', {**current, 'schema_version': 1, 'unit': 'mm',
        'processing_version': processing_version(result), 'cloud_decision': decision,
        'workflow_stages': workflow_state(result),
        'definition': 'current full-density version; originals included for traceability; keep is not measurement acceptance'})
    return current


def write_cloud_review(folder, result):
    """Write review metadata only into a new exported copy."""
    record = result.get('cloud_review')
    path = folder/'cloud_review.json'
    if record:
        if not valid_cloud_review(record, folder, record['relative_dir'], result['report']):
            raise ValueError('点云在筛选后发生变化，请重新核对保留／待定／淘汰状态')
        if 'stages' in record:
            record = {**record, 'decision': cloud_review_state(result)['cloud_decision']}
        elif cloud_review_state(result)['cloud_review_stale']:
            record = {**cloud_review_record(result, record['relative_dir'], 'pending'),
                      'reviewed_at': None, 'previous_review': record,
                      'reason': '处理版本已变化或旧记录未绑定版本，需重新确认采集筛选'}
        write_json(path, record)
    elif path.exists():
        path.unlink()


def cloud_review_record(result, relative_dir, decision, stage=None):
    points = result['folder']/'points.npz'
    if points.is_symlink() or not points.is_file():
        raise ValueError('当前点云文件不存在或路径不合法')
    record = {'schema_version': 2, 'decision': decision, 'relative_dir': relative_dir,
            'input_mode': input_mode(result['report']), 'points_sha256': sha256(points),
            'processing_version': processing_version(result),
            'scope': 'current full-density point set and body reference; not measurement acceptance',
            'reviewed_at': datetime.now().astimezone().isoformat()}
    if stage is not None:
        records = stage_records(result)
        records[stage] = {'decision': decision, 'version': stage_version(result, stage),
                          'reviewed_at': record['reviewed_at']}
        record.update(stages=records, scope='independent workflow stages; upstream rejection blocks downstream')
        record['decision'] = cloud_review_state({**result, 'cloud_review': record})['cloud_decision']
    return record


def stage_export_result(dest, source, plan, relative_dir):
    """Project a capture onto the requested stages, retaining point dependencies."""
    aligned = bool(source.get('has_alignment') and any(k in plan for k in ('alignment', 'review')))
    measured = 'review' in plan and plan['review']['has_output']
    result = {**source, 'folder': dest, 'has_alignment': aligned,
              'alignment_reference_id': source.get('alignment_reference_id', '') if aligned else ''}
    if not aligned:
        for name in SIDE_FILES | {'cleaned_body_points.npz', 'cloud_cleaned_body_mm.ply'}:
            if (dest/name).exists():
                (dest/name).unlink()
    if not measured and result.get('processing'):
        info = result['processing']
        result['processing'] = {**info, 'record': {**info['record'], 'measurement': {}}}
    review = cloud_review_record(source, relative_dir, final_decision(plan))
    records = stage_records(source)
    review['stages'] = {k: {**records.get(k, {}), 'decision': v['decision'], 'version': stage_version(result, k)}
                        for k, v in plan.items()}
    review['scope'] = 'independent workflow stages; upstream rejection blocks downstream'
    result['cloud_review'] = review
    return result


def write_stage_products(dest, result, plan):
    products = {}
    current = current_cloud(result)
    for stage, status in plan.items():
        if stage == 'cleaning':
            files = {'primary_ply': 'cloud_cleaned_mm.ply' if result.get('processing') else primary_ply(result['report']),
                     'primary_npz': 'cleaned_points.npz' if result.get('processing') else 'points.npz'}
        elif stage == 'alignment' and status['has_output']:
            files = {'primary_ply': current['current_ply'], 'primary_npz': current['current_npz'],
                     'alignment': 'alignment.json'}
        elif stage == 'review' and status['has_output']:
            files = {'geometry_json': 'geometry_features.json', 'sequence_csv': 'profile_sequence.csv',
                     'sequence_npz': 'profile_sequence.npz', 'summary': 'measurement_summary.json'}
        else:
            files = {}
        if any(not (dest/name).is_file() for name in files.values()):
            raise ValueError(f'{STAGE_LABELS[stage]}成果文件不完整')
        products[stage] = {'label': STAGE_LABELS[stage], **status, 'files': files}
    write_json(dest/'workflow_stages.json', {'schema_version': 1, 'stages': products})
    return products


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.'+path.name+'.tmp')
    write_json(tmp, data)
    tmp.replace(path)


def directory(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请选择文件夹或填写完整路径')
    p = Path(value).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f'文件夹不存在：{p}')
    return p


def folder_name(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('请填写成果文件夹名')
    if value.startswith('.') or value.endswith(('.', ' ')) or any(x in value for x in '/\\\0<>:"|?*'):
        raise ValueError('成果文件夹名不能包含路径分隔符或系统保留字符')
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
        raise ValueError('请换一个文件夹名，该名称被 Windows 保留')
    return value


def safe_copy_tree(source, target):
    if source.is_symlink():
        raise ValueError('成果目录包含符号链接，拒绝导出')
    target.mkdir()
    for entry in source.iterdir():
        if entry.is_symlink():
            raise ValueError(f'成果包含符号链接：{entry.name}')
        if entry.is_dir():
            safe_copy_tree(entry, target/entry.name)
        elif entry.is_file():
            before = sha256(entry)
            shutil.copy2(entry, target/entry.name)
            if before != sha256(target/entry.name) or before != sha256(entry):
                raise ValueError('复制期间点云文件发生变化，请重试')
        else:
            raise ValueError('成果包含不支持的文件类型')


def check_frame(source, segmentation, declared_mode):
    """Quarantine a bad capture without guessing its modality or aborting the batch."""
    meta, errors, missing = {}, [], []
    path = segmentation/'segmentation.json'
    if not path.is_file():
        missing.append('segmentation.json')
        errors.append('缺少分割记录 segmentation.json')
    else:
        try:
            meta = read_json(path)
            if not isinstance(meta, dict) or not meta:
                raise ValueError('分割记录必须是非空 JSON 对象')
        except (ValueError, OSError) as exc:
            errors.append(f'分割记录无法读取：{exc}')
            meta = {}
    mode = declared_mode
    if meta:
        try:
            mode = input_mode(meta)
            if declared_mode and mode != declared_mode:
                errors.append(f'分割清单与逐帧输入模式不一致（清单 {declared_mode}，逐帧 {mode}）')
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
    # Use the declared mode only to report missing files when metadata is absent.
    # The validation error above always blocks reconstruction of that frame.
    try:
        names = source_files(meta if meta else {'input_mode': declared_mode})
    except (TypeError, AttributeError) as exc:
        names = source_files({'input_mode': mode})
        errors.append(f'分割记录格式无效：{exc}')
    missing.extend(n for n in names if not (source/n).is_file())
    mask = 'trunk_mask.png' if meta.get('reviewed') is True and meta.get('status') == 'reviewed' else (
        'trunk_mask_working.png' if meta.get('status') == 'corrected' else 'trunk_mask_draft.png')
    if not (segmentation/mask).is_file(): missing.append(mask)
    file_missing = sorted(set(missing)-{'segmentation.json'})
    if file_missing: errors.append('缺少文件：'+', '.join(file_missing))
    # Camera grouping comes from capture metadata, never the directory name or
    # the segmentation modality (both cameras can have either RGB or IR masks).
    camera = {'type': 'unknown', 'model': '未知相机'}
    try:
        camera = camera_identity(read_json(source/'reconstruction.json'))
    except (ValueError, OSError, TypeError, AttributeError):
        pass
    return {'reviewed': meta.get('reviewed') is True,
            'status': meta.get('status', 'missing'), 'decision': meta.get('decision', 'pending'),
            'input_mode': mode, 'camera': camera, 'missing': sorted(set(missing)),
            'validation_errors': errors, 'validation_status': 'failed' if errors else 'passed',
            'input_loaded': True}


def saved_job(root):
    """A reopened export can show its completed task without starting a worker."""
    path = root/'export.json'
    if path.is_symlink(): return None
    try:
        job = read_json(path)
        if not isinstance(job, dict) or job.get('status') not in {'completed', 'completed_with_errors', 'stopped', 'failed'}:
            return None
        if job.get('mode') not in {'generate', 'export'}: return None
        if any(type(job.get(key)) is not int or job[key] < 0 for key in ('total', 'done', 'success')):
            return None
        if not job['success'] <= job['done'] <= job['total']: return None
        for field, reason in [('skipped', 'reason'), ('errors', 'error')]:
            if not isinstance(job.get(field), list) or any(
                    not isinstance(row, dict) or not isinstance(row.get('relative_dir'), str)
                    or not isinstance(row.get(reason), str) for row in job[field]):
                return None
        return {**job, 'id': str(job.get('id') or secrets.token_hex(10)), 'output': str(root),
                'current': job.get('current') if isinstance(job.get('current'), str) else '已载入保存的任务记录'}
    except (ValueError, OSError, TypeError):
        return None


class Workspace:
    def __init__(self, state_dir, initial_segmentation=None, initial_results=None):
        self.state_dir = Path(state_dir).resolve()
        self.lock = threading.RLock()
        self.worker = None
        self.stop_event = threading.Event()
        self.job = None
        self.dataset_id = secrets.token_hex(12)
        self.rows = []
        self.results = {}
        self.results_match_inputs = True
        self.source = self.segmentation = self.results_root = None
        self.last_output = None
        self.warning = ''
        self.settings = {}
        self.alignment = AlignmentService(self)
        self.processing = ProcessingService(self)
        settings_file = self.state_dir/'settings.json'
        if settings_file.is_file():
            try:
                self.settings = read_json(settings_file)
            except (ValueError, OSError):
                self.warning = '上次路径记录无法读取，请重新选择目录'
        seg = initial_segmentation or self.settings.get('segmentation')
        if seg:
            try:
                source = self.settings.get('source') if not initial_segmentation else None
                source = source or read_json(Path(seg)/'dataset.json').get('source')
                self.load({'source': str(source), 'segmentation': str(seg)}, remember=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.warning = f'未恢复输入目录：{exc}'
        results = initial_results or self.settings.get('results')
        if results:
            try:
                self.open_results(str(results), remember=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.warning = f'未恢复点云目录：{exc}'

    def remember(self):
        atomic_json(self.state_dir/'settings.json', self.settings)

    def idle(self):
        if self.job and self.job['status'] in {'running', 'stopping'}:
            raise ValueError('正在生成或导出，请先等待完成或停止任务')

    def validation_report(self):
        with self.lock:
            return {'schema_version': 1, 'source': str(self.source or ''),
                    'segmentation': str(self.segmentation or ''),
                    'summary': {'total': len(self.rows),
                                'passed': sum(not r.get('validation_errors') for r in self.rows),
                                'failed': sum(bool(r.get('validation_errors')) for r in self.rows),
                                'excluded': sum(r.get('excluded', False) for r in self.rows)},
                    'issues': [{'relative_dir': r['relative_dir'], 'pig_id': r['pig_id'],
                                'capture_id': r['capture_id'], 'missing': r['missing'],
                                'errors': list(r.get('validation_errors', [])),
                                'excluded': r.get('excluded', False)} for r in self.rows
                               if r.get('validation_errors') or r.get('excluded')]}

    def exclusion_key(self, source, segmentation):
        return hashlib.sha256((str(source)+'\0'+str(segmentation)).encode()).hexdigest()

    def mark_skipped(self, payload):
        with self.lock:
            self.idle()
            if payload.get('dataset_id') != self.dataset_id:
                raise ValueError('目录已切换，请刷新后再操作')
            ids, excluded = payload.get('ids'), payload.get('excluded')
            known = {r['id'] for r in self.rows}
            if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                    or len(set(ids)) != len(ids) or not set(ids) <= known or type(excluded) is not bool):
                raise ValueError('请选择已载入的采集，并指定是否标记跳过')
            rows = [dict(r, excluded=excluded) if r['id'] in ids else dict(r) for r in self.rows]
            settings = copy.deepcopy(self.settings)
            settings.setdefault('skipped_frames', {})[self.exclusion_key(self.source, self.segmentation)] = [
                r['relative_dir'] for r in rows if r.get('excluded')]
            atomic_json(self.state_dir/'settings.json', settings)
            self.rows, self.settings = rows, settings
            return self.state()

    def review_cloud(self, payload):
        with self.lock:
            self.idle()
            if payload.get('dataset_id') != self.dataset_id:
                raise ValueError('目录已切换，请刷新后再筛选点云')
            decision = payload.get('decision')
            if decision not in {'keep', 'pending', 'reject'}:
                raise ValueError('点云筛选状态只能是保留、待定或淘汰')
            row = self.row(payload.get('id'))
            result = self.results.get(row['relative_dir'])
            if not result:
                raise ValueError('请先生成或打开当前采集的点云')
            stage = payload.get('stage')
            if stage is not None:
                if not isinstance(stage, str) or stage not in STAGES:
                    raise ValueError('请选择点云清理、坐标对齐或测量复核阶段')
                status = workflow_state(result)[stage]
                if not status['available']:
                    raise ValueError('前序阶段已淘汰或尚未对齐，不能筛选本阶段')
                if decision == 'keep' and not status['has_output']:
                    raise ValueError('请先完成本阶段处理，再标记保留')
                if 'stage_version' in payload and payload['stage_version'] != status['version']:
                    raise ValueError('本阶段数据已变化，请刷新后重新确认')
            record = cloud_review_record(result, row['relative_dir'], decision, stage)
            settings = copy.deepcopy(self.settings)
            settings.setdefault('cloud_reviews', {}).setdefault(str(self.results_root), {})[row['relative_dir']] = record
            atomic_json(self.state_dir/'settings.json', settings)
            self.settings = settings
            result['cloud_review'] = record
            return self.state()

    def state(self):
        from .native_picker import supported
        with self.lock:
            merged = {r['relative_dir']: dict(r) for r in self.rows}
            for rel, result in self.results.items():
                r = result['report']
                row = merged.setdefault(rel, {'id': hashlib.sha256(rel.encode()).hexdigest()[:20],
                    'relative_dir': rel, 'pig_id': str(r.get('pig_id', '未知')), 'capture_id': r.get('capture_id', Path(rel).name),
                    'reviewed': r.get('reviewed', False), 'status': r.get('segmentation_status'), 'missing': []})
                row.update(has_cloud=True, points=r['stats']['points'], cloud_reviewed=r.get('reviewed', False),
                           cloud_input_mode=input_mode(r), cloud_ply=primary_ply(r),
                           cloud_camera=r.get('camera', {}),
                           **cloud_review_state(result), **current_cloud(result), **measurement_state(result),
                           workflow_stages=workflow_state(result),
                           has_alignment=result.get('has_alignment', False),
                           alignment_cached=result.get('alignment_cached', False),
                           alignment_revision=result.get('alignment_revision', ''),
                           alignment_output=str(result['folder']) if result.get('has_alignment') else '',
                           has_cleaning=bool(result.get('processing')),
                           processing_revision=result.get('processing', {}).get('record', {}).get('revision', ''),
                           cleaning_digest=result.get('processing', {}).get('record', {}).get('cleaning_digest', ''),
                           removed_points=result.get('processing', {}).get('record', {}).get('removed_points', 0),
                           infrared_available=r.get('infrared_available', True),
                           has_ir_file=(result['folder']/'cloud_ir_mm.ply').is_file())
            frames = list(merged.values())
            for row in frames:
                row.setdefault('has_cloud', False)
                row.setdefault('cloud_decision', 'pending')
                row.setdefault('validation_errors', [])
                row.setdefault('excluded', False)
                row.setdefault('input_loaded', False)
            return {'dataset_id': self.dataset_id, 'source': str(self.source or ''),
                    'segmentation': str(self.segmentation or ''), 'results': str(self.results_root or ''),
                    'last_output': str(self.last_output or ''),
                    'frames': frames, 'pigs': len({r['pig_id'] for r in frames}),
                    'reviewed': sum(r.get('reviewed') is True for r in frames),
                    'clouds': len(self.results), 'native_picker': supported(),
                    'aligned': sum(r.get('has_alignment', False) for r in self.results.values()),
                    'results_match_inputs': self.results_match_inputs,
                    'alignment_source': self.settings.get('alignment_sources', {}).get(str(self.results_root), '') or str(self.source or self.settings.get('source', '')),
                    'export_parent': self.settings.get('export_parent', str(DEFAULT_OUTPUT_ROOT)),
                    'suggested_name': '点云成果_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19],
                    'job': copy.deepcopy(self.job), 'warning': self.warning,
                    'validation': self.validation_report()['summary']}

    def load(self, payload, remember=True):
        source, seg = directory(payload.get('source')), directory(payload.get('segmentation'))
        independent_paths(source, seg, self.state_dir)
        if not (seg/'dataset.json').is_file():
            raise ValueError('分割结果目录缺少 dataset.json，请选择包含该文件的成果根目录')
        ds = read_json(seg/'dataset.json')
        rows, ids, relatives = [], set(), set()
        if not isinstance(ds.get('frames'), list) or not ds['frames']:
            raise ValueError('分割结果清单中没有采集记录')
        declared_mode = input_mode(ds) if ds.get('input_mode') else None
        for entry in ds['frames']:
            rel = entry['relative_dir']
            s, m = within(source, rel), within(seg, rel)
            if rel in relatives: raise ValueError('分割清单有重复相对路径')
            relatives.add(rel)
            frame_id = hashlib.sha256(rel.encode()).hexdigest()[:20]
            if frame_id in ids: raise ValueError('采集编号重复')
            ids.add(frame_id)
            rows.append({'id': frame_id, 'relative_dir': rel, 'pig_id': str(entry['pig_id']),
                         'capture_id': entry['capture_id'], **check_frame(s, m, declared_mode)})
        with self.lock:
            self.idle()
            excluded = self.settings.get('skipped_frames', {}).get(self.exclusion_key(source, seg), [])
            for row in rows: row['excluded'] = row['relative_dir'] in excluded
            changed = self.source != source or self.segmentation != seg
            self.source, self.segmentation, self.rows = source, seg, rows
            self.dataset_id = secrets.token_hex(12)
            # A validated reload starts a fresh workspace, even when paths are unchanged.
            # Keep failures visible until this succeeds; active jobs remain protected by idle().
            self.job = None
            self.stop_event.clear()
            if changed or not self.results_match_inputs:
                self.results, self.results_root, self.last_output = {}, None, None
            self.results_match_inputs = True
            self.warning = ''
            if remember:
                self.settings.update(source=str(source), segmentation=str(seg))
                if not self.results_root: self.settings.pop('results', None)
                self.remember()
            return self.state()

    def open_results(self, value, remember=True):
        root = directory(value)
        results = {}
        storage_map = None
        stage_locations = {}
        manifest_file = root/'pointcloud_dataset.json'
        if manifest_file.is_file():
            if manifest_file.is_symlink():
                raise ValueError('点云清单不能使用符号链接')
            manifest = read_json(manifest_file)
            if isinstance(manifest, dict) and manifest.get('package_kind') == 'stage_products':
                raise ValueError('这是按阶段导出的精简成果包，仅包含各阶段成果；继续编辑请选择原成果目录或“按采集合并”的完整导出目录')
            if isinstance(manifest, dict) and (manifest.get('group_by_decision') is True or manifest.get('export_layout') == 'split'):
                storage_map, relatives = {}, set()
                if not isinstance(manifest.get('frames'), list):
                    raise ValueError('分类点云清单缺少采集记录')
                for entry in manifest['frames']:
                    if not isinstance(entry, dict):
                        raise ValueError('分类点云清单的采集记录无效')
                    rel, storage = entry.get('relative_dir'), entry.get('storage_relative_dir')
                    if not isinstance(rel, str) or not isinstance(storage, str):
                        raise ValueError('分类点云清单的采集路径无效')
                    within(root, rel)
                    within(root, storage)
                    if storage in storage_map or rel in relatives:
                        raise ValueError('分类点云清单中有重复采集或存储路径')
                    storage_map[storage] = rel
                    relatives.add(rel)
                    if manifest.get('export_layout') == 'split':
                        locations = entry.get('stage_storage_relative_dirs')
                        if not isinstance(locations, dict) or not locations or any(k not in STAGES for k in locations):
                            raise ValueError('分阶段清单的阶段路径无效')
                        for key, location in locations.items():
                            if not isinstance(location, str) or not location.startswith(STAGE_LABELS[key]+'/'):
                                raise ValueError('分阶段清单的存储路径无效')
                            within(root, location)
                        if storage not in locations.values():
                            raise ValueError('分阶段清单的主采集路径无效')
                        stage_locations[rel] = locations
        for p in sorted(root.rglob('pointcloud.json')):
            storage = p.parent.relative_to(root).as_posix()
            if storage_map is not None and storage not in storage_map:
                continue
            rel = storage_map[storage] if storage_map is not None else storage
            folder = within(root, storage)
            if p.is_symlink() or any((root/x).is_symlink() for x in p.relative_to(root).parents if str(x) != '.'):
                raise ValueError('点云目录不能包含符号链接')
            if not all((folder/n).is_file() and not (folder/n).is_symlink() for n in COMMON_RESULT_FILES):
                continue
            report = read_json(p)
            if not all((folder/n).is_file() and not (folder/n).is_symlink() for n in required_result_files(report)):
                continue
            if report.get('coordinate_system') != 'native_depth_camera' or report.get('unit') != 'mm':
                raise ValueError('该目录的点云不是当前毫米相机坐标格式')
            if rel in results:
                raise ValueError('点云成果含重复采集，请分别打开成果目录')
            aligned = alignment_status(folder)
            results[rel] = {'folder': folder, 'report': report, 'has_alignment': aligned,
                            'alignment_reference_id': read_json(folder/'alignment.json')['reference_id'] if aligned else '',
                            'alignment_revision': sha256(folder/'alignment.json') if aligned else ''}
        if not results: raise ValueError('目录中没有完整点云成果，请选择包含 pig_编号/采集时间 的点云根目录')
        results = dict(sorted(results.items()))
        with self.lock:
            self.idle()
            # Aligned copies are attached per capture; opening the original batch
            # restores them without replacing the batch with a one-capture export.
            invalid = 0
            for rel, entry in self.settings.get('aligned_results', {}).get(str(root), {}).items():
                if rel not in results:
                    continue
                try:
                    original = results[rel]['folder']
                    output = directory(entry['output'])
                    storage = entry.get('storage_relative_dir', rel)
                    aligned = within(output, storage)
                    if any((output/p).is_symlink() for p in Path(storage).parents if str(p) != '.') or aligned.is_symlink():
                        raise ValueError('对齐成果不能包含符号链接')
                    if (sha256(original/'points.npz') != entry['input_cloud_sha256']
                            or sha256(original/'pointcloud.json') != entry['input_report_sha256']
                            or sha256(aligned/'points.npz') != entry['input_cloud_sha256']
                            or sha256(aligned/'pointcloud.json') != entry['input_report_sha256']
                            or not alignment_status(aligned)):
                        raise ValueError('对齐成果与原点云不匹配')
                    results[rel] = {**results[rel], 'folder': aligned, 'has_alignment': True,
                                    'alignment_cached': entry.get('cached') is True,
                                    'alignment_reference_id': read_json(aligned/'alignment.json')['reference_id'],
                                    'alignment_revision': sha256(aligned/'alignment.json')}
                except (ValueError, OSError, KeyError, TypeError):
                    invalid += 1
            review_invalid = 0
            for rel, result in results.items():
                record = self.settings.get('cloud_reviews', {}).get(str(root), {}).get(rel)
                saved_locally = record is not None
                try:
                    review_file = result['folder']/'cloud_review.json'
                    if record is None and review_file.is_file() and not review_file.is_symlink():
                        record = read_json(review_file)
                    if record is not None:
                        if not valid_cloud_review(record, result['folder'], rel, result['report']):
                            raise ValueError('筛选记录与点云不匹配')
                        if rel in stage_locations and not saved_locally:
                            records = {}
                            for key in STAGES:
                                if key not in stage_locations[rel]:
                                    continue
                                relative = Path(stage_locations[rel][key])
                                folder = within(root, relative)
                                if any((root/part).is_symlink() for part in (relative, *relative.parents) if str(part) != '.'):
                                    raise ValueError('分阶段成果不能包含符号链接')
                                path = folder/'cloud_review.json'
                                if path.is_symlink():
                                    raise ValueError('分阶段筛选记录不能使用符号链接')
                                saved = read_json(path)
                                if (not valid_cloud_review(saved, folder, rel, result['report'])
                                        or saved['points_sha256'] != record['points_sha256']
                                        or key not in saved.get('stages', {})):
                                    raise ValueError('分阶段筛选记录与点云不匹配')
                                records[key] = saved['stages'][key]
                            record = {**record, 'stages': records}
                        result['cloud_review'] = record
                except (ValueError, OSError, KeyError, TypeError):
                    review_invalid += 1
            self.results, self.results_root = results, root
            processing_invalid = self.processing.restore()
            # Equal relative paths can occur in different cameras and segmentation
            # jobs. Opening results must not authorize generation from other inputs.
            self.results_match_inputs = bool(self.source and self.segmentation)
            for rel, result in results.items():
                meta_path = within(self.segmentation, rel)/'segmentation.json' if self.segmentation else None
                if not meta_path or not meta_path.is_file() or sha256(meta_path) != result['report'].get('segmentation_json_sha256'):
                    self.results_match_inputs = False
                    break
            self.last_output = root
            self.job = saved_job(root)
            self.dataset_id = secrets.token_hex(12)
            self.warning = f'{invalid} 次已保存对齐成果缺失或与原点云不匹配，已退回相机点云，请重新对齐。' if invalid else ''
            if review_invalid:
                self.warning += f' {review_invalid} 次点云筛选记录与当前文件不匹配，已恢复为待定。'
            if processing_invalid:
                self.warning += f' {processing_invalid} 份清理记录无法匹配或已损坏，未应用；请在清理页复核。'
            if remember:
                self.settings['results'] = str(root)
                self.remember()
            return self.state()

    def attach_alignment(self, ctx, target, cached=False):
        """Select a saved copy for one capture, retaining the loaded batch identity."""
        rel = ctx['row']['relative_dir']
        dest = within(target, rel)
        self.settings.setdefault('aligned_results', {}).setdefault(str(self.results_root), {})[rel] = {
            'output': str(target), 'input_cloud_sha256': ctx['input_cloud_sha256'],
            'input_report_sha256': ctx['input_report_sha256'], 'cached': cached}
        self.results[rel] = {**self.results[rel], 'folder': dest, 'has_alignment': True,
                             'alignment_cached': cached,
                             'alignment_reference_id': read_json(dest/'alignment.json')['reference_id'],
                             'alignment_revision': sha256(dest/'alignment.json')}

    def row(self, frame_id):
        row = next((r for r in self.state()['frames'] if r['id'] == frame_id), None)
        if not row: raise ValueError('找不到采集，请重新载入目录')
        return row

    def media(self, frame_id, filename):
        if filename not in RESULT_FILES: raise ValueError('不支持的成果文件')
        with self.lock:
            row = self.row(frame_id)
            result = self.results.get(row['relative_dir'])
            if not result: raise ValueError('该采集尚未生成点云')
            p = result['folder']/filename
            if p.is_symlink() or p.resolve().parent != result['folder'].resolve():
                raise ValueError('成果路径不合法')
            return p

    def start(self, payload, mode):
        with self.lock:
            self.idle()
            if payload.get('dataset_id') != self.dataset_id:
                raise ValueError('目录已切换，请刷新后再操作')
            ids = payload.get('ids')
            if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
                raise ValueError('请先勾选采集，采集编号不能重复')
            rows = [self.row(i) for i in ids]
            if mode == 'generate' and (not self.source or not self.segmentation):
                raise ValueError('请先选择并载入原始目录与分割结果目录')
            if mode == 'generate' and not self.results_match_inputs:
                raise ValueError('当前成果与载入的分割不是同一批输入；请在目录设置中重新核对载入后生成')
            parent = directory(payload.get('parent'))
            target = parent/folder_name(payload.get('name'))
            protected = [p for p in [self.source, self.segmentation, self.state_dir, self.results_root] if p]
            protected.extend(r['folder'] for r in self.results.values())
            # Validate target against each root without requiring those roots to be independent of each other.
            for p in protected: independent_paths(p, target)
            if target.exists() or target.is_symlink():
                raise ValueError('成果文件夹已存在，请换一个名称，避免覆盖已有文件')
            allow_draft = payload.get('allow_draft') is True
            if mode == 'generate':
                usable = [r for r in rows if not r['missing'] and not r.get('validation_errors')
                          and not r.get('excluded') and r.get('decision') != 'reject'
                          and (allow_draft or r['reviewed'])]
                if not usable: raise ValueError('所选采集中没有可生成项；请查看核对失败或跳过标记，确认分割或勾选“允许初稿预览”')
            elif mode == 'export':
                export_layout = payload.get('export_layout', 'merged')
                if export_layout not in ('merged', 'split'):
                    raise ValueError('请选择按阶段拆分或合并存放')
                stages = payload.get('stages')
                if stages is None and export_layout == 'split':
                    stages = list(STAGES)
                if stages is None and any('stages' in (self.results.get(r['relative_dir'], {}).get('cloud_review') or {}) for r in rows):
                    stages = list(STAGES)
                if stages is not None and (not isinstance(stages, list) or not stages
                        or any(not isinstance(s, str) or s not in STAGES for s in stages)
                        or len(stages) != len(set(stages))):
                    raise ValueError('请至少选择一个有效的导出阶段')
                decisions = payload.get('decisions')
                if decisions is not None and (not isinstance(decisions, list) or not decisions
                        or any(not isinstance(d, str) or d not in REVIEW_GROUPS for d in decisions)
                        or len(decisions) != len(set(decisions))):
                    raise ValueError('请勾选有效的导出状态：保留、待定、淘汰')
                if payload.get('group_by_decision') is True and decisions is None:
                    raise ValueError('分类导出需要明确选择保留／待定／淘汰状态')
                if any(r['relative_dir'] not in self.results for r in rows):
                    raise ValueError('导出范围含未生成采集，请改选“已生成”范围或先生成点云')
                for row in rows:
                    result = self.results[row['relative_dir']]
                    review = result.get('cloud_review')
                    if review and not valid_cloud_review(review, result['folder'], row['relative_dir'], result['report']):
                        raise ValueError('点云已变化，请重新打开成果目录并核对筛选状态')
                    if stages is not None:
                        if payload.get('aligned_only') is True and not alignment_status(result['folder']):
                            raise ValueError('导出范围含未完成对齐或文件已变化的采集')
                        plan = stage_export_plan(result, stages, decisions or ['keep', 'pending'])
                        if not plan:
                            raise ValueError('导出范围含没有所选阶段成果或状态的采集，请更新导出范围')
                        row.update(exported_stages=plan, cloud_decision=final_decision(plan))
                        continue
                    if decisions is not None and row['cloud_decision'] not in decisions:
                        raise ValueError('导出范围含未勾选状态的点云，请更新导出范围')
                    if decisions is None and row['cloud_decision'] == 'reject':
                        raise ValueError('导出范围含已淘汰点云，请调整导出范围')
                    if payload.get('aligned_only') is True:
                        if not alignment_status(result['folder']):
                            raise ValueError('导出对齐点云的范围含未完成对齐或文件已变化的采集')
                        if decisions is None and row['cloud_decision'] != 'keep' and payload.get('include_pending') is not True:
                            raise ValueError('默认只导出已保留且已对齐的点云；待定项需显式勾选包含待定')
            else: raise ValueError('任务类型无效')
            snapshot = {r['relative_dir']: self.results.get(r['relative_dir']) for r in rows}
            scope_ids = payload.get('scope_ids', ids) if mode == 'export' else ids
            if (not isinstance(scope_ids, list) or len(scope_ids) != len(set(scope_ids))
                    or any(not isinstance(i, str) for i in scope_ids) or not set(ids) <= set(scope_ids)):
                raise ValueError('导出筛选记录的采集范围无效')
            review_scope = [self.row(i) for i in scope_ids] if mode == 'export' else []
            self.stop_event.clear()
            self.job = {'id': secrets.token_hex(10), 'mode': mode, 'status': 'running', 'done': 0,
                        'aligned_only': mode == 'export' and payload.get('aligned_only') is True,
                        'group_by_decision': mode == 'export' and payload.get('group_by_decision') is True,
                        'decisions': decisions if mode == 'export' else [],
                        'stages': stages if mode == 'export' else None,
                        'export_layout': export_layout if mode == 'export' else 'merged',
                        'total': len(rows), 'success': 0, 'skipped': [], 'errors': [], 'output': str(target),
                        'current': '准备生成' if mode == 'generate' else '准备按文件夹导出'}
            self.settings.update(export_parent=str(parent))
            self.remember()
            self.worker = threading.Thread(target=self._run, args=(rows, snapshot, target, mode, allow_draft, review_scope), daemon=False)
            self.worker.start()
            return self.state()

    def stop(self):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                self.stop_event.set()
                self.job.update(status='stopping', current='正在完成当前采集，随后停止')
            return self.state()

    def _export_copy(self, source, dest, row, plan):
        """Write one portable capture copy, projected onto its requested stages."""
        rel = row['relative_dir']
        dest.parent.mkdir(parents=True, exist_ok=True)
        safe_copy_tree(source['folder'], dest)
        if plan is not None:
            source = stage_export_result(dest, source, plan, rel)
            source['cloud_review']['processing_version'] = processing_version(source)
        report = read_json(dest/'pointcloud.json')
        normalized = export_metadata(report)
        if not normalized.get('camera'):
            # Optional provenance upgrade; portable exports never require raw inputs.
            raw_record = Path(report.get('source_dir', ''))/'reconstruction.json'
            try:
                if report.get('source_sha256', {}).get('reconstruction.json') == sha256(raw_record):
                    normalized['camera'] = camera_identity(read_json(raw_record))
            except (OSError, ValueError, TypeError):
                pass
        other = 'cloud_ir_mm.ply' if input_mode(report) == 'rgb' else 'cloud_rgb_mm.ply'
        if (dest/other).exists(): (dest/other).unlink()
        if normalized != report and not source.get('has_alignment') and not source.get('processing'):
            write_json(dest/'pointcloud.json', normalized)
        # Folder exports use the current layout/theme even when
        # the measured point data and metadata need no upgrade.
        with np.load(dest/'points.npz', allow_pickle=False) as data:
            write_viewer(dest/'preview.html', data, normalized)
        if source.get('has_alignment'):
            with np.load(dest/'body_points.npz', allow_pickle=False) as data:
                (dest/'body_preview.html').write_text(render_body_view(data, read_json(dest/'alignment.json')), encoding='utf-8')
        write_cloud_review(dest, source)
        self.processing.export_to(dest, source,
            include_measurement=plan is None or plan.get('review', {}).get('has_output', False))
        if plan is not None:
            row['exported_stages'] = write_stage_products(dest, source, plan)
        if self.job.get('aligned_only') and plan is None and not alignment_status(dest):
            raise ValueError('复制后的对齐成果不完整或校验不匹配，请重新核对后导出')
        report = normalized
        current = write_current_cloud(dest, source, row.get('cloud_decision', 'pending'))
        return {**row, **current, 'has_alignment': alignment_status(dest),
                'has_cleaning': (dest/'pointcloud_cleaning.json').is_file()}, report

    def _export_stage_products(self, source, dest, row, plan):
        """Create one compact delivery package, never a partial editable capture."""
        key, status = next(iter(plan.items()))
        inputs = {'points': source['folder']/'points.npz', 'report': source['folder']/'pointcloud.json'}
        if key != 'cleaning' and status['has_output']:
            inputs['alignment'] = source['folder']/'alignment.json'
        if source.get('processing'):
            for name in ('pointcloud_cleaning.json', 'point_edits.npz'):
                inputs[name] = source['processing']['folder']/name
        if any(path.is_symlink() or not path.is_file() for path in inputs.values()):
            raise ValueError('阶段成果的来源文件缺失或路径不合法')
        hashes = {name: sha256(path) for name, path in inputs.items()}
        dest.mkdir(parents=True)
        files = self.processing.export_stage_to(dest, source, key, status['has_output'])
        if any(not (dest/name).is_file() for name in files.values()):
            raise ValueError(f'{STAGE_LABELS[key]}成果文件不完整')
        products = {key: {'label': STAGE_LABELS[key], **status, 'files': files}}
        coordinates = {'cleaning': 'native_depth_camera', 'alignment': 'ground_body',
                       'review': 'measurement'}[key] if status['has_output'] else 'none'
        metadata = {'schema_version': 1, 'package_kind': 'stage_products', 'editable': False,
            'stage': key, 'stage_label': STAGE_LABELS[key],
            **{name: row[name] for name in ('relative_dir', 'pig_id', 'capture_id')},
            'input_mode': input_mode(source['report']), 'unit': 'mm',
            'coordinate_system': coordinates, 'source_sha256': hashes,
            'camera': source['report'].get('camera', {}),
            'decision': status['decision'], 'version': status['version'],
            'has_output': status['has_output'], 'files': files}
        write_json(dest/'stage_result.json', metadata)
        write_json(dest/'workflow_stages.json', {'schema_version': 1, 'package_kind': 'stage_products',
                                                'stages': products})
        if any(sha256(path) != hashes[name] for name, path in inputs.items()):
            raise ValueError('导出期间来源文件发生变化，请重新核对后导出')
        current = {'current_ply': files.get('primary_ply', ''), 'current_npz': files.get('primary_npz', ''),
            'current_coordinates': coordinates, 'current_points': current_cloud(source)['current_points'],
            'measurement_ready': False, 'measurement_usable_slices': 0, 'measurement_slice_count': 0}
        if key == 'review' and status['has_output']:
            summary = read_json(dest/'measurement_summary.json')
            current.update(measurement_ready=summary['measurement_ready'],
                measurement_usable_slices=summary['usable_slices'], measurement_slice_count=summary['slice_count'])
        return {**row, **current, 'exported_stages': products,
            'has_alignment': key == 'alignment' and status['has_output'],
            'has_cleaning': key == 'cleaning' and status['has_output']}, source['report']

    def _run(self, rows, snapshot, target, mode, allow_draft, review_scope):
        stage, completed, stage_copies = None, [], []
        split = self.job['export_layout'] == 'split'
        try:
            stage = Path(tempfile.mkdtemp(prefix='.pointcloud-export-', dir=target.parent))
            roots = [stage/STAGE_LABELS[key] for key in self.job['stages']] if split else [stage]
            for root in roots:
                root.mkdir(exist_ok=True)
                if self.job['group_by_decision']:
                    for decision in self.job['decisions']:
                        (root/REVIEW_GROUPS[decision]).mkdir()
            for row in rows:
                if self.stop_event.is_set(): break
                rel = row['relative_dir']
                storage = (Path(REVIEW_GROUPS[row['cloud_decision']])/rel).as_posix() if self.job['group_by_decision'] else rel
                dest = within(stage, storage)
                written = [dest] if not split else []
                with self.lock: self.job['current'] = rel
                try:
                    if mode == 'generate':
                        if row.get('excluded'):
                            raise SkipCapture('工作台手动标记为跳过')
                        if row.get('validation_errors'):
                            raise SkipCapture('核对失败：'+'；'.join(row['validation_errors']))
                        # Re-read review state in reconstruct; old list never authorizes a changed mask.
                        meta = read_json(within(self.segmentation, rel)/'segmentation.json')
                        if meta.get('decision') == 'reject':
                            raise SkipCapture('分割阶段标记为淘汰')
                        if not allow_draft and not (meta.get('reviewed') is True and meta.get('status') == 'reviewed'):
                            raise SkipCapture('分割尚未确认')
                        if row['missing']: raise ValueError('缺少文件：'+', '.join(row['missing']))
                        data, report, mask = reconstruct(within(self.source, rel), within(self.segmentation, rel), allow_draft)
                        export_frame(within(stage, rel), data, report, mask)
                        write_viewer(within(stage, rel)/'preview.html', data, report)
                    else:
                        copies = []
                        plans = ({key: value} for key, value in row['exported_stages'].items()) if split else [row.get('exported_stages')]
                        for plan in plans:
                            key = next(iter(plan)) if split else ''
                            decision = final_decision(plan) if split else row['cloud_decision']
                            relative = Path(REVIEW_GROUPS[decision])/rel if self.job['group_by_decision'] else Path(rel)
                            storage = (Path(STAGE_LABELS[key])/relative).as_posix() if split else relative.as_posix()
                            dest = within(stage, storage)
                            written.append(dest)
                            export = self._export_stage_products if split else self._export_copy
                            capture, report = export(snapshot[rel], dest,
                                {**row, 'cloud_decision': decision, 'storage_relative_dir': storage}, plan)
                            copies.append((capture, report))
                        if split:
                            stage_copies.extend(copies)
                            all_products = {key: value for capture, _ in copies for key, value in capture['exported_stages'].items()}
                            locations = {next(iter(capture['exported_stages'])): capture['storage_relative_dir'] for capture, _ in copies}
                            capture = {**copies[-1][0], 'exported_stages': all_products,
                                       'stage_storage_relative_dirs': locations}
                        else:
                            capture = copies[0][0]
                    if mode == 'generate':
                        current = write_current_cloud(dest, {'report': report}, 'pending')
                        capture = {**row, **current, 'storage_relative_dir': storage,
                                   'has_alignment': False, 'has_cleaning': False}
                    completed.append((capture, report))
                    with self.lock: self.job['success'] += 1
                except SkipCapture as exc:
                    with self.lock: self.job['skipped'].append({'relative_dir': rel, 'reason': str(exc)})
                except Exception as exc:
                    # Remove partially written frame files; never publish an incomplete capture folder.
                    for bad in written:
                        if bad.exists(): shutil.rmtree(bad)
                    with self.lock: self.job['errors'].append({'relative_dir': rel, 'error': str(exc)})
                    if mode == 'generate':
                        with self.lock:
                            for original in self.rows:
                                if original['id'] == row['id']:
                                    original['validation_status'] = 'failed'
                                    original['validation_errors'] = ['生成核对失败：'+str(exc)]
                finally:
                    with self.lock: self.job['done'] += 1
            if not completed:
                reason = (self.job['errors'] or self.job['skipped'] or [{}])[0]
                detail = reason.get('error') or reason.get('reason') or '没有完成任何采集'
                raise ValueError('没有生成或导出成功的点云；首项原因：'+detail+'。目标文件夹未创建，详见逐项记录')
            status = 'stopped' if self.stop_event.is_set() else ('completed_with_errors' if self.job['errors'] else 'completed')
            coordinates = {row['current_coordinates'] for row, _ in completed}
            manifest = {'schema_version': 1, 'source': str(self.source or ''), 'segmentation': str(self.segmentation or ''),
                        'output': str(target), 'coordinate_system': next(iter(coordinates)) if len(coordinates) == 1 else 'per_frame',
                        'original_coordinate_system': 'native_depth_camera', 'unit': 'mm',
                        'export_policy': EXPORT_POLICY,
                        'frames': [{**row, 'has_cloud': True, 'input_mode': input_mode(report),
                                    'primary_ply': row['current_ply'], 'primary_npz': row['current_npz'],
                                    'original_primary_ply': '' if split else primary_ply(report), 'camera': report.get('camera', {}),
                                    'reviewed': report['reviewed'], 'points': report['stats']['points'],
                                    'has_alignment': 'alignment' in row['exported_stages'] and row['exported_stages']['alignment']['has_output'] if split else alignment_status(within(stage, row['storage_relative_dir'])),
                                    'has_cleaning': 'cleaning' in row['exported_stages'] if split else (within(stage, row['storage_relative_dir'])/'pointcloud_cleaning.json').is_file(),
                                    'has_features': (within(stage, row['storage_relative_dir'])/'geometry_features.json').is_file(),
                                    'cloud_decision': row.get('cloud_decision', 'pending') if mode == 'export' else 'pending'} for row, report in completed]}
            manifest['aligned_only'] = self.job['aligned_only']
            manifest['group_by_decision'] = self.job['group_by_decision']
            manifest['decisions'] = self.job['decisions']
            manifest['stages'] = self.job['stages']
            manifest['export_layout'] = self.job['export_layout']
            if split:
                manifest.update(package_kind='stage_products', editable=False,
                    coordinate_system='per_stage',
                    export_policy='only each stage\'s current products and lightweight provenance; no original dependencies or edit history')
                for frame in manifest['frames']:
                    frame['has_cloud'] = any('primary_ply' in value['files'] for value in frame['exported_stages'].values())
            write_json(stage/'pointcloud_dataset.json', manifest)
            if mode == 'export':
                from .features import aggregate
                if not split:
                    aggregate(stage, completed)
                exported = {row['relative_dir']: row['storage_relative_dir'] for row, _ in completed}
                completed_rows = {row['relative_dir']: row for row, _ in completed}
                screening = []
                for row in review_scope:
                    actual = completed_rows.get(row['relative_dir'], row)
                    decision = actual.get('cloud_decision', 'pending')
                    reason = '' if row['relative_dir'] in exported else (
                        '未勾选此状态' if self.job['decisions'] is not None and decision not in self.job['decisions']
                        else '尚未对齐' if self.job['aligned_only'] and not row.get('has_alignment')
                        else '已淘汰' if decision == 'reject' and self.job['decisions'] is None
                        else '不在本次点云导出范围或导出失败')
                    screening.append({'relative_dir': row['relative_dir'], 'pig_id': row['pig_id'],
                        'capture_id': row['capture_id'], 'input_mode': row.get('cloud_input_mode') or row.get('input_mode'),
                        'decision': decision, 'status': {'keep':'保留','pending':'待定','reject':'淘汰'}[decision],
                        'has_alignment': actual.get('has_alignment', False), 'exported': row['relative_dir'] in exported,
                        'has_cleaning': actual.get('has_cleaning', False), 'removed_points': row.get('removed_points', 0),
                        **{k: actual.get(k, '') for k in ('current_ply', 'current_npz', 'current_coordinates', 'current_points')},
                        'cloud_review_stale': row.get('cloud_review_stale', False),
                        **{k: completed_rows.get(row['relative_dir'], row).get(k, False if k == 'measurement_ready' else 0)
                           for k in ('measurement_ready', 'measurement_usable_slices', 'measurement_slice_count')},
                        'storage_relative_dir': exported.get(row['relative_dir'], ''),
                        'reason': reason})
                write_json(stage/'cloud_screening.json', {'schema_version': 1, 'results': str(self.results_root),
                    'frames': screening})
                with (stage/'cloud_screening.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(screening[0]))
                    writer.writeheader()
                    writer.writerows(screening)
                if self.job['stages'] is not None:
                    stage_rows = []
                    for row in review_scope:
                        states = row.get('workflow_stages', {})
                        exported_stages = completed_rows.get(row['relative_dir'], {}).get('exported_stages', {})
                        for key in self.job['stages']:
                            value = states.get(key, {})
                            stage_rows.append({'relative_dir': row['relative_dir'], 'pig_id': row['pig_id'],
                                'capture_id': row['capture_id'], 'stage': key, 'stage_label': STAGE_LABELS[key],
                                'decision': value.get('decision', 'pending'),
                                'status': REVIEW_GROUPS[value.get('decision', 'pending')],
                                'exported': key in exported_stages,
                                'blocked_by': value.get('blocked_by', ''),
                                'has_output': value.get('has_output', False),
                                'storage_relative_dir': completed_rows.get(row['relative_dir'], {}).get('stage_storage_relative_dirs', {}).get(key, exported.get(row['relative_dir'], '')) if key in exported_stages else '',
                                'files': exported_stages.get(key, {}).get('files', {})})
                    write_json(stage/'stage_screening.json', {'schema_version': 1, 'stages': self.job['stages'], 'frames': stage_rows})
                    with (stage/'stage_screening.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                        writer = csv.DictWriter(stream, fieldnames=list(stage_rows[0]))
                        writer.writeheader()
                        writer.writerows(stage_rows)
            if split:
                for key in self.job['stages']:
                    root = stage/STAGE_LABELS[key]
                    frames = []
                    for capture, report in stage_copies:
                        if key not in capture['exported_stages']:
                            continue
                        capture = {**capture, 'storage_relative_dir': Path(capture['storage_relative_dir']).relative_to(STAGE_LABELS[key]).as_posix()}
                        frames.append((capture, report))
                    if not frames:
                        shutil.rmtree(root)
                        continue
                    child_frames = []
                    for capture, _ in frames:
                        original = next(r for r in manifest['frames'] if r['relative_dir'] == capture['relative_dir'])
                        child = {**original, **capture}
                        child.pop('stage_storage_relative_dirs', None)
                        child.update(primary_ply=capture['current_ply'], primary_npz=capture['current_npz'],
                                     has_cloud=bool(capture['current_ply']),
                                     has_features=(root/capture['storage_relative_dir']/'geometry_features.json').is_file())
                        child_frames.append(child)
                    child_manifest = {**manifest, 'frames': child_frames, 'stages': [key],
                                      'output': str(target/STAGE_LABELS[key])}
                    coordinates = {r['current_coordinates'] for r in child_frames}
                    child_manifest['coordinate_system'] = next(iter(coordinates)) if len(coordinates) == 1 else 'per_frame'
                    write_json(root/'pointcloud_dataset.json', child_manifest)
                    measured_frames = [(capture, report) for capture, report in frames
                                       if key == 'review' and capture['exported_stages'][key]['has_output']]
                    if measured_frames:
                        aggregate(root, measured_frames)
                    records = [{**r, 'storage_relative_dir': Path(r['storage_relative_dir']).relative_to(STAGE_LABELS[key]).as_posix() if r['storage_relative_dir'] else ''}
                               for r in stage_rows if r['stage'] == key]
                    write_json(root/'stage_screening.json', {'schema_version': 1, 'stages': [key], 'frames': records})
                    with (root/'stage_screening.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
                        writer.writeheader()
                        writer.writerows(records)
            if mode == 'generate':
                write_json(stage/'input_validation.json', self.validation_report())
            final_job = {**copy.deepcopy(self.job), 'status': status, 'current': '已按原相对目录保存全部成果'}
            write_json(stage/'export.json', final_job)
            index_frames = [{**row, 'primary_ply': row['current_ply']} for row, _ in stage_copies] if split else manifest['frames']
            links = ''.join(f'<li><a href="{quote(row["storage_relative_dir"], safe="/")}/{"processing_preview.html" if row.get("has_cleaning") else "body_preview.html" if row["has_alignment"] else "preview.html"}">{escape(row["storage_relative_dir"])}</a> · 主点云 <a href="{quote(row["storage_relative_dir"], safe="/")}/{row["primary_ply"]}">{row["primary_ply"]}</a> · 当前 {row["current_points"]:,} 点 · {"测量已复核" if row["measurement_ready"] else "测量待复核"}</li>' for row in index_frames)
            feature_link = '<p><a href="geometry_features.csv">批量几何特征 CSV</a>；已导出的测量阶段目录含 geometry_features.json、profile_sequence.csv / npz（缺失值与复核掩码分别保存）。</p>' if mode == 'export' else ''
            description = '每次采集的 PLY、NPZ、检查图和生成记录均保存在对应文件夹。'
            if split:
                links = ''
                for row, _ in stage_copies:
                    key = next(iter(row['exported_stages']))
                    files = {**row['exported_stages'][key]['files'],
                             'stage_record': 'stage_result.json', 'workflow': 'workflow_stages.json'}
                    products = ' · '.join(f'<a href="{quote(row["storage_relative_dir"]+"/"+name, safe="/")}">{escape(name)}</a>'
                                          for name in files.values())
                    links += f'<li>{escape(row["storage_relative_dir"])} · {products}</li>'
                feature_link = ('<p><a href="'+quote(STAGE_LABELS['review'], safe='')+'/geometry_features.csv">测量复核批量几何特征 CSV</a></p>'
                                if (stage/STAGE_LABELS['review']/'geometry_features.csv').is_file() else '')
                description = '各阶段仅包含本阶段成果和轻量来源记录；测量复核不重复保存点云。此精简包用于交付和分析，继续编辑请使用原成果目录或按采集合并的完整导出目录。'
            if self.job['stages'] is not None:
                feature_link += '<p><a href="stage_screening.csv">各阶段筛选 CSV</a> · <a href="stage_screening.json">各阶段筛选 JSON</a>；各采集的 workflow_stages.json 列出阶段结论和成果文件。</p>'
            (stage/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>点云成果</title><style>body{font:16px system-ui;margin:40px;line-height:2}a{color:#167b6b}</style><h1>毫米点云成果</h1><p>'+description+'</p>'+feature_link+'<ul>'+links+'</ul>', encoding='utf-8')
            if target.exists() or target.is_symlink(): raise ValueError('目标文件夹已被创建，请换名重试')
            stage.rename(target)
            stage = None
            with self.lock:
                self.last_output = target
                if mode == 'generate':
                    self.results_match_inputs = True
                    self.results_root = target
                    self.results = {row['relative_dir']: {'folder': within(target, row['relative_dir']), 'report': report} for row, report in completed}
                    self.settings['results'] = str(target)
                else:
                    for row, _ in completed:
                        rel = row['relative_dir']
                        if self.job['stages'] is not None:
                            # A partial export cannot replace the full editable alignment association.
                            continue
                        if not snapshot[rel].get('alignment_cached'):
                            continue
                        association = self.settings.get('aligned_results', {}).get(str(self.results_root), {}).get(rel)
                        if association:
                            association.update(output=str(target), storage_relative_dir=row['storage_relative_dir'], cached=False)
                            self.results[rel] = {**self.results[rel], 'folder': within(target, row['storage_relative_dir']), 'alignment_cached': False}
                self.job.update(final_job)
                try: self.remember()
                except OSError: self.job['warning'] = '成果已保存，但未能记住本次路径'
        except Exception as exc:
            with self.lock: self.job.update(status='failed', error=str(exc), current='任务未完成')
        finally:
            if stage: shutil.rmtree(stage, ignore_errors=True)


class SkipCapture(Exception):
    pass
