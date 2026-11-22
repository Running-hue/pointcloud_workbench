"""Browser workflow: independent inputs, background jobs and folder-tree exports."""
from __future__ import annotations

import copy
import csv
from datetime import datetime
import hashlib
from html import escape
from pathlib import Path, PurePosixPath, PureWindowsPath
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

RESULT_FILES = {'points.npz', 'cloud_rgb_mm.ply', 'cloud_ir_mm.ply', 'pointcloud.json',
                'preview.html', 'mask_depth_check.png', 'rgb_projection_check.png',
                'rgb_registration_check.png', 'cloud_review.json'} | SIDE_FILES
REVIEW_GROUPS = {'keep': '保留', 'pending': '待定', 'reject': '淘汰'}


def valid_cloud_review(record, folder, relative_dir, report):
    return (isinstance(record, dict) and record.get('schema_version') == 1
            and record.get('decision') in {'keep', 'pending', 'reject'}
            and record.get('relative_dir') == relative_dir
            and record.get('input_mode') == input_mode(report)
            and record.get('points_sha256') == sha256(folder/'points.npz'))


def write_cloud_review(folder, result):
    """Write review metadata only into a new exported copy."""
    record = result.get('cloud_review')
    path = folder/'cloud_review.json'
    if record:
        if not valid_cloud_review(record, folder, record['relative_dir'], result['report']):
            raise ValueError('点云在筛选后发生变化，请重新核对保留／待定／淘汰状态')
        write_json(path, record)
    elif path.exists():
        path.unlink()


def cloud_review_record(result, relative_dir, decision):
    points = result['folder']/'points.npz'
    if points.is_symlink() or not points.is_file():
        raise ValueError('当前点云文件不存在或路径不合法')
    return {'schema_version': 1, 'decision': decision, 'relative_dir': relative_dir,
            'input_mode': input_mode(result['report']), 'points_sha256': sha256(points),
            'reviewed_at': datetime.now().astimezone().isoformat()}


