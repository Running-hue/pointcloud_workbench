"""Stage screening, partial exports and measurement packages on synthetic data."""
import base64
import csv
import io
import unittest
from unittest.mock import patch

from pointcloud_workbench.core import np, read_json, sha256, write_json
from pointcloud_workbench.alignment import transform_points
from pointcloud_workbench.features import GEOMETRY_FIELDS
from pointcloud_workbench.workspace import Workspace
from pointcloud_workbench.workflow import STAGES
from pointcloud_workbench.tests import test_alignment as alignment_fixture
from pointcloud_workbench.tests import test_processing as processing_fixture


class StageExportTest(unittest.TestCase):
    setUp = alignment_fixture.AlignmentWorkflowTest.setUp
    tearDown = alignment_fixture.AlignmentWorkflowTest.tearDown
    context = processing_fixture.ProcessingWorkflowTest.context
    select_some = processing_fixture.ProcessingWorkflowTest.select_some
    apply_selection = processing_fixture.ProcessingWorkflowTest.apply_selection

    def mark(self, stage, decision):
        return self.ws.review_cloud({**self.base, 'stage': stage, 'decision': decision})

    def stages(self):
        return self.ws.row(self.base['id'])['workflow_stages']

    def align(self):
        context = self.ws.alignment.context(self.base)
        self.payload['context_id'] = context['context_id']
        fitted = self.ws.alignment.fit(self.payload)
        self.ws.alignment.confirm({**self.payload, 'candidate_id': fitted['candidate_id'], 'alignment_confirmed': True})

    def measure(self):
        self.context()
        reply = self.ws.processing.features({**self.proc, 'settings': {'count': 16, 'thickness_mm': 100, 'min_points': 3},
            'conditions': {'endpoints': 'complete'}})
        self.proc['revision'] = reply['revision']
        index = next(i for i, row in enumerate(reply['features']['profiles']) if row['width_q02_q98_mm'] is not None)
        reply = self.ws.processing.features({**self.proc, 'profile_index': index, 'status': 'usable'})
        self.proc['revision'] = reply['revision']
        return reply

    def export(self, name, stages=STAGES, decisions=('keep', 'pending', 'reject'), **options):
        self.ws.start({**self.base, 'ids': [self.base['id']], 'parent': str(self.root), 'name': name,
            'stages': list(stages), 'decisions': list(decisions), 'group_by_decision': True, **options}, 'export')
        self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        output = self.root/name
        frame = read_json(output/'pointcloud_dataset.json')['frames'][0]
        return output, output/frame['storage_relative_dir']

    def test_split_exports_only_stage_products_and_preserves_current_data(self):
        self.align()
        self.context()
        self.apply_selection(self.select_some())
        self.mark('cleaning', 'keep')
        self.mark('alignment', 'keep')
        computed = self.measure()['features']
        self.mark('review', 'pending')
        source = self.ws.results[self.rel]['folder']
        before = {p.name: sha256(p) for p in source.iterdir() if p.is_file()}
        output, latest = self.export('split', export_layout='split')
        manifest = read_json(output/'pointcloud_dataset.json')
        self.assertEqual(len(manifest['frames']), 1)
        self.assertEqual(manifest['export_layout'], 'split')
        self.assertEqual(manifest['package_kind'], 'stage_products')
        self.assertFalse(manifest['editable'])
        expected = {'cleaning': '点云清理/保留/', 'alignment': '坐标对齐/保留/', 'review': '测量复核/待定/'}
        records = read_json(output/'stage_screening.json')['frames']
        self.assertEqual([r['storage_relative_dir'] for r in records], [prefix+self.rel for prefix in expected.values()])
        stage_files = {'cleaning': {'cloud_cleaned_mm.ply', 'cleaned_points.npz', 'pointcloud_cleaning.json'},
            'alignment': {'cloud_body_mm.ply', 'body_points.npz', 'alignment.json'},
            'review': {'geometry_features.json', 'profile_sequence.csv', 'profile_sequence.npz', 'measurement_summary.json'}}
        for key, prefix in expected.items():
            dest = output/(prefix+self.rel)
            self.assertEqual({p.name for p in dest.iterdir()}, stage_files[key] | {'stage_result.json', 'workflow_stages.json'})
            self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), [key])
            metadata = read_json(dest/'stage_result.json')
            self.assertEqual(metadata['stage'], key)
            self.assertEqual(metadata['decision'], 'pending' if key == 'review' else 'keep')
            self.assertEqual(metadata['source_sha256']['points'], before['points.npz'])
            self.assertEqual(metadata['source_sha256']['report'], before['pointcloud.json'])
            child_root = output/prefix.split('/')[0]
            child = read_json(child_root/'pointcloud_dataset.json')['frames'][0]
            self.assertEqual(child['has_cloud'], key != 'review')
            self.assertEqual(bool(child['primary_ply']), key != 'review')
            self.assertEqual((child_root/'geometry_features.csv').is_file(), key == 'review')
            for record in read_json(child_root/'stage_screening.json')['frames']:
                for name in record['files'].values():
                    self.assertTrue((child_root/record['storage_relative_dir']/name).is_file())
            with self.assertRaisesRegex(ValueError, '精简成果包'):
                self.ws.open_results(str(child_root))
        self.assertFalse((output/'geometry_features.csv').exists())
        with (output/'测量复核'/'geometry_features.csv').open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['feature_status'], 'computed')
        keep = self.ws.processing.keep_mask(self.ws.results[self.rel])
        cleaned = output/(expected['cleaning']+self.rel)
        aligned = output/(expected['alignment']+self.rel)
        with np.load(source/'points.npz') as original, np.load(cleaned/'cleaned_points.npz') as camera, np.load(aligned/'body_points.npz') as body:
            self.assertEqual(set(camera.files), set(original.files) | {'source_point_index'})
            np.testing.assert_array_equal(camera['source_point_index'], np.flatnonzero(keep))
            np.testing.assert_array_equal(body['source_point_index'], camera['source_point_index'])
            for name in original.files:
                np.testing.assert_array_equal(camera[name], original[name][keep])
                if name != 'xyz_mm':
                    np.testing.assert_array_equal(body[name], camera[name])
            frame = read_json(source/'alignment.json')['frame']
            np.testing.assert_allclose(body['xyz_mm'], transform_points(camera['xyz_mm'], frame), atol=1e-4)
        summary = read_json(cleaned/'pointcloud_cleaning.json')
        self.assertNotIn('measurement', summary)
        self.assertNotIn('operations', summary)
        self.assertEqual(summary['kept_points'], int(keep.sum()))
        self.assertEqual(read_json(aligned/'alignment.json')['body_points_sha256'], sha256(aligned/'body_points.npz'))
        self.assertEqual(read_json(latest/'geometry_features.json'), computed)
        self.assertTrue((latest/'profile_sequence.csv').is_file())
        self.assertTrue((latest/'profile_sequence.npz').is_file())
        with self.assertRaisesRegex(ValueError, '精简成果包'):
            self.ws.open_results(str(output))
        merged, _ = self.export('complete-comparison')
        self.assertLess(sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
                        sum(p.stat().st_size for p in merged.rglob('*') if p.is_file()))
        self.assertEqual(before, {p.name: sha256(p) for p in source.iterdir() if p.is_file()})

    def test_split_early_rejection_and_stage_subset(self):
        self.mark('cleaning', 'reject')
        output, dest = self.export('split-reject', export_layout='split')
        self.assertEqual(dest, output/'点云清理/淘汰'/self.rel)
        self.assertFalse((output/'坐标对齐').exists())
        self.assertFalse((output/'测量复核').exists())
        self.assertEqual(len(list(output.rglob('stage_result.json'))), 1)
        self.assertFalse(list(output.rglob('pointcloud.json')))
        self.assertEqual([r['exported'] for r in read_json(output/'stage_screening.json')['frames']], [True, False, False])
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'reject')
        output, dest = self.export('split-later-reject', export_layout='split')
        self.assertEqual(dest, output/'坐标对齐/淘汰'/self.rel)
        self.assertTrue((output/'点云清理/保留'/self.rel/'cleaned_points.npz').is_file())
        self.assertEqual(len(list(output.rglob('stage_result.json'))), 2)
        output, dest = self.export('split-subset', ['cleaning'], export_layout='split', group_by_decision=False)
        self.assertEqual(dest, output/'点云清理'/self.rel)
        self.assertFalse((output/'坐标对齐').exists())
        self.assertEqual(read_json(dest/'workflow_stages.json')['stages']['cleaning']['decision'], 'keep')

    def test_invalid_export_layout_is_rejected_before_job_starts(self):
        for layout in ('unknown', {}, [], None):
            with self.assertRaisesRegex(ValueError, '拆分或合并'):
                self.export('invalid-layout', export_layout=layout)
        self.assertIsNone(self.ws.worker)

    def test_review_rejection_before_measurement_keeps_alignment_dependency(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'keep')
        self.mark('review', 'reject')
        for layout in ('merged', 'split'):
            output, dest = self.export('review-reject-'+layout, export_layout=layout)
            self.assertFalse((dest/'geometry_features.json').exists())
            if layout == 'split':
                self.assertEqual({p.name for p in dest.iterdir()}, {'stage_result.json', 'workflow_stages.json'})
                self.assertEqual(read_json(dest/'workflow_stages.json')['stages']['review']['files'], {})
                self.assertFalse((output/'测量复核'/'geometry_features.csv').exists())
                continue
            self.assertTrue((dest/'alignment.json').is_file())
            self.assertEqual(read_json(dest/'current_pointcloud.json')['current_coordinates'], 'ground_body')
            reopened = Workspace(self.root/('review-reject-reopen-'+layout), initial_results=output)
            states = reopened.state()['frames'][0]['workflow_stages']
            self.assertEqual([states[key]['decision'] for key in STAGES], ['keep', 'keep', 'reject'])
            self.assertFalse(any(states[key]['stale'] for key in STAGES))

    def test_split_failure_does_not_publish_partial_capture_or_change_source(self):
        self.align()
        source = self.ws.results[self.rel]['folder']
        before = {p.name: sha256(p) for p in source.iterdir() if p.is_file()}
        export_copy = self.ws._export_stage_products
        def fail_second(source, dest, row, plan):
            if 'alignment' in plan:
                raise OSError('synthetic copy failure')
            return export_copy(source, dest, row, plan)
        with patch.object(self.ws, '_export_stage_products', side_effect=fail_second):
            self.ws.start({**self.base, 'ids': [self.base['id']], 'parent': str(self.root), 'name': 'split-failure',
                'stages': list(STAGES), 'decisions': ['pending'], 'group_by_decision': True, 'export_layout': 'split'}, 'export')
            self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'], 'failed')
        self.assertIn('synthetic copy failure', self.ws.job['error'])
        self.assertFalse((self.root/'split-failure').exists())
        self.assertFalse(list(self.root.glob('.pointcloud-export-*')))
        self.assertEqual(before, {p.name: sha256(p) for p in source.iterdir() if p.is_file()})

    def test_split_single_alignment_and_review_have_no_upstream_products(self):
        self.align()
        self.measure()
        for key, point_name in (('alignment', 'body_points.npz'), ('review', 'profile_sequence.npz')):
            output, dest = self.export('split-only-'+key, [key], export_layout='split', group_by_decision=False)
            self.assertEqual({p.name for p in output.iterdir() if p.is_dir()},
                             {'坐标对齐' if key == 'alignment' else '测量复核'})
            self.assertTrue((dest/point_name).is_file())
            self.assertFalse((dest/'points.npz').exists())
            self.assertFalse((dest/'cleaned_points.npz').exists())
            self.assertEqual((dest/'body_points.npz').exists(), key == 'alignment')

    def test_split_stop_finishes_current_capture_without_partial_stages(self):
        self.align()
        self.measure()
        export_stage = self.ws._export_stage_products
        def stop_after_first(*args):
            result = export_stage(*args)
            self.ws.stop_event.set()
            return result
        with patch.object(self.ws, '_export_stage_products', side_effect=stop_after_first):
            self.ws.start({**self.base, 'ids': [self.base['id']], 'parent': str(self.root), 'name': 'split-stop',
                'stages': list(STAGES), 'decisions': ['pending'], 'group_by_decision': True, 'export_layout': 'split'}, 'export')
            self.ws.worker.join(20)
        self.assertEqual(self.ws.job['status'], 'stopped', self.ws.job)
        output = self.root/'split-stop'
        self.assertEqual(len(list(output.rglob('stage_result.json'))), 3)
        self.assertFalse(list(self.root.glob('.pointcloud-export-*')))
        rows = read_json(output/'stage_screening.json')['frames']
        self.assertTrue(all(row['exported'] for row in rows))

    def test_old_complete_split_packages_still_reopen(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'keep')
        self.measure()
        self.mark('review', 'pending')
        # Recreate the previous complete-copy layout, then remove only the new
        # delivery-package discriminator to match legacy manifests.
        with patch.object(self.ws, '_export_stage_products', side_effect=self.ws._export_copy):
            output, _ = self.export('legacy-split', export_layout='split')
        for path in output.rglob('pointcloud_dataset.json'):
            manifest = read_json(path)
            manifest.pop('package_kind', None)
            manifest.pop('editable', None)
            if path.parent != output:
                manifest['export_layout'] = 'merged'
            write_json(path, manifest)
        reopened = Workspace(self.root/'legacy-root-reopen', initial_results=output)
        row = reopened.state()['frames'][0]
        self.assertEqual(row['relative_dir'], self.rel)
        self.assertEqual([row['workflow_stages'][key]['decision'] for key in STAGES], ['keep', 'keep', 'pending'])
        self.assertEqual(row['measurement_usable_slices'], 1)
        for key, label in zip(STAGES, ('点云清理', '坐标对齐', '测量复核')):
            child = Workspace(self.root/('legacy-child-'+key), initial_results=output/label)
            self.assertEqual(child.state()['frames'][0]['workflow_stages'][key]['decision'],
                             'pending' if key == 'review' else 'keep')
        manifest = read_json(output/'pointcloud_dataset.json')
        manifest['frames'][0]['stage_storage_relative_dirs']['cleaning'] = '点云清理/../../escape'
        write_json(output/'pointcloud_dataset.json', manifest)
        with self.assertRaisesRegex(ValueError, '非法'):
            self.ws.open_results(str(output))

    def test_cleaning_without_other_steps_exports_and_reopens(self):
        original = sha256(self.folder/'points.npz')
        self.mark('cleaning', 'keep')
        output, dest = self.export('clean-only')
        stages = read_json(dest/'workflow_stages.json')['stages']
        self.assertEqual(list(stages), ['cleaning'])
        self.assertEqual(stages['cleaning']['decision'], 'keep')
        self.assertFalse((dest/'alignment.json').exists())
        self.assertFalse((dest/'geometry_features.json').exists())
        self.assertFalse((dest/'measurement_summary.json').exists())
        self.assertEqual(sha256(dest/'points.npz'), original)
        reopened = Workspace(self.root/'reopened', initial_results=output)
        self.assertEqual(reopened.state()['frames'][0]['workflow_stages']['cleaning']['decision'], 'keep')

    def test_alignment_preserves_cleaning_and_does_not_invent_measurement(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.assertEqual(self.stages()['cleaning']['decision'], 'keep')
        self.assertFalse(self.stages()['cleaning']['stale'])
        self.mark('alignment', 'keep')
        output, dest = self.export('two-stages')
        self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), ['cleaning', 'alignment'])
        self.assertTrue((dest/'body_points.npz').is_file())
        self.assertFalse((dest/'geometry_features.json').exists())
        self.assertFalse((dest/'measurement_summary.json').exists())
        rows = read_json(output/'stage_screening.json')['frames']
        self.assertEqual([r['exported'] for r in rows], [True, True, False])

    def test_cleaning_selection_omits_existing_alignment_and_measurement(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.measure()
        before = {str(p): sha256(p) for p in self.ws.results[self.rel]['folder'].iterdir() if p.is_file()}
        output, dest = self.export('only-cleaning', ['cleaning'])
        self.assertFalse((dest/'alignment.json').exists())
        self.assertFalse((dest/'geometry_features.json').exists())
        self.assertFalse((dest/'measurement_summary.json').exists())
        self.assertEqual(read_json(dest/'pointcloud_cleaning.json')['measurement'], {})
        self.assertEqual(read_json(dest/'current_pointcloud.json')['current_coordinates'], 'native_depth_camera')
        self.assertEqual(read_json(output/'cloud_screening.json')['frames'][0]['current_coordinates'], 'native_depth_camera')
        self.assertTrue(self.ws.row(self.base['id'])['has_alignment'])
        self.assertEqual(before, {str(p): sha256(p) for p in self.ws.results[self.rel]['folder'].iterdir() if p.is_file()})
        reopened = Workspace(self.root/'clean-reopen', initial_results=output)
        self.assertFalse(reopened.state()['frames'][0]['has_alignment'])

    def test_editing_after_alignment_does_not_start_measurement(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.context()
        self.apply_selection(self.select_some())
        self.assertFalse(self.stages()['review']['has_output'])
        _, dest = self.export('edited-aligned')
        self.assertFalse((dest/'geometry_features.json').exists())
        self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), ['cleaning', 'alignment'])

    def test_alignment_and_review_can_each_be_exported_alone(self):
        self.align()
        self.measure()
        for key in ('alignment', 'review'):
            output, dest = self.export('only-'+key, [key])
            self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), [key])
            self.assertTrue((dest/'alignment.json').is_file())
            self.assertEqual((dest/'geometry_features.json').is_file(), key == 'review')
            reopened = Workspace(self.root/('reopen-'+key), initial_results=output)
            self.assertTrue(reopened.state()['frames'][0]['has_alignment'])
            self.assertEqual(reopened.state()['frames'][0]['measurement_usable_slices'], 1 if key == 'review' else 0)

    def test_later_rejection_keeps_earlier_results_and_filters_each_stage(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'reject')
        self.assertEqual(self.stages()['review']['blocked_by'], 'alignment')
        with self.assertRaisesRegex(ValueError, '前序'):
            self.mark('review', 'keep')
        _, dest = self.export('keep-earlier', decisions=['keep'])
        self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), ['cleaning'])
        self.assertFalse((dest/'alignment.json').exists())
        output, dest = self.export('keep-rejected-stage')
        stages = read_json(dest/'workflow_stages.json')['stages']
        self.assertEqual([v['decision'] for v in stages.values()], ['keep', 'reject'])
        self.assertNotIn('review', stages)
        reopened = Workspace(self.root/'reject-reopen', initial_results=output)
        values = reopened.state()['frames'][0]['workflow_stages']
        self.assertEqual(values['cleaning']['decision'], 'keep')
        self.assertEqual(values['alignment']['decision'], 'reject')

    def test_upstream_rejection_blocks_actions_and_omits_all_downstream_files(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'keep')
        self.measure()
        self.mark('cleaning', 'reject')
        with self.assertRaisesRegex(ValueError, '淘汰'):
            self.ws.alignment.context(self.base)
        with self.assertRaisesRegex(ValueError, '淘汰'):
            self.ws.processing.features(self.proc)
        _, dest = self.export('rejected-cleaning')
        self.assertEqual(list(read_json(dest/'workflow_stages.json')['stages']), ['cleaning'])
        self.assertFalse((dest/'alignment.json').exists())
        self.assertFalse((dest/'geometry_features.json').exists())
        self.mark('cleaning', 'keep')
        self.assertEqual(self.stages()['alignment']['decision'], 'keep')
        self.assertFalse(self.stages()['review']['blocked_by'])

    def test_batch_measurement_matches_individual_json_csv_npz(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'keep')
        computed = self.measure()['features']
        self.mark('review', 'pending')
        csv_reply = self.ws.processing.features({**self.proc, 'format': 'csv'})
        npz_reply = self.ws.processing.features({**self.proc, 'format': 'npz'})
        output, dest = self.export('measured')
        self.assertEqual(read_json(dest/'geometry_features.json'), computed)
        # Every overall metric shown in the panel must accompany the sequence,
        # including missing values and review flags for partially reviewed data.
        geometry = read_json(dest/'geometry_features.json')['geometry']
        self.assertEqual(set(geometry), set(GEOMETRY_FIELDS))
        with (output/'geometry_features.csv').open(encoding='utf-8-sig') as stream:
            geometry_rows = list(csv.DictReader(stream))
        self.assertEqual(len(geometry_rows), 1)
        for key in GEOMETRY_FIELDS:
            metric = computed['geometry'][key]
            self.assertEqual(geometry[key], metric)
            value = geometry_rows[0][key]
            if metric['value'] is None:
                self.assertEqual(value, '')
            else:
                self.assertAlmostEqual(float(value), metric['value'])
            self.assertEqual(int(geometry_rows[0][key+'_valid']), int(metric['valid']))
            self.assertEqual(int(geometry_rows[0][key+'_reviewed']), int(metric['reviewed']))
        files = read_json(dest/'workflow_stages.json')['stages']['review']['files']
        self.assertEqual(set(files.values()), {'geometry_features.json', 'profile_sequence.csv',
            'profile_sequence.npz', 'measurement_summary.json'})
        self.assertEqual((dest/'profile_sequence.csv').read_bytes(), base64.b64decode(csv_reply['download']['base64']))
        with np.load(dest/'profile_sequence.npz') as batch, np.load(io.BytesIO(base64.b64decode(npz_reply['download']['base64']))) as single:
            self.assertEqual(batch.files, single.files)
            for key in batch.files:
                np.testing.assert_array_equal(batch[key], single[key])
        summary = read_json(dest/'measurement_summary.json')
        self.assertEqual(summary['usable_slices'], 1)
        self.assertFalse(summary['measurement_ready'])
        self.assertEqual(read_json(dest/'workflow_stages.json')['stages']['cleaning']['decision'], 'keep')
        with (output/'stage_screening.csv').open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r['decision'] for r in rows], ['keep', 'keep', 'pending'])
        reopened = Workspace(self.root/'measure-reopen', initial_results=output)
        self.assertEqual(reopened.state()['frames'][0]['measurement_usable_slices'], 1)

    def test_versions_invalidate_only_the_affected_stage(self):
        self.mark('cleaning', 'keep')
        self.align()
        self.mark('alignment', 'keep')
        self.measure()
        self.mark('review', 'keep')
        # Final keep closes the pending profiles and invalidates the old editor.
        # Reopen and change a finalized label to test version invalidation.
        self.context()
        reply = self.ws.processing.features({**self.proc, 'profile_index': 1, 'status': 'pending'})
        self.proc['revision'] = reply['revision']
        self.assertEqual(self.stages()['cleaning']['decision'], 'keep')
        self.assertEqual(self.stages()['alignment']['decision'], 'keep')
        self.assertEqual(self.stages()['review']['decision'], 'pending')
        self.assertTrue(self.stages()['review']['stale'])
        self.apply_selection(self.select_some())
        self.assertEqual(self.stages()['cleaning']['decision'], 'pending')
        self.assertTrue(self.stages()['alignment']['stale'])

    def test_invalid_stages_and_stale_screening_payloads_are_rejected(self):
        for stage in ('bogus', [], {}):
            with self.assertRaises(ValueError):
                self.mark(stage, 'keep')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.ws.review_cloud({**self.base, 'stage': 'cleaning', 'decision': 'keep', 'stage_version': {}})
        with self.assertRaisesRegex(ValueError, '完成本阶段'):
            self.mark('alignment', 'keep')
        for stages in ([], ['bogus'], ['cleaning', 'cleaning']):
            with self.assertRaisesRegex(ValueError, '导出阶段'):
                self.export('invalid', stages)


if __name__ == '__main__':
    unittest.main()
