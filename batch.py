"""Generate per-capture metric surface clouds from segmentation datasets."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pointcloud_workbench.core import (independent_paths, within, read_json, write_json,
                                       reconstruct, export_frame, sha256, VERSION, cv2,
                                       required_result_files, EXPORT_POLICY, input_mode)
from pointcloud_workbench.viewer import write_viewer


def positive(value):
    v = int(value)
    if v <= 0:
        raise argparse.ArgumentTypeError('必须是正整数')
    return v


def run(args):
    seg = Path(args.segmentation).resolve()
    ds = read_json(seg/'dataset.json')
    source, seg, output = independent_paths(ds['source'], seg, args.output)
    if not source.is_dir():
        raise ValueError('dataset.json 中的源数据目录不存在')
    rows = ds['frames']
    if args.pig:
        rows = [r for r in rows if str(r['pig_id']) in args.pig]
    if args.capture:
        rows = [r for r in rows if r['capture_id'] in args.capture]
    if not rows:
        raise ValueError('未找到匹配的采集')
    if args.limit:
        rows = rows[:args.limit]
    # Check the actual source directory independently of arbitrary dataset paths.
    for row in rows:
        if within(source, row['relative_dir']) != Path(row['source_dir']).resolve():
            raise ValueError('采集源路径与 dataset.json 不一致')
        within(seg, row['relative_dir'])
        within(output, row['relative_dir'])
    output.mkdir(parents=True, exist_ok=True)
    summary = {'version': 1, 'source': str(source), 'segmentation': str(seg),
               'output': str(output), 'mode': 'draft_preview' if args.allow_draft else 'reviewed_only',
               'unit': 'mm', 'coordinate_system': 'native_depth_camera',
               'generated': [], 'existing': [], 'skipped': [], 'errors': []}
    completed = []
    for row in rows:
        relative = row['relative_dir']
        s, m, dest = within(source, relative), within(seg, relative), within(output, relative)
        try:
            meta = read_json(m/'segmentation.json')
            if meta.get('decision') == 'reject':
                summary['skipped'].append({'relative_dir': relative, 'reason': 'segmentation_rejected'})
                continue
            if not args.allow_draft and not (meta.get('reviewed') is True and meta.get('status') == 'reviewed'):
                summary['skipped'].append({'relative_dir': relative, 'reason': 'segmentation_not_reviewed'})
                continue
            if dest.exists():
                # Do not silently reuse stale clouds after manual edits or changed inputs.
                report = read_json(dest/'pointcloud.json')
                mask_path = m/report['mask_file']
                fresh = report['segmentation_json_sha256'] == sha256(m/'segmentation.json')
                fresh &= report['version'] == VERSION
                if input_mode(report) == 'rgb':
                    from pointcloud_workbench.registration import AUDIT_VERSION
                    fresh &= report.get('rgb_registration', {}).get('version') == AUDIT_VERSION
                fresh &= report.get('export_policy') == EXPORT_POLICY
                fresh &= report['mask_sha256'] == sha256(mask_path)
                fresh &= all(sha256(s/name) == digest for name, digest in report['source_sha256'].items())
                fresh &= report['rgb']['max_absolute_delta_us'] == args.max_rgb_delta_us
                fresh &= report['rgb'].get('visibility_tolerance_mm', args.visibility_mm) == args.visibility_mm
                fresh &= all((dest/n).is_file() for n in required_result_files(report))
                if not fresh:
                    raise ValueError('已有点云与当前输入或参数不一致，请使用新的输出目录')
                summary['existing'].append(relative)
                completed.append((relative, report))
                continue
            payload, report, mask = reconstruct(s, m, args.allow_draft, args.max_rgb_delta_us, args.visibility_mm)
            # Stage complete files before publishing a per-frame directory.
            stage_root = Path(tempfile.mkdtemp(prefix='.pointcloud-', dir=output))
            try:
                staged = export_frame(stage_root/'frame', payload, report, mask)
                write_viewer(staged/'preview.html', payload, report)
                dest.parent.mkdir(parents=True, exist_ok=True)
                staged.rename(dest)
            finally:
                shutil.rmtree(stage_root)
            summary['generated'].append({'relative_dir': relative, **report['stats']})
            completed.append((relative, report))
            print(f'{relative}: {len(payload["xyz_mm"])} points; RGB {report["stats"]["rgb_valid_ratio"]:.1%}; reviewed={report["reviewed"]}', flush=True)
        except (ValueError, KeyError, TypeError, OSError, cv2.error) as exc:
            summary['errors'].append({'relative_dir': relative, 'error': str(exc)})
            print(f'{relative}: ERROR {exc}', file=sys.stderr, flush=True)
    write_json(output/'summary.json', summary)
    from html import escape
    # The index includes prior successful captures when the next run selects more pigs.
    completed = []
    for row in ds['frames']:
        relative = row['relative_dir']
        report_path = within(output, relative)/'pointcloud.json'
        if report_path.is_file():
            completed.append((relative, read_json(report_path)))
    links = ''.join(f'<li><a href="{escape(rel, quote=True)}/preview.html">{escape(rel)}</a> — '
                    f'{r["stats"]["points"]:,} 点 · {"已确认分割" if r["reviewed"] else "未审核初稿预览"}</li>' for rel, r in completed)
    (output/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>毫米点云结果</title>'
        '<style>body{max-width:1000px;margin:50px auto;font:17px system-ui;background:#101722;color:#dde7ee}a{color:#70dbc0}li{margin:18px 0}</style>'
        '<h1>相机坐标 · 毫米点云</h1><p>每次采集独立生成；原始数据与分割只读。单视角可见表面。</p>'
        f'<p>本次生成 {len(summary["generated"])}，复用 {len(summary["existing"])}，未确认跳过 {len(summary["skipped"])}，错误 {len(summary["errors"])}</p>'
        f'<ul>{links}</ul>', encoding='utf-8')
    print(f'完成：生成 {len(summary["generated"])}，已有 {len(summary["existing"])}，跳过 {len(summary["skipped"])}，错误 {len(summary["errors"])}')
    return 1 if summary['errors'] else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--segmentation', type=Path, default=Path(__file__).resolve().parents[1]/'outputs/itof_SAM2猪体分割')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--allow-draft', action='store_true', help='显式允许未审核初稿，仅供预览')
    p.add_argument('--pig', action='append', help='耳标，可多次指定')
    p.add_argument('--capture', action='append', help='采集时间目录名，可多次指定')
    p.add_argument('--limit', type=positive)
    p.add_argument('--max-rgb-delta-us', type=positive, default=10000)
    p.add_argument('--visibility-mm', type=positive, default=20)
    args = p.parse_args()
    try:
        return run(args)
    except (ValueError, KeyError, OSError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
