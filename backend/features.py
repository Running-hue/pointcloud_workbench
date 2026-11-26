"""Visible-surface geometry and fixed-length local-normal profiles, in mm.

No completion, anatomical inference, posture restoration or weight model. All
statistics use the retained full-density cloud; smoothing affects the reference
curve only. Numeric availability and human boundary review are separate masks.
"""
from __future__ import annotations

import csv
import io
import json
import numpy as np

VERSION = 'visible-surface-features-v2'
CENTERLINE_VERSION = 'endpoint-constrained-smoothing-spline-v1'
BLENDED_CENTERLINE_VERSION = 'endpoint-constrained-mid-envelope-v1'
LEGACY_CENTERLINE_VERSION = 'local-smoothed-mid-envelope-v1'
CENTERLINE_SMOOTHING = .001
CENTERLINE_KNOTS = (.25, .5, .75)
FEATURE_FILES = {'geometry_features.json', 'profile_sequence.csv', 'profile_sequence.npz'}
DEFAULTS = {'count': 32, 'thickness_mm': 20., 'min_points': 20, 'grid_mm': 10.}
QUALITY = {'endpoints': 'unknown', 'ground': 'unknown'}
CHANNELS = {
    'width_q02_q98_mm': 'mm', 'height_p95_mm': 'mm',
    'relative_height_p95_mm': 'mm', 'height_gradient': 'mm/mm',
    'transverse_curvature_per_mm': '1/mm', 'bulge_mm': 'mm',
    'point_count': 'count', 'transverse_bin_coverage': 'ratio',
    'fit_rmse_mm': 'mm',
    'visible_transverse_arc_mm': 'mm',
}
ARC_BIN_MM = 5.
ARC_MIN_BIN_POINTS = 2
GEOMETRY_FIELDS = ('endpoint_ground_chord_mm', 'visible_centerline_arc_mm', 'centerline_arc_chord_ratio',
    'profile_width_median_mm', 'profile_width_max_mm', 'profile_width_p90_mm',
    'width_25pct_mm', 'width_50pct_mm', 'width_75pct_mm', 'visible_height_p95_mm',
    'relative_height_p95_p05_mm', 'observed_grid_area_mm2', 'observed_positive_height_integral_mm3')


def settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('几何特征参数无效')
    out = {**DEFAULTS, **value}
    for key, lo, hi in [('count', 16, 64), ('thickness_mm', 5, 100),
                        ('min_points', 3, 500), ('grid_mm', 2, 50)]:
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f'几何特征参数 {key} 应在 {lo}–{hi} 之间')
    if out['count'] not in (16, 32, 64) or out['min_points'] != int(out['min_points']):
        raise ValueError('剖面数量限 16/32/64，最少点数须为整数')
    out['count'], out['min_points'] = int(out['count']), int(out['min_points'])
    return out


def quality(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value)-set(QUALITY):
        raise ValueError('特征复核条件无效')
    out = {**QUALITY, **value}
    if (not isinstance(out['endpoints'], str) or not isinstance(out['ground'], str)
            or out['endpoints'] not in {'unknown', 'complete', 'truncated'} or out['ground'] not in {'unknown', 'reliable'}):
        raise ValueError('端点或地面复核条件无效')
    return out


def centerline_version(measurement):
    """Keep existing reviewed planes until an explicit recalculation/re-alignment."""
    version = measurement.get('centerline_version') or (
        LEGACY_CENTERLINE_VERSION if measurement.get('feature_reviews') else CENTERLINE_VERSION)
    if version not in {CENTERLINE_VERSION, BLENDED_CENTERLINE_VERSION, LEGACY_CENTERLINE_VERSION}:
        raise ValueError('中心线算法版本不受支持，请重新计算几何特征')
    if not any(v in {'usable', 'unusable'} for v in measurement.get('feature_reviews', {}).values()):
        return CENTERLINE_VERSION
    return version


def _constrained_spline_basis(t, second_derivative=False):
    """C2 cubic regression spline with both endpoint values fixed to zero.

    Eliminate the constant and linear coefficients analytically. Each remaining
    basis function vanishes at t=0 and t=1, so the fit has exact endpoints without
    editing a fitted curve or adding separate endpoint connector segments.
    """
    t = np.asarray(t)[:, None]
    knots = np.asarray(CENTERLINE_KNOTS)
    if second_derivative:
        return np.column_stack([np.full(len(t), 2.), 6*t[:, 0], 6*np.maximum(t-knots, 0)])
    return np.column_stack([t[:, 0]**2-t[:, 0], t[:, 0]**3-t[:, 0],
                            np.maximum(t-knots, 0)**3-t*(1-knots)**3])


