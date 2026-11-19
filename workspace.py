"""Browser workflow: independent inputs, background jobs and folder-tree exports."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
from html import escape
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
from urllib.parse import quote

from .core import independent_paths, within, read_json, write_json, sha256, reconstruct, export_frame, source_files
from .viewer import write_viewer

RESULT_FILES = {'points.npz', 'cloud_rgb_mm.ply', 'cloud_ir_mm.ply', 'pointcloud.json',
                'preview.html', 'mask_depth_check.png', 'rgb_projection_check.png'}


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
        self.source = self.segmentation = self.results_root = None
        self.last_output = None
        self.warning = ''
        self.settings = {}
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

    def state(self):
        from .native_picker import supported
        with self.lock:
            merged = {r['relative_dir']: dict(r) for r in self.rows}
            for rel, result in self.results.items():
                r = result['report']
                row = merged.setdefault(rel, {'id': hashlib.sha256(rel.encode()).hexdigest()[:20],
                    'relative_dir': rel, 'pig_id': str(r.get('pig_id', '未知')), 'capture_id': r.get('capture_id', Path(rel).name),
                    'reviewed': r.get('reviewed', False), 'status': r.get('segmentation_status'), 'missing': []})
                row.update(has_cloud=True, points=r['stats']['points'], cloud_reviewed=r.get('reviewed', False))
            frames = list(merged.values())
            for row in frames: row.setdefault('has_cloud', False)
            return {'dataset_id': self.dataset_id, 'source': str(self.source or ''),
                    'segmentation': str(self.segmentation or ''), 'results': str(self.results_root or ''),
                    'last_output': str(self.last_output or ''),
                    'frames': frames, 'pigs': len({r['pig_id'] for r in frames}),
                    'reviewed': sum(r.get('reviewed') is True for r in frames),
                    'clouds': len(self.results), 'native_picker': supported(),
                    'export_parent': self.settings.get('export_parent', str(Path(__file__).resolve().parents[1]/'outputs')),
                    'suggested_name': '点云成果_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19],
                    'job': copy.deepcopy(self.job), 'warning': self.warning}

    def load(self, payload, remember=True):
        source, seg = directory(payload.get('source')), directory(payload.get('segmentation'))
        independent_paths(source, seg, self.state_dir)
        if not (seg/'dataset.json').is_file():
            raise ValueError('分割结果目录缺少 dataset.json，请选择包含该文件的成果根目录')
        ds = read_json(seg/'dataset.json')
        rows, ids, relatives = [], set(), set()
        if not isinstance(ds.get('frames'), list) or not ds['frames']:
            raise ValueError('分割结果清单中没有采集记录')
        for entry in ds['frames']:
            rel = entry['relative_dir']
            s, m = within(source, rel), within(seg, rel)
            if rel in relatives: raise ValueError('分割清单有重复相对路径')
            relatives.add(rel)
            frame_id = hashlib.sha256(rel.encode()).hexdigest()[:20]
            if frame_id in ids: raise ValueError('采集编号重复')
            ids.add(frame_id)
            meta = read_json(m/'segmentation.json') if (m/'segmentation.json').is_file() else {}
            missing = [n for n in source_files(meta) if not (s/n).is_file()]
            if not meta: missing.append('segmentation.json')
            mask = 'trunk_mask.png' if meta.get('reviewed') is True and meta.get('status') == 'reviewed' else (
                'trunk_mask_working.png' if meta.get('status') == 'corrected' else 'trunk_mask_draft.png')
            if not (m/mask).is_file(): missing.append(mask)
            rows.append({'id': frame_id, 'relative_dir': rel, 'pig_id': str(entry['pig_id']),
                         'capture_id': entry['capture_id'], 'reviewed': meta.get('reviewed') is True,
                         'status': meta.get('status', 'missing'), 'decision': meta.get('decision', 'pending'),
                         'missing': sorted(missing)})
        if all(r['missing'] for r in rows):
            example = rows[0]
            raise ValueError(f'没有匹配的完整采集，请核对原始目录层级：{example["relative_dir"]} 缺少 '+', '.join(example['missing']))
        with self.lock:
            self.idle()
            changed = self.source != source or self.segmentation != seg
            self.source, self.segmentation, self.rows = source, seg, rows
            self.dataset_id = secrets.token_hex(12)
            # A validated reload starts a fresh workspace, even when paths are unchanged.
            # Keep failures visible until this succeeds; active jobs remain protected by idle().
            self.job = None
            self.stop_event.clear()
            if changed:
                self.results, self.results_root, self.last_output = {}, None, None
            self.warning = ''
            if remember:
                self.settings.update(source=str(source), segmentation=str(seg))
                if changed: self.settings.pop('results', None)
                self.remember()
            return self.state()

    def open_results(self, value, remember=True):
        root = directory(value)
        results = {}
        for p in sorted(root.rglob('pointcloud.json')):
            rel = p.parent.relative_to(root).as_posix()
            folder = within(root, rel)
            if p.is_symlink() or any((root/x).is_symlink() for x in p.relative_to(root).parents if str(x) != '.'):
                raise ValueError('点云目录不能包含符号链接')
            if not all((folder/n).is_file() and not (folder/n).is_symlink() for n in RESULT_FILES):
                continue
            report = read_json(p)
            if report.get('coordinate_system') != 'native_depth_camera' or report.get('unit') != 'mm':
                raise ValueError('该目录的点云不是当前毫米相机坐标格式')
            results[rel] = {'folder': folder, 'report': report}
        if not results: raise ValueError('目录中没有完整点云成果，请选择包含 pig_编号/采集时间 的点云根目录')
        with self.lock:
            self.idle()
            self.results, self.results_root = results, root
            self.last_output = root
            self.job = None
            self.dataset_id = secrets.token_hex(12)
            if remember:
                self.settings['results'] = str(root)
                self.remember()
            return self.state()

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
            parent = directory(payload.get('parent'))
            target = parent/folder_name(payload.get('name'))
            protected = [p for p in [self.source, self.segmentation, self.state_dir, self.results_root] if p]
            # Validate target against each root without requiring those roots to be independent of each other.
            for p in protected: independent_paths(p, target)
            if target.exists() or target.is_symlink():
                raise ValueError('成果文件夹已存在，请换一个名称，避免覆盖已有文件')
            allow_draft = payload.get('allow_draft') is True
            if mode == 'generate':
                usable = [r for r in rows if not r['missing'] and r.get('decision') != 'reject' and (allow_draft or r['reviewed'])]
                if not usable: raise ValueError('所选采集中没有可生成项；请确认分割，或勾选“允许初稿预览”，并检查缺失文件')
            elif mode == 'export':
                if any(r['relative_dir'] not in self.results for r in rows):
                    raise ValueError('导出范围含未生成采集，请改选“已生成”范围或先生成点云')
            else: raise ValueError('任务类型无效')
            snapshot = {r['relative_dir']: self.results.get(r['relative_dir']) for r in rows}
            self.stop_event.clear()
            self.job = {'id': secrets.token_hex(10), 'mode': mode, 'status': 'running', 'done': 0,
                        'total': len(rows), 'success': 0, 'skipped': [], 'errors': [], 'output': str(target),
                        'current': '准备生成' if mode == 'generate' else '准备按文件夹导出'}
            self.settings.update(export_parent=str(parent))
            self.remember()
            self.worker = threading.Thread(target=self._run, args=(rows, snapshot, target, mode, allow_draft), daemon=False)
            self.worker.start()
            return self.state()

    def stop(self):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                self.stop_event.set()
                self.job.update(status='stopping', current='正在完成当前采集，随后停止')
            return self.state()

    def _run(self, rows, snapshot, target, mode, allow_draft):
        stage, completed = None, []
        try:
            stage = Path(tempfile.mkdtemp(prefix='.pointcloud-export-', dir=target.parent))
            for row in rows:
                if self.stop_event.is_set(): break
                rel = row['relative_dir']
                with self.lock: self.job['current'] = rel
                try:
                    if mode == 'generate':
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
                        source = snapshot[rel]
                        dest = within(stage, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        safe_copy_tree(source['folder'], dest)
                        report = read_json(dest/'pointcloud.json')
                    completed.append((row, report))
                    with self.lock: self.job['success'] += 1
                except SkipCapture as exc:
                    with self.lock: self.job['skipped'].append({'relative_dir': rel, 'reason': str(exc)})
                except Exception as exc:
                    # Remove partially written frame files; never publish an incomplete capture folder.
                    bad = within(stage, rel)
                    if bad.exists(): shutil.rmtree(bad)
                    with self.lock: self.job['errors'].append({'relative_dir': rel, 'error': str(exc)})
                finally:
                    with self.lock: self.job['done'] += 1
            if not completed:
                reason = (self.job['errors'] or self.job['skipped'] or [{}])[0]
                detail = reason.get('error') or reason.get('reason') or '没有完成任何采集'
                raise ValueError('没有生成或导出成功的点云；首项原因：'+detail+'。目标文件夹未创建，详见逐项记录')
            status = 'stopped' if self.stop_event.is_set() else ('completed_with_errors' if self.job['errors'] else 'completed')
            manifest = {'schema_version': 1, 'source': str(self.source or ''), 'segmentation': str(self.segmentation or ''),
                        'output': str(target), 'coordinate_system': 'native_depth_camera', 'unit': 'mm',
                        'frames': [{**row, 'reviewed': report['reviewed'], 'points': report['stats']['points']} for row, report in completed]}
            write_json(stage/'pointcloud_dataset.json', manifest)
            final_job = {**copy.deepcopy(self.job), 'status': status, 'current': '已按原相对目录保存全部成果'}
            write_json(stage/'export.json', final_job)
            links = ''.join(f'<li><a href="{quote(row["relative_dir"], safe="/")}/preview.html">{escape(row["relative_dir"])}</a> · {report["stats"]["points"]:,} 点</li>' for row, report in completed)
            (stage/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>点云成果</title><style>body{font:16px system-ui;margin:40px;line-height:2}a{color:#167b6b}</style><h1>毫米点云成果</h1><p>每次采集的 PLY、NPZ、检查图和生成记录均保存在对应文件夹。</p><ul>'+links+'</ul>', encoding='utf-8')
            if target.exists() or target.is_symlink(): raise ValueError('目标文件夹已被创建，请换名重试')
            stage.rename(target)
            stage = None
            with self.lock:
                self.last_output = target
                if mode == 'generate':
                    self.results_root = target
                    self.results = {row['relative_dir']: {'folder': within(target, row['relative_dir']), 'report': report} for row, report in completed}
                    self.settings['results'] = str(target)
                self.job.update(final_job)
                try: self.remember()
                except OSError: self.job['warning'] = '成果已保存，但未能记住本次路径'
        except Exception as exc:
            with self.lock: self.job.update(status='failed', error=str(exc), current='任务未完成')
        finally:
            if stage: shutil.rmtree(stage, ignore_errors=True)


class SkipCapture(Exception):
    pass
