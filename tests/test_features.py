"""Analytical surfaces, missingness, persisted reviews and actual export inputs."""
import base64
import copy
import csv
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from pointcloud_workbench import features
from pointcloud_workbench.core import sha256, read_json
from pointcloud_workbench.workspace import Workspace
from pointcloud_workbench.app import make_handler
from pointcloud_workbench.tests import test_alignment as fixture


def surface(bent=False, step=2):
    x, u = np.meshgrid(np.arange(0, 1000.1, step), np.linspace(-100, 100, 81), indexing='ij')
    y = 180*np.sin(np.pi*x/1000) if bent else x*0
    dy = 180*np.pi/1000*np.cos(np.pi*x/1000) if bent else x*0
    t = np.sqrt(1+dy*dy)
    # 200 mm transverse normal span, 600 mm ridge and 550 mm at both edges.
    return np.column_stack([(x-u*dy/t).ravel(), (y+u/t).ravel(), (600-.005*u*u).ravel()])


class FeatureMathTest(unittest.TestCase):
    def test_only_reviewed_historical_planes_keep_the_old_model(self):
        for version in (features.LEGACY_CENTERLINE_VERSION, features.BLENDED_CENTERLINE_VERSION):
            self.assertEqual(features.centerline_version({'centerline_version': version}), features.CENTERLINE_VERSION)
            self.assertEqual(features.centerline_version({'centerline_version': version,
                'feature_reviews': {'0': 'pending'}}), features.CENTERLINE_VERSION)
            self.assertEqual(features.centerline_version({'centerline_version': version,
                'feature_reviews': {'0': 'usable'}}), version)

    def test_centerline_joins_marked_endpoints_and_keeps_supported_bend(self):
        p = surface(True)+[0, 65, 0]
        old = features.calculate(p, 1000, centerline_version=features.LEGACY_CENTERLINE_VERSION)
        got = features.calculate(p, 1000)
        xy = np.asarray(got['centerline']['xy_mm'])
        np.testing.assert_allclose(xy[[0, -1]], [[0, 0], [1000, 0]], atol=1e-10)
        self.assertGreater(xy[128, 1], 200)  # Follow the bend, not a straight chord.
        self.assertLess(np.max(np.abs(xy[64:193, 1]-np.asarray(old['centerline']['xy_mm'])[64:193, 1])), 25)
        for row, endpoint in zip([got['profiles'][0], got['profiles'][-1]], xy[[0, -1]]):
            np.testing.assert_allclose([row['center_x_mm'], row['center_y_mm']], endpoint)
        self.assertNotEqual(got['geometry']['visible_centerline_arc_mm'], old['geometry']['visible_centerline_arc_mm'])
        self.assertNotEqual(got['profiles'][0]['tangent_y'], old['profiles'][0]['tangent_y'])
        self.assertEqual(got['centerline']['version'], features.CENTERLINE_VERSION)
        self.assertEqual(got['centerline']['xy_mm'], features.calculate(p, 1000, {'count': 64})['centerline']['xy_mm'])

    def test_endpoints_constrain_the_whole_fit_without_short_end_connectors(self):
        p = surface()+[0, 55, 0]
        got = features.calculate(p, 1000)
        xy = np.asarray(got['centerline']['xy_mm'])
        blended = features.calculate(p, 1000, centerline_version=features.BLENDED_CENTERLINE_VERSION)
        old_xy = np.asarray(blended['centerline']['xy_mm'])
        # A nearly parallel but offset envelope must yield a broad smooth bow,
        # not preserve its old flat middle and turn sharply at each endpoint.
        self.assertLess(xy[32, 1], 40)
        self.assertGreater(xy[128, 1], 40)
        self.assertGreater(np.max(np.abs(xy[32:65, 1]-old_xy[32:65, 1])), 10)
        slope = np.diff(xy[:, 1])/np.diff(xy[:, 0])
        self.assertLess(np.max(np.abs(slope)), .4)
        self.assertLess(np.max(np.abs(np.diff(slope))), .015)
        np.testing.assert_allclose(xy[:, 1], xy[::-1, 1], atol=1e-8)
        self.assertTrue(all(got['centerline']['supported']))

    def test_endpoint_constraints_do_not_create_support_for_missing_ends(self):
        p = surface()+[0, 50, 0]
        got = features.calculate(p[p[:, 0] >= 100], 1000)
        self.assertFalse(got['centerline']['supported'][0])
        self.assertFalse(got['profiles'][0]['centerline_supported'])
        self.assertIsNone(got['geometry']['visible_centerline_arc_mm']['value'])
        self.assertIsNone(got['profiles'][0]['width_q02_q98_mm'])
        self.assertFalse(features.arrays(got)['usable_mask'].any())

    def test_straight_surface_known_width_arc_curvature_and_height(self):
        p = surface(); original = p.copy()
        r = features.calculate(p, 1000, conditions={'endpoints':'complete', 'ground':'reliable'})
        g, row = r['geometry'], r['profiles'][16]
        self.assertAlmostEqual(g['visible_centerline_arc_mm']['value'], 1000, places=5)
        self.assertAlmostEqual(row['width_q02_q98_mm'], 195, delta=3)
        self.assertAlmostEqual(row['transverse_curvature_per_mm'], -.01, delta=.0005)
        self.assertAlmostEqual(row['bulge_mm'], 47.5, delta=3)
        self.assertAlmostEqual(row['height_p95_mm'], 600, delta=1)
        self.assertGreater(row['visible_transverse_arc_mm'], row['width_q02_q98_mm'])
        self.assertEqual(len(r['profiles']), 32)
        self.assertEqual([r['fraction'] for r in r['fixed_fraction_profiles']], [.25, .5, .75])
        self.assertAlmostEqual(g['width_25pct_mm']['value'], row['width_q02_q98_mm'], delta=1)
        self.assertFalse(features.arrays(r)['reviewed_mask'].any())
        np.testing.assert_array_equal(p, original)

    def test_transverse_arc_matches_analytic_curve_and_flat_span(self):
        u = np.linspace(-100, 100, 2001)
        flat, reason, count = features._visible_transverse_arc(u, u*0+600, -100, 100)
        self.assertEqual(reason, '')
        self.assertEqual(count, 40)
        self.assertAlmostEqual(flat, 195, delta=.1)
        curved, reason, _ = features._visible_transverse_arc(u, 600-.005*u*u, -100, 100)
        # Integral of sqrt(1 + (.01*u)^2) over the observed median-node span.
        edge = 97.5
        expected = edge*np.sqrt(1+(.01*edge)**2)+np.arcsinh(.01*edge)/.01
        self.assertAlmostEqual(curved, expected, delta=.2)
        self.assertGreater(curved, flat)
        shifted, _, _ = features._visible_transverse_arc(u+200, 680-.005*u*u, 100, 300)
        self.assertAlmostEqual(shifted, curved, places=7)

    def test_transverse_arc_rejects_holes_sparse_bins_and_height_jumps(self):
        u = np.linspace(-100, 100, 2001)
        take = np.abs(u)>12
        for x, z, expected in [
            (u[take], 600-.005*u[take]**2, 'transverse_gap_or_sparse_bin'),
            (np.linspace(-100,100,41), np.full(41,600), 'transverse_gap_or_sparse_bin'),
            (u, np.where(u<0, 500, 600), 'transverse_height_jump'),
        ]:
            value, reason, _ = features._visible_transverse_arc(x, z, -100, 100)
            self.assertIsNone(value)
            self.assertEqual(reason, expected)
        p = surface()
        p = p[np.abs(p[:,1])>30]
        result = features.calculate(p, 1000, conditions={'endpoints':'complete'}, reviews={'16':'usable'})
        row = result['profiles'][16]
        self.assertIsNotNone(row['width_q02_q98_mm'])
        self.assertIsNone(row['visible_transverse_arc_mm'])
        self.assertEqual(row['visible_transverse_arc_missing_reason'], 'transverse_gap_or_sparse_bin')
        a = features.arrays(result)
        index = list(features.CHANNELS).index('visible_transverse_arc_mm')
        self.assertTrue(np.isnan(a['values'][16,index]))
        self.assertFalse(a['valid_mask'][16,index])
        self.assertFalse(a['usable_mask'][16,index])
        self.assertFalse(a['reviewed_mask'][16,index])
        self.assertEqual(result['quality']['computable_transverse_arcs'], 0)

    def test_bent_tube_uses_local_normal_and_arc_not_straight_x_width(self):
        r = features.calculate(surface(True), 1000, conditions={'ground':'reliable'})
        rows = r['profiles'][3:-3]
        self.assertTrue(all(abs(r['width_q02_q98_mm']-192) < 10 for r in rows))
        self.assertGreater(r['geometry']['visible_centerline_arc_mm']['value'], 1040)
        slopes = [row['tangent_y'] for row in rows]
        self.assertGreater(max(slopes), .3)
        self.assertLess(min(slopes), -.3)
        self.assertNotEqual(r['geometry']['endpoint_ground_chord_mm']['value'], r['geometry']['visible_centerline_arc_mm']['value'])

    def test_physical_scale_and_translation_preserve_expected_dimensions(self):
        config = {'grid_mm': 10, 'thickness_mm': 20}
        p = surface()
        a = features.calculate(p, 1000, config, {'ground':'reliable'})
        b = features.calculate(p*2, 2000, {'grid_mm':20, 'thickness_mm':40}, {'ground':'reliable'})
        self.assertAlmostEqual(b['geometry']['profile_width_median_mm']['value']/a['geometry']['profile_width_median_mm']['value'], 2, places=3)
        self.assertAlmostEqual(b['geometry']['observed_grid_area_mm2']['value']/a['geometry']['observed_grid_area_mm2']['value'], 4, places=5)
        self.assertAlmostEqual(b['geometry']['observed_positive_height_integral_mm3']['value']/a['geometry']['observed_positive_height_integral_mm3']['value'], 8, places=5)
        c = features.calculate(p+[0,200,80], 1000, config, {'ground':'reliable'})
        self.assertAlmostEqual(c['profiles'][16]['relative_height_p95_mm'], a['profiles'][16]['relative_height_p95_mm'])
        self.assertAlmostEqual(c['profiles'][16]['height_p95_mm']-a['profiles'][16]['height_p95_mm'], 80)

    def test_holes_and_unknown_ground_stay_missing_not_filled_zero(self):
        p = surface();p = p[(p[:,0] < 420) | (p[:,0] > 580)]
        r = features.calculate(p, 1000)
        self.assertIsNone(r['geometry']['visible_centerline_arc_mm']['value'])
        self.assertIsNone(r['geometry']['width_50pct_mm']['value'])
        self.assertIsNone(r['geometry']['visible_height_p95_mm']['value'])
        self.assertTrue(any(row['missing_reason']=='centerline_gap' for row in r['profiles']))
        a = features.arrays(r)
        self.assertTrue(np.isnan(a['values']).any())
        self.assertFalse(a['usable_mask'].any())
        self.assertEqual(json.loads(json.dumps(r, allow_nan=False))['schema_version'], features.VERSION)
        full = features.calculate(surface(), 1000, conditions={'endpoints':'truncated'})
        self.assertIsNone(full['geometry']['endpoint_ground_chord_mm']['value'])
        self.assertFalse(features.arrays(full)['usable_mask'].any())

    def test_grid_integrates_only_occupied_cells_and_masks_rejections(self):
        r = features.calculate(surface(), 1000, conditions={'ground':'reliable', 'endpoints':'complete'},
            reviews={str(i):'usable' if i!=12 else 'unusable' for i in range(32)})
        a=features.arrays(r)
        self.assertTrue(a['valid_mask'][12,0])
        self.assertFalse(a['usable_mask'][12].any())
        self.assertTrue(a['reviewed_mask'][11,0])
        self.assertFalse(r['quality']['all_profiles_reviewed'])
        # Removing an entire central strip must reduce the occupied area and
        # height integral; there is no hull fill through the hole.
        p = surface();p = p[np.abs(p[:,1])>30]
        h = features.calculate(p, 1000, conditions={'ground':'reliable'})
        self.assertLess(h['geometry']['observed_grid_area_mm2']['value'], r['geometry']['observed_grid_area_mm2']['value']*.85)
        self.assertLess(h['geometry']['observed_positive_height_integral_mm3']['value'], r['geometry']['observed_positive_height_integral_mm3']['value']*.85)

    def test_invalid_config_empty_cloud_and_safe_npz(self):
        for config in [{'count':33}, {'min_points':3.5}, {'grid_mm':0}, {'count':True}, {'extra':1}, [], False]:
            with self.assertRaises(ValueError):features.calculate(surface(),1000,config)
        r=features.calculate(np.empty((0,3)),1000)
        self.assertTrue(all(r['width_q02_q98_mm'] is None for r in r['profiles']))
        with tempfile.TemporaryDirectory() as folder:
            features.write_products(Path(folder),r)
            with np.load(Path(folder)/'profile_sequence.npz',allow_pickle=False) as saved:
                self.assertEqual(saved['values'].shape,(32,len(features.CHANNELS)))
                self.assertTrue(np.isnan(saved['values'][:,0]).all())
                self.assertEqual(saved['channel_names'][0],'width_q02_q98_mm')
                self.assertEqual(saved['channel_names'][-1],'visible_transverse_arc_mm')
                self.assertEqual(saved['values'].shape, (32,10))
                self.assertEqual(saved['channel_units'][-1], 'mm')