def _fit_constrained_centerline(centers, y, length, x):
    """One fit over the entire marked interval, with a curvature penalty.

    Seed bands have equal weight, independent of point density. Normalize X to
    [0,1] and use mean-square terms so resampling/count/physical scale do not
    change the meaning of the fixed smoothness parameter.
    """
    good = np.isfinite(y)
    basis = _constrained_spline_basis(centers[good]/length)
    evaluate = _constrained_spline_basis(x/length)
    curvature = _constrained_spline_basis(np.linspace(0, 1, 257), second_derivative=True)
    design = np.vstack([basis/np.sqrt(good.sum()),
                        np.sqrt(CENTERLINE_SMOOTHING/len(curvature))*curvature])
    target = np.r_[y[good]/np.sqrt(good.sum()), np.zeros(len(curvature))]
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    return evaluate @ coefficients


def _centerline(p, length, minimum, version=CENTERLINE_VERSION):
    """Smoothed mid-envelope y(x); monotone-X, operational visible centerline.

    Seed bins have equal physical X width independent of requested K. Fit all
    supported seeds in one endpoint-constrained smoothing spline. Historical
    models are retained only for reproducing previously reviewed profiles.
    Unsupported seed bins remain invalid with every method.
    """
    n = int(np.clip(np.ceil(length/30), 16, 64))
    edges = np.linspace(0, length, n+1)
    centers = (edges[1:]+edges[:-1])/2
    y = np.full(n, np.nan)
    for i in range(n):
        q = p[(p[:, 0] >= edges[i]) & (p[:, 0] <= edges[i+1] if i == n-1 else p[:, 0] < edges[i+1])]
        if len(q) >= minimum:
            low, high = np.percentile(q[:, 1], [2, 98])
            y[i] = (low+high)/2
    good = np.isfinite(y)
    x = np.linspace(0, length, 257)
    if good.sum() < 4:
        return x, np.zeros_like(x), np.zeros(len(x), bool), edges, good
    # A fit does not create support in missing bands. Both adjacent seed bands
    # must be observed, even when a candidate spline can be evaluated there.
    left = np.clip(np.searchsorted(centers, x)-1, 0, n-1)
    right = np.clip(np.searchsorted(centers, x), 0, n-1)
    supported = good[left] & good[right]
    if version == CENTERLINE_VERSION:
        return x, _fit_constrained_centerline(centers, y, length, x), supported, edges, good

    # Reproduce historical curves exactly for their existing reviewed planes.
    smooth = y.copy()
    for i in np.flatnonzero(good):
        ids = np.arange(max(0, i-2), min(n, i+3))
        # Do not smooth across an empty seed bin.
        ids = np.array([j for j in ids if good[min(i, j):max(i, j)+1].all()])
        if len(ids) > 1:
            smooth[i] = np.polyval(np.polyfit(centers[ids]-centers[i], y[ids], 1), 0)
    curve = np.interp(x, centers[good], smooth[good])
    # Endpoint slope rather than flat extension, limited to the half-bin margin.
    ids = np.flatnonzero(good)
    for a, b, take in [(ids[0], ids[1], x < centers[ids[0]]),
                       (ids[-2], ids[-1], x > centers[ids[-1]])]:
        curve[take] = smooth[a]+(x[take]-centers[a])*(smooth[b]-smooth[a])/(centers[b]-centers[a])
    if version == BLENDED_CENTERLINE_VERSION:
        blend = 2*length/n
        for distance, residual, endpoint_supported in [
                (x, curve[0], supported[0]), (length-x, curve[-1], supported[-1])]:
            if endpoint_supported:
                t = np.clip(distance/blend, 0, 1)
                curve -= residual*(1-3*t*t+2*t*t*t)
    return x, curve, supported, edges, good


