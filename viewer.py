"""Self-contained offline Canvas viewer; exported NPZ/PLY remain full density."""
import json
from pathlib import Path

import numpy as np
from .core import display_ir


def render_viewer(payload, report, embedded=False):
    # Uniform reproducible display sampling; never modifies exported point sets.
    n = len(payload['xyz_mm'])
    # Seeded uniform sampling avoids stripes caused by a fixed raster stride.
    idx = np.sort(np.random.default_rng(7).choice(n, min(24000, n), replace=False))
    rgb = payload['rgb'].copy()
    rgb[~payload['rgb_valid']] = [110, 120, 140]
    data = {'xyz': np.round(payload['xyz_mm'][idx], 3).tolist(),
            'rgb': rgb[idx].tolist(), 'ir': display_ir(payload['infrared_raw'])[idx].tolist(),
            'valid': payload['rgb_valid'][idx].astype(int).tolist(), 'report': report}
    template = Path(__file__).with_name('viewer.html').read_text(encoding='utf-8')
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    return template.replace('__POINT_DATA__', encoded).replace('__BODY_CLASS__', 'embedded' if embedded else '')


def write_viewer(path, payload, report):
    Path(path).write_text(render_viewer(payload, report), encoding='utf-8')
