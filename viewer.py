"""Self-contained offline Canvas viewer; exported NPZ/PLY remain full density."""
import json
from pathlib import Path

import numpy as np
from .core import display_ir, export_metadata


def view_bounds(xyz):
    """Fit the body without letting a few boundary points dominate the camera.

    Only the view changes: retain the original points and full measured bounds.
    Use the full cloud, rather than the display sample, for reproducible framing.
    """
    low, high = np.min(xyz, axis=0), np.max(xyz, axis=0)
    qlow, qhigh = np.percentile(xyz, [.5, 99.5], axis=0)
    padding = np.maximum((qhigh-qlow)*.1, 1.)
    body_low, body_high = np.maximum(low, qlow-padding), np.minimum(high, qhigh+padding)
    outside = np.any((xyz < body_low) | (xyz > body_high), axis=1)
    return {'full': {'min': low.tolist(), 'max': high.tolist()},
            'body': {'min': body_low.tolist(), 'max': body_high.tolist()},
            'outside_body_points': int(outside.sum())}


def render_viewer(payload, report, embedded=False):
    report = export_metadata(report)
    # Uniform reproducible display sampling; never modifies exported point sets.
    n = len(payload['xyz_mm'])
    # Seeded uniform sampling avoids stripes caused by a fixed raster stride.
    idx = np.sort(np.random.default_rng(7).choice(n, min(24000, n), replace=False))
    rgb = payload['rgb'].copy()
    rgb[~payload['rgb_valid']] = [110, 120, 140]
    data = {'xyz': np.round(payload['xyz_mm'][idx], 3).tolist(),
            'rgb': rgb[idx].tolist(), 'ir': display_ir(payload['infrared_raw'])[idx].tolist(),
            'valid': payload['rgb_valid'][idx].astype(int).tolist(), 'report': report,
            'view_bounds': view_bounds(payload['xyz_mm'])}
    template = Path(__file__).with_name('viewer.html').read_text(encoding='utf-8')
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    registration_image = report.get('rgb_registration', {}).get('check_image') == 'rgb_registration_check.png'
    return (template.replace('__POINT_DATA__', encoded)
            .replace('__BODY_CLASS__', 'embedded' if embedded else '')
            .replace('__PRIMARY_PLY__', report['primary_ply'])
            .replace('__RGB_CHECK_IMAGE__', 'rgb_registration_check.png' if registration_image else 'rgb_projection_check.png')
            .replace('__RGB_CHECK_LABEL__', 'RGB 配准核对' if registration_image else 'RGB 投影')
            .replace('__PLY_LABEL__', 'IR PLY' if report['input_mode'] == 'infrared' else 'RGB PLY'))


def write_viewer(path, payload, report):
    Path(path).write_text(render_viewer(payload, report), encoding='utf-8')