def _shape(u, z, low, high):
    """Quadratic fit to transverse-bin median heights; no filled profile."""
    if high-low < 40:
        return None, None, None, 0.
    take = (u >= low) & (u <= high)
    u, z = u[take], z[take]
    edges = np.linspace(low, high, 13)
    bid = np.minimum(np.searchsorted(edges, u, side='right')-1, 11)
    pairs = [(float(np.median(u[bid == j])), float(np.median(z[bid == j])))
             for j in range(12) if np.count_nonzero(bid == j) >= 2]
    coverage = len(pairs)/12
    if len(pairs) < 6:
        return None, None, None, coverage
    t = np.asarray(pairs)
    if np.ptp(t[:, 0]) < .7*(high-low):
        return None, None, None, coverage
    v = (t[:, 0]-(low+high)/2)/(high-low)
    a, b, c = np.polyfit(v, t[:, 1], 2)
    error = float(np.sqrt(np.mean((np.polyval([a, b, c], v)-t[:, 1])**2)))
    curvature = float((2*a/(high-low)**2)/(1+(b/(high-low))**2)**1.5)
    bulge = float(-a/4)  # fitted center minus mean fitted heights at both limits
    return curvature, bulge, error, coverage


def _visible_transverse_arc(u, z, low, high):
    """Length of an observed transverse median polyline, never across gaps.

    Use the same 2--98% lateral range as width. Bin medians suppress duplicate
    samples along the finite-thickness band; endpoints are the first/last
    supported median nodes, with no extrapolation to the width limits.
    """
    if high-low < 10:
        return None, 'narrow_span', 0
    count = int(np.clip(np.ceil((high-low)/ARC_BIN_MM), 2, 256))
    take = (u >= low) & (u <= high)
    u, z = u[take], z[take]
    edges = np.linspace(low, high, count+1)
    bins = np.minimum(np.searchsorted(edges, u, side='right')-1, count-1)
    nodes = []
    for i in range(count):
        selected = bins == i
        if np.count_nonzero(selected) < ARC_MIN_BIN_POINTS:
            return None, 'transverse_gap_or_sparse_bin', count
        nodes.append([float(np.median(u[selected])), float(np.median(z[selected]))])
    delta = np.diff(np.asarray(nodes), axis=0)
    # A large vertical jump may join disconnected surfaces despite XY coverage.
    # This conservative threshold is an implementation guard, not calibrated
    # anatomical evidence that the remaining surface is complete.
    if np.any(np.abs(delta[:, 1]) > np.maximum(20., 4*delta[:, 0])):
        return None, 'transverse_height_jump', count
    return float(np.hypot(delta[:, 0], delta[:, 1]).sum()), '', count


def profile_selection(points, row, config, length_mm):
    p = np.asarray(points)
    delta = p[:, :2]-[row['center_x_mm'], row['center_y_mm']]
    tangent = np.array([row['tangent_x'], row['tangent_y']])
    return ((p[:, 0] >= 0) & (p[:, 0] <= length_mm)
            & (np.abs(delta @ tangent) <= config['thickness_mm']/2)
            & (np.abs(delta[:, 0]) <= length_mm*.2))


