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
    def test_straight_surface_known_width_arc_curvature_and_height(self):
        p = surface(); original = p.copy()
        r = features.calculate(p, 1000, conditions={'endpoints':'complete', 'ground':'reliable'})
        g, row = r['geometry'], r['profiles'][16]
        self.assertAlmostEqual(g['visible_centerline_arc_mm']['value'], 1000, places=5)
        self.assertAlmostEqual(row['width_q02_q98_mm'], 195, delta=3)
        self.assertAlmostEqual(row['transverse_curvature_per_mm'], -.01, delta=.0005)
        self.assertAlmostEqual(row['bulge_mm'], 47.5, delta=3)
        self.assertAlmostEqual(row['height_p95_mm'], 600, delta=1)
        self.assertEqual(len(r['profiles']), 32)
        self.assertEqual([r['fraction'] for r in r['fixed_fraction_profiles']], [.25, .5, .75])
        self.assertAlmostEqual(g['width_25pct_mm']['value'], row['width_q02_q98_mm'], delta=1)
        self.assertFalse(features.arrays(r)['reviewed_mask'].any())
        np.testing.assert_array_equal(p, original)

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
        self.assertEqual(computed['geometry'],response['features']['geometry'])
        self.assertEqual(computed['provenance']['relative_dir'],self.rel)
        self.assertFalse((dest/'pointcloud_cleaning.json').exists())
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

    def test_cleaning_and_reference_changes_invalidate_only_profile_reviews(self):
        self.aligned()
        response=self.ws.processing.features({**self.proc,'settings':{'min_points':3},'conditions':{'ground':'reliable','endpoints':'complete'},'profile_index':16,'status':'usable'})
        self.proc['revision']=response['revision']
        service=self.ws.processing;ctx=service.contexts[self.rel]
        ctx['candidate']={'id':'test','indices':np.array([0,1]),'action':'remove','detail':{}}
        edited=service.edit({**self.proc,'operation':'apply','candidate_id':'test'})
        self.proc['revision']=edited['revision']
        got=service.features(self.proc)['features']
        self.assertEqual(got['conditions']['ground'],'unknown')
        self.assertTrue(all(r['status']=='pending' for r in got['profiles']))
        self.assertEqual(got['quality']['input_retained_points'],len(self.cloud['xyz_mm'])-2)
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'cleaned-features',
            'aligned_only':True,'decisions':['pending','keep'],'group_by_decision':False},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        exported=read_json(self.root/'cleaned-features'/self.rel/'geometry_features.json')
        self.assertEqual(exported['geometry'],got['geometry'])
        self.assertEqual(exported['provenance']['cleaning_digest'],got['provenance']['cleaning_digest'])
        # Old reference-bound conditions may not be reused on a new frame.
        ctx['measurement'].update(reference_id='old-reference',feature_reviews={'16':'usable'},feature_quality={'ground':'reliable'})
        got=service.features(self.proc)['features']
        self.assertEqual(got['conditions']['ground'],'unknown')
        self.assertEqual(got['profiles'][16]['status'],'pending')

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
            before=got['revision']
            with self.assertRaises(HTTPError) as e:request({**self.proc,'settings':{'count':16},'profile_index':999})
            self.assertEqual(e.exception.code,400)
            self.assertEqual(request(self.proc)['revision'],before)
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