def capture_input_mode(meta, expected):
    mode = input_mode(meta)
    if expected and mode != expected:
        labels = {'rgb': 'RGB', 'infrared': 'IR'}
        raise ValueError(f'分割清单与逐帧输入模式不一致（清单：{labels[expected]}，采集：{labels[mode]}）')
    return mode


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
                labels = {'rgb': 'RGB', 'infrared': 'IR'}
                errors.append(f'分割清单与逐帧输入模式不一致（清单：{labels[declared_mode]}，采集：{labels[mode]}）')
        except (ValueError, TypeError) as exc:
            mode = 'unknown'
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
        self.dataset_input_mode = None
        self.results = {}
        self.results_match_inputs = True
        self.source = self.segmentation = self.results_root = None
        self.last_output = None
        self.warning = ''
        self.input_warning = ''
        self.results_warning = ''
        self.settings = {}
        self.alignment = AlignmentService(self)
        settings_file = self.state_dir/'settings.json'
        if settings_file.is_file():
            try:
                self.settings = read_json(settings_file)
            except (ValueError, OSError):
                self.warning = '上次路径记录无法读取，请重新选择目录'
        # Local settings may have travelled with the repository from another OS.
        foreign_paths = []
        for key in ('source', 'segmentation', 'results', 'export_parent'):
            value = self.settings.get(key)
            if isinstance(value, str) and not Path(value).is_absolute() and (
                    PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
                foreign_paths.append(key)
                self.settings.pop(key)
        if foreign_paths:
            self.warning = '检测到其他操作系统保存的路径，请在本机重新选择输入与导出目录'
        seg = initial_segmentation or self.settings.get('segmentation')
        if seg:
            try:
                source = self.settings.get('source') if not initial_segmentation else None
                source = source or read_json(Path(seg)/'dataset.json').get('source')
                self.load({'source': str(source), 'segmentation': str(seg)}, remember=False)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.input_warning = f'未恢复输入目录：{exc}'
                self.warning = self.input_warning
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
            record = cloud_review_record(result, row['relative_dir'], decision)
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
                           cloud_decision=result.get('cloud_review', {}).get('decision', 'pending'),
                           has_alignment=result.get('has_alignment', False),
                           alignment_cached=result.get('alignment_cached', False),
                           alignment_revision=result.get('alignment_revision', ''),
                           alignment_output=str(result['folder']) if result.get('has_alignment') else '',
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
                    'export_parent': self.settings.get('export_parent', str(Path(__file__).resolve().parents[1]/'outputs')),
                    'suggested_name': '点云成果_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19],
                    'job': copy.deepcopy(self.job), 'warning': self.warning, 'input_warning': self.input_warning,
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
            self.dataset_input_mode = declared_mode
            self.dataset_id = secrets.token_hex(12)
            # A validated reload starts a fresh workspace, even when paths are unchanged.
            # Keep failures visible until this succeeds; active jobs remain protected by idle().
            self.job = None
            self.stop_event.clear()
            if changed or not self.results_match_inputs:
                self.results, self.results_root, self.last_output = {}, None, None
            self.results_match_inputs = True
            self.warning = ''
            self.input_warning = ''
            if remember:
                self.settings.update(source=str(source), segmentation=str(seg))
                if not self.results_root: self.settings.pop('results', None)
                self.remember()
            return self.state()

    def open_results(self, value, remember=True):
        root = directory(value)
        results = {}
        storage_map = None
        manifest_file = root/'pointcloud_dataset.json'
        if manifest_file.is_file():
            if manifest_file.is_symlink():
                raise ValueError('点云清单不能使用符号链接')
            manifest = read_json(manifest_file)
            if isinstance(manifest, dict) and manifest.get('group_by_decision') is True:
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
            results[rel] = {'folder': folder, 'report': report, 'has_alignment': alignment_status(folder)}
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
                                    'alignment_revision': sha256(aligned/'alignment.json')}
                except (ValueError, OSError, KeyError, TypeError):
                    invalid += 1
            review_invalid = 0
            for rel, result in results.items():
                record = self.settings.get('cloud_reviews', {}).get(str(root), {}).get(rel)
                try:
                    review_file = result['folder']/'cloud_review.json'
                    if record is None and review_file.is_file() and not review_file.is_symlink():
                        record = read_json(review_file)
                    if record is not None:
                        if not valid_cloud_review(record, result['folder'], rel, result['report']):
                            raise ValueError('筛选记录与点云不匹配')
                        result['cloud_review'] = record
                except (ValueError, OSError, KeyError, TypeError):
                    review_invalid += 1
            self.results, self.results_root = results, root
            # Equal relative paths can occur in different cameras and segmentation
            # jobs. Opening results must not authorize generation from other inputs.
            self.results_match_inputs = bool(self.source and self.segmentation)
            for rel, result in results.items():
                meta_path = within(self.segmentation, rel)/'segmentation.json' if self.segmentation else None
                if not meta_path or not meta_path.is_file() or sha256(meta_path) != result['report'].get('segmentation_json_sha256'):
                    self.results_match_inputs = False
                    break
            # Replace diagnostics for the previously opened results, while keeping
            # unrelated configuration warnings and input diagnostics separate.
            if self.results_warning and self.warning.endswith(self.results_warning):
                self.warning = self.warning[:-len(self.results_warning)].strip()
            # Viewing saved results does not require restored input directories.
            # Input diagnostics remain available in directory settings.
            if self.warning.startswith(('未恢复点云目录：', '未恢复输入目录：')):
                self.warning = ''
            self.last_output = root
            self.job = saved_job(root)
            self.dataset_id = secrets.token_hex(12)
            self.results_warning = f'{invalid} 次已保存对齐成果缺失或与原点云不匹配，已退回相机点云，请重新对齐。' if invalid else ''
            if review_invalid:
                self.results_warning += f' {review_invalid} 次点云筛选记录与当前文件不匹配，已恢复为待定。'
            self.warning = ' '.join(filter(None, [self.warning, self.results_warning.strip()]))
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

    def _run(self, rows, snapshot, target, mode, allow_draft, review_scope):
        stage, completed = None, []
        try:
            stage = Path(tempfile.mkdtemp(prefix='.pointcloud-export-', dir=target.parent))
            if self.job['group_by_decision']:
                for decision in self.job['decisions']:
                    (stage/REVIEW_GROUPS[decision]).mkdir()
            for row in rows:
                if self.stop_event.is_set(): break
                rel = row['relative_dir']
                storage = (Path(REVIEW_GROUPS[row['cloud_decision']])/rel).as_posix() if self.job['group_by_decision'] else rel
                with self.lock: self.job['current'] = rel
                try:
                    if mode == 'generate':
                        if row.get('excluded'):
                            raise SkipCapture('工作台手动标记为跳过')
                        if row.get('validation_errors'):
                            raise SkipCapture('核对失败：'+'；'.join(row['validation_errors']))
                        # Re-read review state in reconstruct; old list never authorizes a changed mask.
                        meta = read_json(within(self.segmentation, rel)/'segmentation.json')
                        try:
                            capture_input_mode(meta, self.dataset_input_mode)
                        except ValueError as exc:
                            with self.lock:
                                row['validation_errors'] = [str(exc)]
                                for loaded_row in self.rows:
                                    if loaded_row['id'] == row['id']:
                                        loaded_row['validation_errors'] = list(row['validation_errors'])
                            raise SkipCapture(str(exc)) from exc
                        if meta.get('decision') == 'reject':
                            raise SkipCapture('分割阶段标记为淘汰')
                        if not allow_draft and not (meta.get('reviewed') is True and meta.get('status') == 'reviewed'):
                            raise SkipCapture('分割尚未确认')
                        if row['missing']: raise ValueError('缺少文件：'+', '.join(row['missing']))
                        data, report, mask = reconstruct(within(self.source, rel), within(self.segmentation, rel), allow_draft)
                        export_frame(within(stage, rel), data, report, mask)
                        write_viewer(within(stage, rel)/'preview.html', data, report)
                    else:
                        source = snapshot[rel]
                        dest = within(stage, storage)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        safe_copy_tree(source['folder'], dest)
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
                        if normalized != report and not source.get('has_alignment'):
                            write_json(dest/'pointcloud.json', normalized)
                        # Folder exports use the current layout/theme even when
                        # the measured point data and metadata need no upgrade.
                        with np.load(dest/'points.npz', allow_pickle=False) as data:
                            write_viewer(dest/'preview.html', data, normalized)
                        if source.get('has_alignment'):
                            with np.load(dest/'body_points.npz', allow_pickle=False) as data:
                                (dest/'body_preview.html').write_text(render_body_view(data, read_json(dest/'alignment.json')), encoding='utf-8')
                        write_cloud_review(dest, source)
                        if self.job.get('aligned_only') and not alignment_status(dest):
                            raise ValueError('复制后的对齐成果不完整或校验不匹配，请重新核对后导出')
                        report = normalized
                    completed.append(({**row, 'storage_relative_dir': storage}, report))
                    with self.lock: self.job['success'] += 1
                except SkipCapture as exc:
                    with self.lock: self.job['skipped'].append({'relative_dir': rel, 'reason': str(exc)})
                except Exception as exc:
                    # Remove partially written frame files; never publish an incomplete capture folder.
                    bad = within(stage, storage)
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
            manifest = {'schema_version': 1, 'source': str(self.source or ''), 'segmentation': str(self.segmentation or ''),
                        'output': str(target), 'coordinate_system': 'native_depth_camera', 'unit': 'mm',
                        'export_policy': EXPORT_POLICY,
                        'frames': [{**row, 'has_cloud': True, 'input_mode': input_mode(report),
                                    'primary_ply': primary_ply(report), 'camera': report.get('camera', {}),
                                    'reviewed': report['reviewed'], 'points': report['stats']['points'],
                                    'has_alignment': alignment_status(within(stage, row['storage_relative_dir'])),
                                    'cloud_decision': row.get('cloud_decision', 'pending') if mode == 'export' else 'pending'} for row, report in completed]}
            manifest['aligned_only'] = self.job['aligned_only']
            manifest['group_by_decision'] = self.job['group_by_decision']
            manifest['decisions'] = self.job['decisions']
            write_json(stage/'pointcloud_dataset.json', manifest)
            if mode == 'export':
                exported = {row['relative_dir']: row['storage_relative_dir'] for row, _ in completed}
                screening = []
                for row in review_scope:
                    decision = row.get('cloud_decision', 'pending')
                    reason = '' if row['relative_dir'] in exported else (
                        '未勾选此状态' if self.job['decisions'] is not None and decision not in self.job['decisions']
                        else '尚未对齐' if self.job['aligned_only'] and not row.get('has_alignment')
                        else '已淘汰' if decision == 'reject' and self.job['decisions'] is None
                        else '不在本次点云导出范围或导出失败')
                    screening.append({'relative_dir': row['relative_dir'], 'pig_id': row['pig_id'],
                        'capture_id': row['capture_id'], 'input_mode': row.get('cloud_input_mode') or row.get('input_mode'),
                        'decision': decision, 'status': {'keep':'保留','pending':'待定','reject':'淘汰'}[decision],
                        'has_alignment': row.get('has_alignment', False), 'exported': row['relative_dir'] in exported,
                        'storage_relative_dir': exported.get(row['relative_dir'], ''),
                        'reason': reason})
                write_json(stage/'cloud_screening.json', {'schema_version': 1, 'results': str(self.results_root),
                    'frames': screening})
                with (stage/'cloud_screening.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(screening[0]))
                    writer.writeheader()
                    writer.writerows(screening)
            if mode == 'generate':
                write_json(stage/'input_validation.json', self.validation_report())
            final_job = {**copy.deepcopy(self.job), 'status': status, 'current': '已按原相对目录保存全部成果'}
            write_json(stage/'export.json', final_job)
            links = ''.join(f'<li><a href="{quote(row["storage_relative_dir"], safe="/")}/{"body_preview.html" if row["has_alignment"] else "preview.html"}">{escape(row["storage_relative_dir"])}</a> · {row["points"]:,} 点</li>' for row in manifest['frames'])
            (stage/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>点云成果</title><style>body{font:16px system-ui;margin:40px;line-height:2}a{color:#167b6b}</style><h1>毫米点云成果</h1><p>每次采集的 PLY、NPZ、检查图和生成记录均保存在对应文件夹。</p><ul>'+links+'</ul>', encoding='utf-8')
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