def calculate(points, length_mm, config=None, conditions=None, reviews=None, provenance=None,
              centerline_version=CENTERLINE_VERSION):
    config, conditions = settings(config), quality(conditions)
    if (isinstance(length_mm, bool) or not isinstance(length_mm, (int, float))
            or not np.isfinite(length_mm) or not 50 <= length_mm <= 5000):
        raise ValueError('端点地面投影距离应在 50–5000 mm 之间')
    p = np.asarray(points, float)
    if p.ndim != 2 or p.shape[1] != 3 or not np.isfinite(p).all():
        raise ValueError('需要有限的 N×3 毫米猪体坐标')
    original_count = len(p)
    p = p[(p[:, 0] >= 0) & (p[:, 0] <= length_mm)]
    if centerline_version not in {CENTERLINE_VERSION, BLENDED_CENTERLINE_VERSION, LEGACY_CENTERLINE_VERSION}:
        raise ValueError('中心线算法版本不受支持')
    cx, cy, support, seeds, seed_good = _centerline(p, length_mm, config['min_points'], centerline_version)
    ds = np.hypot(np.diff(cx), np.diff(cy))
    arc = np.r_[0., np.cumsum(ds)]
    fractions = np.r_[np.linspace(0, 1, config['count']), .25, .5, .75]
    stations = fractions*arc[-1]
    xs, ys = np.interp(stations, arc, cx), np.interp(stations, arc, cy)
    slope = np.interp(stations, arc, np.gradient(cy, cx))
    supported = np.interp(stations, arc, support.astype(float)) >= 1-1e-8
    reference_z = float(np.percentile(p[:, 2], 5)) if len(p) else None
    rows = []
    for i, (f, s, x, y, dy) in enumerate(zip(fractions, stations, xs, ys, slope)):
        tangent = np.array([1., dy])/np.hypot(1, dy)
        normal = np.array([-tangent[1], tangent[0]])
        # Prevent distant parts of a bent body from re-entering the same plane.
        # The y(x) model deliberately does not support folded/U-shaped postures.
        delta = p[:, :2]-[x, y]
        along, across = delta @ tangent, delta @ normal
        take = (np.abs(along) <= config['thickness_mm']/2) & (np.abs(delta[:, 0]) <= length_mm*.2)
        u, z = across[take], p[take, 2]
        available = bool(supported[i] and len(z) >= config['min_points'])
        status = (reviews or {}).get(str(i), 'pending')
        if status not in {'pending', 'usable', 'unusable'} or (status == 'usable' and not available):
            status = 'pending'
        low, high = np.percentile(u, [2, 98]) if available else (None, None)
        curve, bulge, error, coverage = _shape(u, z, low, high) if available else (None, None, None, None)
        transverse_arc, arc_reason, arc_bins = (_visible_transverse_arc(u, z, low, high) if available
            else (None, 'profile_unavailable', 0))
        h = float(np.percentile(z, 95)) if available else None
        row = {'index': i, 'fraction': float(f), 'arc_position_mm': float(s),
               'center_x_mm': float(x), 'center_y_mm': float(y),
               'tangent_x': float(tangent[0]), 'tangent_y': float(tangent[1]),
               'centerline_supported': bool(supported[i]), 'status': status,
               'point_count': int(len(z)), 'width_q02_q98_mm': float(high-low) if available else None,
               'width_raw_span_mm': float(np.ptp(u)) if available else None,
               'height_p95_mm': h if conditions['ground'] == 'reliable' else None,
               'relative_height_p95_mm': h-reference_z if available else None,
               'height_gradient': None, 'transverse_curvature_per_mm': curve,
               'bulge_mm': bulge, 'fit_rmse_mm': error, 'transverse_bin_coverage': coverage,
               'visible_transverse_arc_mm': transverse_arc,
               'visible_transverse_arc_missing_reason': arc_reason,
               'visible_transverse_arc_bin_count': arc_bins,
               'missing_reason': '' if available else 'centerline_gap' if not supported[i] else 'insufficient_points'}
        rows.append(row)
    fixed_rows, rows = rows[config['count']:], rows[:config['count']]
    for i, row in enumerate(rows):
        a, b = rows[max(0, i-1)], rows[min(len(rows)-1, i+1)]
        if all(r['height_p95_mm'] is not None and r['status'] != 'unusable' for r in (a, row, b)):
            row['height_gradient'] = (b['height_p95_mm']-a['height_p95_mm'])/(b['arc_position_mm']-a['arc_position_mm'])
    warnings = []
    if centerline_version != CENTERLINE_VERSION:
        warnings.append('沿用历史中心线及其剖面复核；点击“计算并保存参数”可改用端点约束中心线，更新后需重新复核剖面。')
    if not support.all(): warnings.append('中心线有缺失支撑；跨缺口剖面与完整弧长置为缺失。')
    if conditions['endpoints'] != 'complete': warnings.append('尾根与颈躯连接点完整性未确认；长度和定长位置须复核。')
    if conditions['ground'] != 'reliable': warnings.append('地面高度参考未复核；仅输出相对高度，地面高度与积分代理缺失。')
    if len(p) and np.mean(p[:, 2] < -20) > .01: warnings.append('超过 1% 点低于地面 20 mm，请复核地面与飞点。')
    good_rows = [r for r in rows if r['width_q02_q98_mm'] is not None and r['status'] != 'unusable']
    widths = [r['width_q02_q98_mm'] for r in good_rows]
    g = {}
    def put(name, value, unit, reviewed=False):
        g[name] = {'value': value, 'unit': unit, 'valid': value is not None,
                   'reviewed': bool(value is not None and reviewed)}
    endpoint_ok = conditions['endpoints'] == 'complete'
    full_path = bool(support.all() and conditions['endpoints'] != 'truncated')
    all_reviewed = all(r['status'] == 'usable' for r in rows) and endpoint_ok and full_path
    put('endpoint_ground_chord_mm', float(length_mm) if conditions['endpoints'] != 'truncated' else None, 'mm', endpoint_ok)
    put('visible_centerline_arc_mm', float(arc[-1]) if full_path else None, 'mm', endpoint_ok)
    put('centerline_arc_chord_ratio', float(arc[-1]/length_mm) if full_path else None, 'ratio', endpoint_ok)
    for label, op in [('median', np.median), ('max', np.max), ('p90', lambda v: np.percentile(v, 90))]:
        put('profile_width_'+label+'_mm', float(op(widths)) if widths else None, 'mm', all_reviewed)
    for r in fixed_rows:
        # These exact planes are additional candidate measurements; a nearby
        # fixed-K row's review never confirms a different cross-section.
        r['status'] = 'pending'
        put(f'width_{int(r["fraction"]*100)}pct_mm', r['width_q02_q98_mm'] if full_path else None, 'mm')
    ground = conditions['ground'] == 'reliable'
    put('visible_height_p95_mm', float(np.percentile(p[:, 2], 95)) if ground and len(p) else None, 'mm', ground)
    put('relative_height_p95_p05_mm', float(np.percentile(p[:, 2], 95)-reference_z) if len(p) else None, 'mm')
    cell = config['grid_mm']
    integral = 0.
    if len(p):
        keys, inv = np.unique(np.floor(p[:, :2]/cell).astype(np.int64), axis=0, return_inverse=True)
        # Sorted groups avoid an O(cells * points) loop.
        order = np.argsort(inv, kind='stable')
        cuts = np.r_[0, np.flatnonzero(np.diff(inv[order]))+1, len(p)]
        if ground:
            integral = sum(max(0., float(np.median(p[order[a:b], 2]))) for a, b in zip(cuts[:-1], cuts[1:]))*cell**2
        cells = len(keys)
    else:
        cells = 0
    put('observed_grid_area_mm2', float(cells*cell**2) if cells else None, 'mm2')
    put('observed_positive_height_integral_mm3', float(integral) if cells and ground else None, 'mm3')
    return {'schema_version': VERSION, 'settings': config, 'conditions': conditions,
            'provenance': provenance or {}, 'channels': CHANNELS,
            'geometry': g, 'profiles': rows, 'fixed_fraction_profiles': fixed_rows,
            'centerline': {'xy_mm': np.column_stack([cx, cy]).tolist(), 'supported': support.astype(int).tolist(),
                           'seed_edges_mm': seeds.tolist(), 'seed_supported': seed_good.astype(int).tolist(),
                           'version': centerline_version,
                           'model': {
                               CENTERLINE_VERSION: 'whole-interval endpoint-constrained cubic smoothing spline of visible mid-envelope y(x); no posture restoration',
                               BLENDED_CENTERLINE_VERSION: 'endpoint-constrained local-smoothed visible mid-envelope y(x); no posture restoration',
                               LEGACY_CENTERLINE_VERSION: 'local-smoothed visible mid-envelope y(x); no posture restoration',
                           }[centerline_version]},
            'quality': {'input_retained_points': original_count, 'points_in_endpoint_interval': len(p),
                        'sequence_position_valid': full_path,
                        'computable_profiles': len(good_rows), 'reviewed_profiles': sum(r['status'] == 'usable' for r in rows),
                        'computable_transverse_arcs': sum(r['visible_transverse_arc_mm'] is not None
                            and r['status'] != 'unusable' for r in rows),
                        'all_profiles_reviewed': all_reviewed, 'warnings': warnings},
            'definitions': {'width': 'local normal-plane visible 2–98 percentile lateral span, not anatomical full width',
                            'height': 'p95 Z relative to reviewed local ground; not necessarily gravitational vertical',
                            'relative_height': 'profile p95 Z minus retained interval global p05 Z',
                            'visible_transverse_arc': {
                                'method': 'polyline length through transverse-bin median (u,Z) nodes inside lateral P02--P98; first/last median endpoints; no extrapolation or gap bridging; not girth',
                                'target_bin_mm': ARC_BIN_MM, 'maximum_bins': 256,
                                'minimum_points_per_bin': ARC_MIN_BIN_POINTS,
                                'minimum_width_mm': 10.,
                                'maximum_vertical_step': 'max(20 mm, 4 * adjacent median horizontal distance)',
                            },
                            'area': 'occupied XY grid cells times cell area; holes unfilled; resolution dependent',
                            'integral': 'positive median Z per occupied XY cell times cell area; visible-surface proxy, not body volume',
                            'mask': 'valid = numerically available; usable = valid and not rejected; reviewed = explicit boundary confirmation',
                            'endpoints': 'tail root to neck/trunk junction; no ear-root inference',
                            'scope': 'candidate features from a single visible surface; no weight labels or weight prediction'}}