class FeatureWorkflowTest(unittest.TestCase):
    setUp=fixture.AlignmentWorkflowTest.setUp
    tearDown=fixture.AlignmentWorkflowTest.tearDown
    fit=fixture.AlignmentWorkflowTest.fit

    def aligned(self):
        fitted=self.fit()
        self.ws.alignment.confirm({**self.payload,'candidate_id':fitted['candidate_id'],'alignment_confirmed':True})
        response=self.ws.processing.context(self.base)
        self.proc={**self.base,'context_id':response['context_id'],'revision':response['revision']}

    def test_alignment_gate_read_only_computation_and_export_without_edits(self):
        ctx=self.ws.processing.context(self.base)
        with self.assertRaises(ValueError):self.ws.processing.features({**self.base,'context_id':ctx['context_id']})
        self.aligned()
        folder=self.ws.results[self.rel]['folder'];before=sha256(folder/'points.npz')
        response=self.ws.processing.features(self.proc)
        self.assertEqual(response['revision'],'')
        self.assertEqual(response['features']['conditions'], {'endpoints':'complete','ground':'reliable'})
        self.assertEqual(response['features']['provenance']['reference_conditions_source'], 'confirmed_alignment_default')
        self.assertNotIn('processing',self.ws.results[self.rel])
        self.assertEqual(sha256(folder/'points.npz'),before)
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'features-export',
            'aligned_only':True,'decisions':['pending','keep'],'group_by_decision':False},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        dest=self.root/'features-export'/self.rel
        self.assertTrue((dest/'profile_sequence.npz').is_file())
        self.assertTrue((self.root/'features-export'/'geometry_features.csv').is_file())
        computed=read_json(dest/'geometry_features.json')
        self.assertEqual(computed['schema_version'], 'visible-surface-features-v2')
        self.assertEqual(computed['centerline']['version'], features.CENTERLINE_VERSION)
        with np.load(dest/'profile_sequence.npz', allow_pickle=False) as saved:
            self.assertEqual(str(saved['centerline_version']), features.CENTERLINE_VERSION)
        self.assertIn('visible_transverse_arc_mm', computed['profiles'][0])
        with (dest/'profile_sequence.csv').open(encoding='utf-8-sig',newline='') as stream:
            columns = csv.DictReader(stream).fieldnames
        for prefix in ['', 'valid_', 'usable_', 'reviewed_']:
            self.assertIn(prefix+'visible_transverse_arc_mm', columns)
        self.assertIn('centerline_version', columns)
        with (self.root/'features-export'/'geometry_features.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['centerline_version'], features.CENTERLINE_VERSION)
        self.assertEqual(computed['geometry'],response['features']['geometry'])
        self.assertEqual(computed['provenance']['relative_dir'],self.rel)
        self.assertFalse((dest/'pointcloud_cleaning.json').exists())
        summary = read_json(dest/'measurement_summary.json')
        self.assertEqual(summary['slice_count'], 32)
        self.assertEqual(summary['usable_slices'], 0)
        self.assertEqual(read_json(dest/'current_pointcloud.json')['measurement_slice_count'], 32)
        self.assertEqual(sha256(dest/'points.npz'),before)

    def test_settings_review_restart_and_rollback(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'count':16,'min_points':3},
            'conditions':{'ground':'reliable','endpoints':'complete'}})
        self.proc['revision']=response['revision']
        rows=response['features']['profiles'];index=next(i for i,r in enumerate(rows) if r['width_q02_q98_mm'] is not None)
        response=self.ws.processing.features({**self.proc,'profile_index':index,'status':'usable'})
        self.proc['revision']=response['revision']
        self.assertEqual(response['features']['profiles'][index]['status'],'usable')
        self.assertEqual(len(response['profile_yz']),rows[index]['point_count'])
        for form in ['csv','npz']:
            download=self.ws.processing.features({**self.proc,'format':form})['download']
            content=base64.b64decode(download['base64'])
            if form=='npz':
                with np.load(io.BytesIO(content),allow_pickle=False) as saved:
                    self.assertEqual(saved['values'].shape[0],16)
            else:self.assertIn(b'valid_width_q02_q98_mm',content)
        before=copy.deepcopy(self.ws.processing.contexts[self.rel]['measurement'])
        with patch('pointcloud_workbench.workspace.atomic_json',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.ws.processing.features({**self.proc,'settings':{'count':64}})
        self.assertEqual(self.ws.processing.contexts[self.rel]['measurement'],before)
        restored=Workspace(self.root/'state',initial_results=self.results)
        ctx=restored.processing.context({**self.base,'dataset_id':restored.dataset_id})
        got=restored.processing.features({**self.base,'dataset_id':restored.dataset_id,'context_id':ctx['context_id'],'revision':ctx['revision']})
        self.assertEqual(got['features']['profiles'][index]['status'],'usable')
        self.assertEqual(got['features']['settings']['count'],16)
        self.assertEqual(restored.row(self.base['id'])['measurement_usable_slices'], 1)
        self.assertEqual(restored.row(self.base['id'])['measurement_slice_count'], 16)

    def test_sidebar_counts_current_profiles_and_ignores_legacy_slice_labels(self):
        self.aligned()
        service = self.ws.processing
        legacy = service.review({**self.proc, 'thickness_mm': 100, 'min_points': 3, 'slice_index': 0, 'status': 'usable'})
        self.proc['revision'] = legacy['revision']
        self.assertEqual(self.ws.row(self.base['id'])['measurement_usable_slices'], 0)
        for index in range(3):
            response = service.features({**self.proc, 'settings': {'min_points': 3},
                'profile_index': index, 'status': 'usable'})
            self.proc['revision'] = response['revision']
        row = self.ws.row(self.base['id'])
        self.assertEqual((row['measurement_usable_slices'], row['measurement_slice_count']), (3, 32))
        self.assertEqual(row['measurement_usable_slices'], response['features']['quality']['reviewed_profiles'])
        response = service.features({**self.proc, 'settings': {'count': 64, 'min_points': 3}})
        self.proc['revision'] = response['revision']
        row = self.ws.row(self.base['id'])
        self.assertEqual((row['measurement_usable_slices'], row['measurement_slice_count']), (0, 64))
        self.assertFalse(row['measurement_ready'])

    def test_reconfirming_in_the_same_editor_recomputes_geometry_and_profiles(self):
        self.aligned()
        service = self.ws.processing
        first = service.features({**self.proc, 'profile_index': 31, 'status': 'unusable'})
        old_editor = {**self.proc, 'revision': first['revision'], 'profile_index': 31}
        before = self.ws.row(self.base['id'])
        self.ws.review_cloud({**self.base, 'stage': 'review', 'decision': 'keep'})

        # Reuse the open alignment editor and move both endpoints.
        payload = {**self.payload, 'tail_uv': [35, 42], 'neck_uv': [65, 39]}
        fit = self.ws.alignment.fit(payload)
        saved = self.ws.alignment.confirm({**payload, 'candidate_id': fit['candidate_id'], 'alignment_confirmed': True})
        row = saved['workspace_state']['frames'][0]
        self.assertTrue(saved['realigned'])
        self.assertNotEqual(row['alignment_revision'], before['alignment_revision'])
        self.assertTrue(row['workflow_stages']['review']['stale'])
        self.assertEqual(row['workflow_stages']['review']['decision'], 'pending')
        with self.assertRaisesRegex(ValueError, '页面已过期'):
            service.features({**old_editor, 'status': 'usable'})
        new_context = service.context(self.base)
        with self.assertRaisesRegex(ValueError, '对齐坐标已更新'):
            service.features({**self.base, 'context_id': new_context['context_id'],
                'revision': new_context['revision'], 'status': 'usable', 'profile_index': 31,
                'alignment_reference_id': first['features']['provenance']['reference_id']})

        def verify_latest(workspace):
            base = {**self.base, 'dataset_id': workspace.dataset_id}
            context = workspace.processing.context(base)
            features = workspace.processing.features({**base, 'context_id': context['context_id'],
                'revision': context['revision'], 'profile_index': 31})['features']
            length = fit['record']['frame']['neck_tail_horizontal_span_mm']
            self.assertEqual(context['alignment']['reference_id'], saved['reference_id'])
            self.assertEqual(features['provenance']['reference_id'], saved['reference_id'])
            self.assertAlmostEqual(features['geometry']['endpoint_ground_chord_mm']['value'], length)
            self.assertNotEqual(features['geometry']['endpoint_ground_chord_mm'],
                                first['features']['geometry']['endpoint_ground_chord_mm'])
            self.assertNotEqual(features['profiles'][-1]['center_x_mm'], first['features']['profiles'][-1]['center_x_mm'])
            np.testing.assert_allclose(np.asarray(features['centerline']['xy_mm'])[[0, -1]],
                                       [[0, 0], [length, 0]], atol=1e-10)
            self.assertNotEqual(features['centerline']['xy_mm'], first['features']['centerline']['xy_mm'])
            self.assertTrue(all(profile['status'] == 'pending' for profile in features['profiles']))

        verify_latest(self.ws)
        verify_latest(Workspace(self.ws.state_dir, initial_results=self.results))

    def test_historical_review_keeps_its_curve_until_explicit_recalculation(self):
        self.aligned()
        service = self.ws.processing
        ctx = service.contexts[self.rel]
        # A pre-fix record has reviews and no centerline version.
        ctx['measurement'] = {'reference_id': ctx['alignment']['reference_id'],
            'cleaning_digest': service._measurement(ctx)['cleaning_digest'],
            'feature_reviews': {'1': 'unusable'}}
        old = service.features(self.proc)
        self.assertEqual(old['features']['centerline']['version'], features.LEGACY_CENTERLINE_VERSION)
        self.assertEqual(old['features']['profiles'][1]['status'], 'unusable')
        saved = service.features({**self.proc, 'profile_index': 2, 'status': 'unusable'})
        self.proc['revision'] = saved['revision']
        self.ws.review_cloud({**self.base, 'stage': 'review', 'decision': 'keep'})
        before = self.ws.row(self.base['id'])
        restored = Workspace(self.ws.state_dir, initial_results=self.results)
        base = {**self.base, 'dataset_id': restored.dataset_id}
        context = restored.processing.context(base)
        payload = {**base, 'context_id': context['context_id'], 'revision': context['revision']}
        old = restored.processing.features(payload)['features']
        self.assertEqual(old['centerline']['version'], features.LEGACY_CENTERLINE_VERSION)
        self.assertEqual(old['profiles'][1]['status'], 'unusable')
        self.assertEqual(restored.row(base['id'])['workflow_stages']['review'], before['workflow_stages']['review'])
        dest = self.root/'historical-export'; dest.mkdir()
        exported = restored.processing.export_features_to(dest, restored.results[self.rel], source_folder=restored.results[self.rel]['folder'])
        self.assertEqual(exported['centerline'], old['centerline'])
        self.assertEqual(exported['profiles'], old['profiles'])
        new = restored.processing.features({**payload, 'settings': old['settings']})['features']
        self.assertEqual(new['centerline']['version'], features.CENTERLINE_VERSION)
        self.assertEqual(new['provenance']['centerline_version'], features.CENTERLINE_VERSION)
        self.assertTrue(all(row['status'] == 'pending' for row in new['profiles']))
        self.assertTrue(restored.row(base['id'])['workflow_stages']['review']['stale'])
        self.assertEqual(restored.row(base['id'])['workflow_stages']['review']['decision'], 'pending')

    def test_blended_curve_review_keeps_its_method_on_restart_and_upgrades_explicitly(self):
        self.aligned()
        service = self.ws.processing
        ctx = service.contexts[self.rel]
        ctx['measurement'] = {**service._measurement(ctx),
            'centerline_version': features.BLENDED_CENTERLINE_VERSION, 'feature_reviews': {'1': 'unusable'}}
        saved = service.features({**self.proc, 'profile_index': 2, 'status': 'unusable'})
        self.assertEqual(saved['features']['centerline']['version'], features.BLENDED_CENTERLINE_VERSION)
        restored = Workspace(self.ws.state_dir, initial_results=self.results)
        base = {**self.base, 'dataset_id': restored.dataset_id}
        context = restored.processing.context(base)
        payload = {**base, 'context_id': context['context_id'], 'revision': context['revision']}
        old = restored.processing.features(payload)['features']
        self.assertEqual(old['centerline'], saved['features']['centerline'])
        self.assertEqual([r['status'] for r in old['profiles']], [r['status'] for r in saved['features']['profiles']])
        new = restored.processing.features({**payload, 'settings': old['settings']})['features']
        self.assertEqual(new['centerline']['version'], features.CENTERLINE_VERSION)
        self.assertTrue(all(row['status'] == 'pending' for row in new['profiles']))

    def test_cleaning_and_reference_changes_invalidate_only_profile_reviews(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'min_points':3},'conditions':{'ground':'reliable','endpoints':'complete'},'profile_index':16,'status':'usable'})
        self.proc['revision']=response['revision']
        service=self.ws.processing;ctx=service.contexts[self.rel]
        ctx['candidate']={'id':'test','indices':np.array([0,1]),'action':'remove','detail':{}}
        edited=service.edit({**self.proc,'operation':'apply','candidate_id':'test'})
        self.proc['revision']=edited['revision']
        got=service.features(self.proc)['features']
        self.assertEqual(got['conditions']['ground'],'reliable')
        self.assertTrue(all(r['status']=='pending' for r in got['profiles']))
        self.assertEqual(got['quality']['input_retained_points'],len(self.cloud['xyz_mm'])-2)
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'cleaned-features',
            'aligned_only':True,'decisions':['pending','keep'],'group_by_decision':False},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        exported=read_json(self.root/'cleaned-features'/self.rel/'geometry_features.json')
        self.assertEqual(exported['geometry'],got['geometry'])
        self.assertEqual(exported['provenance']['cleaning_digest'],got['provenance']['cleaning_digest'])
        # Old labels expire; references default to the current confirmed frame.
        ctx['measurement'].update(reference_id='old-reference',feature_reviews={'16':'usable'},feature_quality={'ground':'reliable'})
        got=service.features(self.proc)['features']
        self.assertEqual(got['conditions']['ground'],'reliable')
        self.assertEqual(got['profiles'][16]['status'],'pending')

    def test_bulk_ranges_keep_outside_labels_restore_and_export_masks(self):
        self.aligned()
        service=self.ws.processing
        response=service.features({**self.proc,'settings':{'count':16,'min_points':3,'thickness_mm':100}})
        self.proc['revision']=response['revision']
        for bounds,status,expected in [([2,9],'usable',8),([5,6],'unusable',2),([3,4],'pending',2)]:
            response=service.features({**self.proc,'profile_range':bounds,'status':status})
            self.proc['revision']=response['revision']
            self.assertEqual(response['review_update'], {'profile_range':bounds,'status':status,'updated':expected,'skipped':[]})
        profiles=response['features']['profiles']
        expected=['usable' if i in (2,7,8,9) else 'unusable' if i in (5,6) else 'pending' for i in range(16)]
        self.assertEqual([r['status'] for r in profiles],expected)
        self.assertEqual(self.ws.row(self.base['id'])['measurement_usable_slices'],4)
        restored=Workspace(self.root/'state',initial_results=self.results)
        context=restored.processing.context({**self.base,'dataset_id':restored.dataset_id})
        got=restored.processing.features({**self.base,'dataset_id':restored.dataset_id,
            'context_id':context['context_id'],'revision':context['revision']})
        self.assertEqual([r['status'] for r in got['features']['profiles']],expected)
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'bulk-export',
            'stages':['review'],'decisions':['pending','keep'],'group_by_decision':False},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        dest=self.root/'bulk-export'/self.rel
        self.assertEqual(read_json(dest/'geometry_features.json'),response['features'])
        self.assertEqual(read_json(dest/'measurement_summary.json')['usable_slices'],4)
        with np.load(dest/'profile_sequence.npz',allow_pickle=False) as saved:
            self.assertFalse(saved['usable_mask'][5:7].any())
            self.assertTrue(saved['reviewed_mask'][2,0])
            self.assertFalse(saved['reviewed_mask'][3:5].any())

    def test_bulk_confirmation_skips_missing_profiles_and_rejection_covers_them(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'count':64,'min_points':3,'thickness_mm':5}})
        self.proc['revision']=response['revision']
        missing=[r['index'] for r in response['features']['profiles'] if r['width_q02_q98_mm'] is None]
        self.assertTrue(missing)
        self.assertLess(len(missing),64)
        response=self.ws.processing.features({**self.proc,'profile_range':[0,63],'status':'usable'})
        self.proc['revision']=response['revision']
        self.assertEqual(response['review_update']['skipped'],missing)
        self.assertEqual(response['review_update']['updated'],64-len(missing))
        self.assertEqual([r['status'] for r in response['features']['profiles']],
            ['pending' if i in missing else 'usable' for i in range(64)])
        response=self.ws.processing.features({**self.proc,'profile_range':[0,63],'status':'unusable'})
        self.assertTrue(all(r['status']=='unusable' for r in response['features']['profiles']))
        self.assertFalse(features.arrays(response['features'])['usable_mask'].any())

    def test_invalid_bulk_ranges_and_failed_save_leave_prior_version_intact(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'min_points':3}})
        self.proc['revision']=response['revision']
        service=self.ws.processing
        before=copy.deepcopy(service.contexts[self.rel]['measurement'])
        settings_hash=sha256(self.ws.state_dir/'settings.json')
        for bounds in (None,[],[0],[0,1,2],[-1,2],[0,32],[4,2],[1.5,2],[True,2],'all'):
            with self.subTest(bounds=bounds),self.assertRaises(ValueError):
                service.features({**self.proc,'profile_range':bounds,'status':'usable'})
            self.assertEqual(service.contexts[self.rel]['measurement'],before)
            self.assertEqual(service.contexts[self.rel]['revision'],self.proc['revision'])
        with self.assertRaises(ValueError):service.features({**self.proc,'profile_range':[0,5]})
        with patch('pointcloud_workbench.workspace.atomic_json',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):service.features({**self.proc,'profile_range':[0,5],'status':'unusable'})
        self.assertEqual(service.contexts[self.rel]['measurement'],before)
        self.assertEqual(sha256(self.ws.state_dir/'settings.json'),settings_hash)
        response=service.features({**self.proc,'profile_range':[0,5],'status':'usable'})
        self.assertNotEqual(response['revision'],self.proc['revision'])
        with self.assertRaisesRegex(ValueError,'变化'):
            service.features({**self.proc,'profile_range':[6,10],'status':'usable'})

    def test_old_reference_options_do_not_reset_saved_profile_labels(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'min_points':3},'profile_index':16,'status':'usable'})
        self.proc['revision']=response['revision']
        ctx=self.ws.processing.contexts[self.rel]
        ctx['measurement']['feature_quality']={'endpoints':'unknown','ground':'unknown'}
        settings_hash=sha256(self.ws.state_dir/'settings.json')
        got=self.ws.processing.features(self.proc)
        self.assertEqual(got['features']['conditions'], {'endpoints':'complete','ground':'reliable'})
        self.assertEqual(got['features']['profiles'][16]['status'],'usable')
        self.assertEqual(got['revision'],response['revision'])
        self.assertEqual(sha256(self.ws.state_dir/'settings.json'),settings_hash)

    def test_unaligned_batch_reports_missing_features_and_does_not_copy_stale_products(self):
        (self.folder/'geometry_features.json').write_text('{"stale":true}')
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'unaligned-features',
            'aligned_only':False,'decisions':['pending'],'group_by_decision':False},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        folder=self.root/'unaligned-features'
        self.assertFalse((folder/self.rel/'geometry_features.json').exists())
        with (folder/'geometry_features.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['feature_status'],'unaligned')
        self.assertEqual(rows[0]['visible_centerline_arc_mm'],'')
        self.assertEqual(rows[0]['visible_centerline_arc_mm_valid'],'0')
        self.assertEqual(rows[0]['weight_kg'],'')
        self.assertEqual(rows[0]['weight_status'],'not_provided')

    def test_feature_http_route_authentication_and_independent_review_versions(self):
        self.aligned()
        self.ws.review_cloud({**self.base,'decision':'keep'})
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.ws,'features-secret'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}/api/processing/features'
        def request(payload,token='features-secret'):
            return json.load(urlopen(Request(url,data=json.dumps(payload).encode(),
                headers={'Content-Type':'application/json','X-App-Token':token})))
        try:
            with self.assertRaises(HTTPError) as e:request(self.proc,'wrong')
            self.assertEqual(e.exception.code,403)
            got=request({**self.proc,'settings':{'min_points':3},'profile_index':16,'status':'usable'})
            self.proc['revision']=got['revision']
            self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'],'keep')
            self.assertFalse(self.ws.row(self.base['id'])['measurement_ready'])
            self.assertEqual(got['features']['profiles'][16]['status'],'usable')
            self.assertFalse(got['features']['geometry']['profile_width_median_mm']['reviewed'])
            got=request({**self.proc,'profile_range':[2,4],'status':'unusable'})
            self.proc['revision']=got['revision']
            self.assertEqual(got['review_update']['updated'],3)
            self.assertTrue(all(r['status']=='unusable' for r in got['features']['profiles'][2:5]))
            before=got['revision']
            with self.assertRaises(HTTPError) as e:request({**self.proc,'settings':{'count':16},'profile_index':999})
            self.assertEqual(e.exception.code,400)
            self.assertEqual(request(self.proc)['revision'],before)
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
