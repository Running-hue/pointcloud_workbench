"""Full-density selection, reversible histories, measurement gates and exports."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pointcloud_workbench.core import np, sha256, read_json, write_json
from pointcloud_workbench.processing import (detect_candidates, screen_selection,
    measure_slices, mask_digest, VERSION)
from pointcloud_workbench.workspace import Workspace
from pointcloud_workbench.app import make_handler
from pointcloud_workbench.tests import test_alignment as fixture


class ProcessingMathTest(unittest.TestCase):
    def test_far_candidates_preserve_input_and_do_not_trim_normal_body(self):
        rng = np.random.default_rng(2)
        body = rng.uniform([0, -180, 800], [1100, 180, 1300], (4000, 3)).astype(np.float32)
        points = np.vstack([body, [[-5000, 20, 11000], [10, 0, 9000]]]).astype(np.float32)
        before = points.copy()
        selected, detail = detect_candidates(points)
        self.assertEqual(np.flatnonzero(selected).tolist(), [4000, 4001])
        self.assertTrue(detail['candidate_only'])
        np.testing.assert_array_equal(before, points)

    def test_neighbor_candidates_use_physical_floor_and_include_isolated_point(self):
        points = np.vstack([np.random.default_rng(3).normal(0, 2, (500, 3)), [200, 0, 0]]).astype(np.float32)
        selected, detail = detect_candidates(points, 'neighbor', neighbor_mm=40)
        self.assertEqual(np.flatnonzero(selected).tolist(), [500])
        self.assertGreaterEqual(detail['neighbor_threshold_mm'], 40)

    def test_rectangle_selects_all_depths_and_full_resolution(self):
        # Coincident projections are both selected, including the occluded point.
        p = np.array([[0,0,0], [0,0,1000], [5,0,0], [100,100,0]], float)
        view = {'rotation': np.eye(3).tolist(), 'center': [0,0,0], 'scale': 1, 'size': [200,100]}
        np.testing.assert_array_equal(screen_selection(p, view, [98,48,102,52]), [True,True,False,False])
        with self.assertRaises(ValueError): screen_selection(p, {**view, 'rotation': [[1,0,0]]*3}, [0,0,10,10])

    def test_slices_keep_definitions_separate_and_require_explicit_reviews(self):
        # 300 mm length, 10 mm bands. Width varies by band, all endpoints included.
        p = np.array([[x,y,600] for x in range(301) for y in [-20-x/10,20+x/10]], float)
        rows, summary = measure_slices(p, 300, 10, 3)
        self.assertEqual(len(rows), 30)
        self.assertEqual(sum(r['points'] for r in rows), len(p))
        self.assertEqual([rows[i]['region'] for i in [9,10,19,20]], [0,1,1,2])
        self.assertIsNone(summary['regions'][0]['confirmed_max_visible_width_mm'])
        self.assertIsNone(summary['fixed_widths']['W25']['confirmed_width_mm'])
        self.assertNotEqual(summary['regions'][0]['candidate_max_visible_width_mm'],
                            summary['fixed_widths']['W25']['candidate_width_mm'])
        reviews = {str(i): 'usable' for i in range(30)}
        _, confirmed = measure_slices(p, 300, 10, 3, reviews)
        self.assertIsNotNone(confirmed['confirmed_envelope_area_mm2'])
        reviews['2'] = 'unusable'
        _, partial = measure_slices(p, 300, 10, 3, reviews)
        self.assertIsNone(partial['regions'][0]['confirmed_max_visible_width_mm'])
        self.assertIsNone(partial['confirmed_envelope_area_mm2'])
        self.assertIsNotNone(partial['regions'][1]['confirmed_max_visible_width_mm'])

    def test_thin_last_band_and_empty_band_are_not_invented(self):
        p = np.array([[0,-20,600], [0,20,600], [0,0,600], [305,0,600]])
        rows, summary = measure_slices(p, 305, 17, 3, {'1':'usable'})
        self.assertEqual(rows[-1]['x_end_mm'],305)
        self.assertIsNone(rows[1]['confirmed_width_mm'])
        self.assertEqual(rows[1]['status'],'pending')
        self.assertIsNone(summary['confirmed_envelope_area_mm2'])


class ProcessingWorkflowTest(unittest.TestCase):
    setUp = fixture.AlignmentWorkflowTest.setUp
    tearDown = fixture.AlignmentWorkflowTest.tearDown
    fit = fixture.AlignmentWorkflowTest.fit

    def context(self):
        response = self.ws.processing.context(self.base)
        self.proc = {**self.base, 'context_id': response['context_id'], 'revision': response['revision']}
        return response

    def select_some(self):
        response = self.ws.processing.select({**self.proc, 'action':'remove', 'coordinate':'camera',
            'view':{'rotation':np.eye(3).tolist(), 'center':[0,0,1200], 'scale':1, 'size':[2000,2000]},
            'rectangle':[0,0,999,2000]})
        self.assertGreater(response['candidate_count'], 0)
        self.assertLess(response['candidate_count'], response['total'])
        return response

    def apply_selection(self, selected):
        response = self.ws.processing.edit({**self.proc, 'operation':'apply', 'candidate_id':selected['candidate_id']})
        self.proc['revision'] = response['revision']
        return response

    def test_continuous_rectangles_accumulate_unique_indices_across_views_and_undo(self):
        self.context()
        with np.load(self.folder/'points.npz') as cloud:
            points = cloud['xyz_mm'].copy()
        first_view = {'rotation': np.eye(3).tolist(), 'center': [0,0,1200], 'scale': 1, 'size': [2000,2000]}
        second_view = {**first_view, 'rotation': [[0,1,0],[-1,0,0],[0,0,1]]}
        rectangles = [(first_view, [950,970,1000,1030]), (second_view, [970,970,1030,1000])]
        candidate, expected = '', np.zeros(len(points), bool)
        for index, (view, rectangle) in enumerate(rectangles + rectangles[:1]):
            expected |= screen_selection(points, view, rectangle)
            selected = self.ws.processing.select({**self.proc, 'action': 'remove', 'append': True,
                'candidate_id': candidate, 'view': view, 'rectangle': rectangle})
            candidate = selected['candidate_id']
            self.assertEqual(selected['candidate_count'], int(expected.sum()))
            self.assertEqual(selected['candidate_detail']['rectangle_count'], index+1)
        removed = self.apply_selection(selected)
        self.assertEqual(removed['removed_count'], int(expected.sum()))
        self.assertEqual(removed['candidate_count'], 0)
        # A new round starts from an empty candidate and excludes deleted points.
        more = self.ws.processing.select({**self.proc, 'action': 'remove', 'append': True, 'candidate_id': '',
            'view': first_view, 'rectangle': [1000,1000,1040,1030]})
        expected_more = screen_selection(points, first_view, [1000,1000,1040,1030]) & ~expected
        self.assertEqual(more['candidate_count'], int(expected_more.sum()))
        self.assertGreater(more['candidate_count'], 0)
        after = self.apply_selection(more)
        self.assertEqual(after['removed_count'], int((expected | expected_more).sum()))
        undone = self.ws.processing.edit({**self.proc, 'operation': 'undo'})
        self.proc['revision'] = undone['revision']
        self.assertEqual(undone['removed_count'], int(expected.sum()))
        restored = self.ws.processing.select({**self.proc, 'action': 'restore', 'append': True, 'candidate_id': '',
            'view': first_view, 'rectangle': [950,970,1000,1030]})
        restored_again = self.ws.processing.select({**self.proc, 'action': 'restore', 'append': True,
            'candidate_id': restored['candidate_id'], 'view': second_view, 'rectangle': [970,970,1030,1000]})
        self.assertEqual(restored_again['candidate_count'], int(expected.sum()))
        self.assertEqual(self.apply_selection(restored_again)['removed_count'], 0)

    def test_continuous_selection_rejects_stale_ids_and_switching_action_replaces_candidate(self):
        self.context()
        selected = self.select_some()
        payload = {**self.proc, 'append': True, 'candidate_id': selected['candidate_id'], 'action': 'restore',
            'view': {'rotation': np.eye(3).tolist(), 'center': [0,0,1200], 'scale': 1, 'size': [2000,2000]},
            'rectangle': [0,0,999,2000]}
        with self.assertRaisesRegex(ValueError, '选区已变化'):
            self.ws.processing.select({**payload, 'candidate_id': 'stale'})
        with self.assertRaisesRegex(ValueError, '连续框选'):
            self.ws.processing.select({**payload, 'append': 'true'})
        restored = self.ws.processing.select(payload)
        self.assertEqual(restored['candidate_count'], 0)
        self.assertEqual(restored['candidate_action'], 'restore')
        self.assertEqual(restored['candidate_detail']['rectangle_count'], 1)

    def test_edits_undo_redo_restore_and_restart_leave_originals_untouched(self):
        before = {p.name: sha256(p) for p in self.folder.iterdir() if p.is_file()}
        self.context()
        selected = self.select_some()
        response = self.apply_selection(selected)
        count = response['removed_count']
        self.assertEqual(count, selected['candidate_count'])
        self.assertTrue(self.ws.state()['frames'][0]['has_cleaning'])
        with self.assertRaises(ValueError):
            self.ws.processing.edit({**self.proc, 'revision':'stale', 'operation':'undo'})
        restored = Workspace(self.root/'state', initial_results=self.results)
        base = {'id':restored.state()['frames'][0]['id'], 'dataset_id':restored.dataset_id}
        fresh = restored.processing.context(base)
        self.assertEqual(fresh['removed_count'],count)
        fresh = restored.processing.edit({**base,'context_id':fresh['context_id'],'revision':fresh['revision'],'operation':'undo'})
        self.assertEqual(fresh['removed_count'],0)
        fresh = restored.processing.edit({**base,'context_id':fresh['context_id'],'revision':fresh['revision'],'operation':'redo'})
        self.assertEqual(fresh['removed_count'],count)
        fresh = restored.processing.edit({**base,'context_id':fresh['context_id'],'revision':fresh['revision'],'operation':'reset'})
        self.assertEqual(fresh['removed_count'],0)
        self.assertEqual(before,{p.name:sha256(p) for p in self.folder.iterdir() if p.is_file()})

    def test_failed_persistence_rolls_back_memory_and_keeps_previous_version(self):
        self.context()
        selected = self.select_some()
        with patch('pointcloud_workbench.workspace.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.apply_selection(selected)
        ctx = self.ws.processing.contexts[self.rel]
        self.assertEqual(ctx['removed'].sum(),0)
        self.assertEqual(ctx['cursor'],0)
        self.assertFalse(self.ws.state()['frames'][0]['has_cleaning'])
        self.assertFalse(list((self.root/'state'/'processing_cache').glob('*/pointcloud_cleaning.json')))

    def test_capture_screening_follows_point_set_and_export_uses_current_full_version(self):
        original = sha256(self.folder/'points.npz')
        self.ws.review_cloud({**self.base, 'decision': 'keep'})
        self.context()
        # Notes create a processing record without changing point membership.
        response = self.ws.processing.review({**self.proc, 'notes': '尚未删点'})
        self.proc['revision'] = response['revision']
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'], 'keep')
        edited = self.apply_selection(self.select_some())
        row = self.ws.row(self.base['id'])
        self.assertEqual(row['cloud_decision'], 'pending')
        self.assertTrue(row['cloud_review_stale'])
        self.assertEqual(row['current_ply'], 'cloud_cleaned_mm.ply')
        self.assertEqual(row['current_points'], edited['kept_count'])
        self.ws.start({**self.base, 'ids': [self.base['id']], 'parent': str(self.root), 'name': 'pending-current',
                       'decisions': ['pending'], 'group_by_decision': True}, 'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        output = self.root/'pending-current'
        dest = output/'待定'/self.rel
        selected = read_json(dest/'current_pointcloud.json')
        self.assertEqual(selected['current_npz'], 'cleaned_points.npz')
        self.assertEqual(selected['current_coordinates'], 'native_depth_camera')
        self.assertEqual(selected['current_points'], edited['kept_count'])
        self.assertFalse(selected['measurement_ready'])
        self.assertEqual(sha256(dest/'points.npz'), original)
        with np.load(dest/selected['current_npz']) as points:
            self.assertEqual(len(points['xyz_mm']), edited['kept_count'])
        review = read_json(dest/'cloud_review.json')
        self.assertEqual(review['decision'], 'pending')
        self.assertEqual(review['processing_version'], selected['processing_version'])
        self.assertIsNone(review['reviewed_at'])
        manifest = read_json(output/'pointcloud_dataset.json')['frames'][0]
        self.assertEqual(manifest['primary_ply'], selected['current_ply'])
        self.assertEqual(manifest['primary_npz'], selected['current_npz'])
        screening = read_json(output/'cloud_screening.json')['frames'][0]
        self.assertEqual(screening['current_ply'], selected['current_ply'])
        self.assertEqual(screening['current_points'], selected['current_points'])
        self.assertFalse(screening['measurement_ready'])
        reopened = Workspace(self.root/'portable-version', initial_results=output)
        self.assertEqual(reopened.state()['frames'][0]['cloud_decision'], 'pending')
        self.assertEqual(reopened.state()['frames'][0]['current_ply'], selected['current_ply'])

    def test_measurement_progress_and_aligned_export_are_separate_from_capture_keep(self):
        fitted = self.fit()
        self.ws.alignment.confirm({**self.payload, 'candidate_id': fitted['candidate_id'], 'alignment_confirmed': True})
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'], 'pending')
        self.ws.review_cloud({**self.base, 'decision': 'keep'})
        self.context()
        self.assertEqual(self.ws.row(self.base['id'])['measurement_slice_count'], 32)
        response = self.ws.processing.features({**self.proc, 'settings': {'count': 16, 'thickness_mm': 100, 'min_points': 3},
            'conditions': {'endpoints': 'complete'}})
        self.proc['revision'] = response['revision']
        rows = response['features']['profiles']
        self.assertTrue(all(row['width_q02_q98_mm'] is not None for row in rows))
        for row in rows:
            response = self.ws.processing.features({**self.proc, 'profile_index': row['index'], 'status': 'usable'})
            self.proc['revision'] = response['revision']
        state = self.ws.row(self.base['id'])
        self.assertEqual(state['cloud_decision'], 'keep')
        self.assertTrue(state['measurement_ready'])
        self.assertEqual(state['measurement_usable_slices'], len(rows))
        self.ws.start({**self.base, 'ids': [self.base['id']], 'parent': str(self.root), 'name': 'aligned-current',
                       'aligned_only': True, 'decisions': ['keep'], 'group_by_decision': True}, 'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        output = self.root/'aligned-current'
        dest = output/'保留'/self.rel
        selected = read_json(dest/'current_pointcloud.json')
        self.assertEqual(selected['current_ply'], 'cloud_cleaned_body_mm.ply')
        self.assertEqual(selected['current_npz'], 'cleaned_body_points.npz')
        self.assertEqual(selected['current_coordinates'], 'ground_body')
        self.assertTrue(selected['measurement_ready'])
        self.assertTrue(read_json(output/'cloud_screening.json')['frames'][0]['measurement_ready'])
        self.assertEqual(read_json(output/'pointcloud_dataset.json')['coordinate_system'], 'ground_body')
        # Rebuild summaries from profile labels on reopen without rewriting records.
        legacy = read_json(dest/'pointcloud_cleaning.json')
        legacy['measurement'].pop('review_summary')
        write_json(dest/'pointcloud_cleaning.json', legacy)
        legacy_bytes = (dest/'pointcloud_cleaning.json').read_bytes()
        reopened = Workspace(self.root/'portable-measured', initial_results=output)
        self.assertTrue(reopened.state()['frames'][0]['measurement_ready'])
        self.assertEqual(reopened.state()['frames'][0]['cloud_decision'], 'keep')
        self.assertEqual((dest/'pointcloud_cleaning.json').read_bytes(), legacy_bytes)
        self.apply_selection(self.select_some())
        self.assertFalse(self.ws.row(self.base['id'])['measurement_ready'])
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'], 'pending')

    def test_edited_source_and_tampered_history_cannot_be_reused(self):
        self.context(); self.apply_selection(self.select_some())
        saved = self.ws.results[self.rel]['processing']['folder']
        r = read_json(saved/'pointcloud_cleaning.json')
        r['cleaning_digest'] = 'bad'
        write_json(saved/'pointcloud_cleaning.json',r)
        reopened = Workspace(self.root/'state', initial_results=self.results)
        self.assertFalse(reopened.state()['frames'][0]['has_cleaning'])
        self.assertIn('清理记录', reopened.warning)

    def test_cleaning_before_alignment_refreshes_context_and_previews_only_kept_points(self):
        self.context()
        p = self.cloud['xyz_mm'][0]
        x,y = p[:2]+1000
        selected = self.ws.processing.select({**self.proc,'action':'remove','coordinate':'camera',
            'view':{'rotation':np.eye(3).tolist(),'center':[0,0,1200],'scale':1,'size':[2000,2000]},
            'rectangle':[float(x-.1),float(y-.1),float(x+.1),float(y+.1)]})
        self.assertEqual(selected['candidate_count'],1)
        self.apply_selection(selected)
        with self.assertRaisesRegex(ValueError,'清理版本'):
            self.fit()
        context = self.ws.alignment.context(self.base)
        self.assertEqual(context['cleaning_removed_count'],1)
        payload = {**self.payload,'context_id':context['context_id']}
        fit = self.ws.alignment.fit(payload)
        preview = self.ws.alignment.preview({**payload,'candidate_id':fit['candidate_id']})
        data = json.loads(preview.split('const DATA=',1)[1].split(', r=DATA.record',1)[0])
        self.assertEqual(data['count'],len(self.cloud['xyz_mm'])-1)
        self.assertTrue(data['record']['display_filtered'])
        self.ws.alignment.confirm({**payload,'candidate_id':fit['candidate_id'],'alignment_confirmed':True})
        folder = self.ws.results[self.rel]['folder']
        with np.load(folder/'body_points.npz') as original,np.load(folder/'cleaned_body_points.npz') as cleaned:
            self.assertEqual(len(original['xyz_mm']),len(self.cloud['xyz_mm']))
            self.assertEqual(len(cleaned['xyz_mm']),len(self.cloud['xyz_mm'])-1)

    def test_new_alignment_reference_invalidates_previous_slice_reviews(self):
        fit = self.fit()
        self.ws.alignment.confirm({**self.payload,'candidate_id':fit['candidate_id'],'alignment_confirmed':True})
        self.context()
        rows = self.ws.processing.slices(self.proc)['rows']
        i = next(i for i,r in enumerate(rows) if r['point_support_sufficient'])
        response = self.ws.processing.review({**self.proc,'slice_index':i,'status':'usable'})
        self.proc['revision']=response['revision']
        self.ws.review_cloud({**self.base, 'decision': 'keep'})
        context=self.ws.alignment.context(self.base)
        payload={**self.payload,'context_id':context['context_id'],'neck_uv':[69,40]}
        fit=self.ws.alignment.fit(payload)
        self.ws.alignment.confirm({**payload,'candidate_id':fit['candidate_id'],'alignment_confirmed':True})
        self.assertEqual(self.ws.processing.slices({**self.proc,'slice_index':i})['current']['status'],'pending')
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'], 'pending')
        self.assertTrue(self.ws.row(self.base['id'])['cloud_review_stale'])
        self.assertEqual(self.ws.row(self.base['id'])['measurement_usable_slices'], 0)

    def test_cleaning_and_alignment_reuse_same_reference_and_invalidate_slice_reviews(self):
        fit = self.fit()
        self.ws.alignment.confirm({**self.payload,'candidate_id':fit['candidate_id'],'alignment_confirmed':True})
        original_reference = read_json(self.ws.results[self.rel]['folder']/'alignment.json')['reference_id']
        self.context()
        slices = self.ws.processing.slices(self.proc)
        i = next(i for i,r in enumerate(slices['rows']) if r['point_support_sufficient'])
        reviewed = self.ws.processing.review({**self.proc,'slice_index':i,'status':'usable'})
        self.proc['revision'] = reviewed['revision']
        self.assertEqual(self.ws.processing.slices({**self.proc,'slice_index':i})['current']['status'],'usable')
        self.apply_selection(self.select_some())
        self.assertEqual(self.ws.processing.slices({**self.proc,'slice_index':i})['current']['status'],'pending')
        self.assertEqual(read_json(self.ws.results[self.rel]['folder']/'alignment.json')['reference_id'],original_reference)
        result = self.ws.results[self.rel]
        dest = self.root/'derived'
        import shutil
        shutil.copytree(result['folder'],dest)
        self.ws.processing.export_to(dest,result)
        with np.load(dest/'cleaned_points.npz') as camera, np.load(dest/'cleaned_body_points.npz') as body:
            np.testing.assert_array_equal(camera['source_point_index'],body['source_point_index'])
            m = np.asarray(read_json(dest/'alignment.json')['frame']['camera_to_body_4x4'])
            np.testing.assert_allclose(body['xyz_mm'],camera['xyz_mm'] @ m[:3,:3].T+m[:3,3],atol=1e-4)
        self.assertIn(b'coordinates ground_body', (dest/'cloud_cleaned_body_mm.ply').read_bytes()[:150])
        summary = read_json(dest/'measurement_summary.json')
        self.assertFalse(summary['measurement_ready'])
        self.assertEqual(summary['measurement_method'], 'local_normal_profiles')
        self.assertEqual(summary['slice_count'], 32)
        self.assertFalse((dest/'slice_measurements.csv').exists())
        self.assertTrue((dest/'profile_sequence.csv').is_file())
        page = (dest/'processing_preview.html').read_text()
        self.assertIn('OFFLINE_DATA=',page)
        self.assertNotIn('src="/processing.js"',page)

    def test_portable_export_reopens_with_history_and_preserves_all_point_attributes(self):
        original = sha256(self.folder/'points.npz')
        self.context(); response = self.apply_selection(self.select_some())
        self.ws.start({**self.base,'ids':[self.base['id']],'parent':str(self.root),'name':'exported'},'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        dest = self.root/'exported'/self.rel
        self.assertEqual(sha256(dest/'points.npz'),original)
        with np.load(dest/'cleaned_points.npz') as cleaned:
            ids = cleaned['source_point_index']
            self.assertEqual(len(ids),response['kept_count'])
            for key,value in self.cloud.items(): np.testing.assert_array_equal(cleaned[key],value[ids])
        reopened = Workspace(self.root/'fresh-state',initial_results=self.root/'exported')
        state = reopened.state()
        self.assertTrue(state['frames'][0]['has_cleaning'])
        fresh = reopened.processing.context({'dataset_id':state['dataset_id'],'id':state['frames'][0]['id']})
        self.assertEqual(fresh['removed_count'],response['removed_count'])
        self.assertTrue(fresh['can_undo'])

    def test_http_contract_requires_token_and_exposes_processing_assets(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.ws,'secret'))
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            for file in ('processing','processing.js','processing.css'):
                self.assertEqual(urlopen(url+'/'+file).status,200)
            body = json.dumps(self.base).encode()
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url+'/api/processing/context',data=body,headers={'Content-Type':'application/json'}))
            self.assertEqual(error.exception.code,403)
            response = json.load(urlopen(Request(url+'/api/processing/context',data=body,
                headers={'Content-Type':'application/json','X-App-Token':'secret'})))
            self.assertEqual(response['total'],len(self.cloud['xyz_mm']))
            self.assertEqual(len(response['indices']),response['total'])
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__ == '__main__':
    unittest.main()