def arrays(result):
    names = list(CHANNELS)
    rows = result['profiles']
    values = np.array([[r[k] if r[k] is not None else np.nan for k in names] for r in rows], np.float64)
    valid = np.isfinite(values)
    usable = valid & np.array([r['status'] != 'unusable' and r['centerline_supported'] for r in rows])[:, None]
    usable &= result['quality']['sequence_position_valid']
    reviewed = usable & np.array([r['status'] == 'usable' for r in rows])[:, None]
    reviewed &= result['conditions']['endpoints'] == 'complete'
    return {'values': values, 'valid_mask': valid, 'usable_mask': usable, 'reviewed_mask': reviewed,
            'channel_names': np.array(names), 'channel_units': np.array(list(CHANNELS.values())),
            'fraction': np.array([r['fraction'] for r in rows]),
            'arc_position_mm': np.array([r['arc_position_mm'] for r in rows]),
            'centerline_version': np.array(result['centerline'].get('version', LEGACY_CENTERLINE_VERSION)),
            'schema_version': np.array(VERSION)}


def write_products(folder, result):
    # JSON retains G, definitions, provenance and profiles; NPZ contains no object arrays.
    (folder/'geometry_features.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    (folder/'profile_sequence.csv').write_bytes(csv_bytes(result))
    np.savez_compressed(folder/'profile_sequence.npz', **arrays(result))


def csv_bytes(result):
    stream = io.StringIO(newline='')
    rows, a = [], arrays(result)
    for i, row in enumerate(result['profiles']):
        rows.append({**row, 'centerline_version': str(a['centerline_version']), **{f'{kind}_{key}': int(a[kind+'_mask'][i, j])
            for kind in ('valid', 'usable', 'reviewed') for j, key in enumerate(CHANNELS)}})
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def aggregate(folder, frames):
    rows = []
    for row, report in frames:
        source = folder/row['storage_relative_dir']/'geometry_features.json'
        feature = json.loads(source.read_text(encoding='utf-8')) if source.is_file() else None
        item = {k: row.get(k, '') for k in ('relative_dir', 'storage_relative_dir', 'pig_id', 'capture_id')}
        item.update(input_mode=report.get('input_mode', ''), camera=json.dumps(report.get('camera', {}), ensure_ascii=False),
                    schema_version=VERSION, feature_status='computed' if feature else 'unaligned',
                    weight_kg='', weight_status='not_provided')
        item.update(reference_id='', centerline_version='', input_points_sha256='', cleaning_digest='', original_valid_depth_ratio='',
                    retained_points='', points_in_endpoint_interval='', profile_count='', computable_profiles='',
                    reviewed_profiles='', sequence_position_valid=False, endpoint_condition='unknown',
                    ground_condition='unknown', settings_json='', all_profiles_reviewed=False)
        for key in GEOMETRY_FIELDS:
            item.update({key: None, key+'_valid': 0, key+'_reviewed': 0})
        if feature:
            item.update(reference_id=feature['provenance']['reference_id'],
                        centerline_version=feature['centerline'].get('version', LEGACY_CENTERLINE_VERSION),
                        input_points_sha256=feature['provenance']['input_points_sha256'],
                        cleaning_digest=feature['provenance']['cleaning_digest'],
                        original_valid_depth_ratio=feature['provenance']['original_valid_depth_ratio'],
                        retained_points=feature['quality']['input_retained_points'],
                        points_in_endpoint_interval=feature['quality']['points_in_endpoint_interval'],
                        profile_count=len(feature['profiles']), computable_profiles=feature['quality']['computable_profiles'],
                        reviewed_profiles=feature['quality']['reviewed_profiles'],
                        sequence_position_valid=feature['quality']['sequence_position_valid'],
                        endpoint_condition=feature['conditions']['endpoints'], ground_condition=feature['conditions']['ground'],
                        settings_json=json.dumps(feature['settings'], sort_keys=True),
                        all_profiles_reviewed=feature['quality']['all_profiles_reviewed'])
            for key, value in feature['geometry'].items():
                item[key] = value['value']
                item[key+'_valid'] = int(value['valid'])
                item[key+'_reviewed'] = int(value['reviewed'])
        rows.append(item)
    columns = list(dict.fromkeys(k for r in rows for k in r))
    with (folder/'geometry_features.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
