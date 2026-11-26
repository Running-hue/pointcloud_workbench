"""Finalize only pending measurement profiles through the stage keep action."""
import copy
import csv
import unittest
from unittest.mock import patch

from pointcloud_workbench import features
from pointcloud_workbench.core import np, read_json, sha256
from pointcloud_workbench.workspace import Workspace, atomic_json
from pointcloud_workbench.workflow import stage_version
from pointcloud_workbench.tests import test_alignment as fixture


class ReviewFinalizationTest(unittest.TestCase):
    tearDown = fixture.AlignmentWorkflowTest.tearDown

    def setUp(self):
        fixture.AlignmentWorkflowTest.setUp(self)
        fitted = self.ws.alignment.fit(self.payload)
        self.ws.alignment.confirm({**self.payload, 'candidate_id': fitted['candidate_id'],
                                   'alignment_confirmed': True})
        context = self.ws.processing.context(self.base)
        self.proc = {**self.base, 'context_id': context['context_id'], 'revision': context['revision']}
        configured = self.ws.processing.features({**self.proc,
            'settings': {'count': 32, 'thickness_mm': 100, 'min_points': 3}})
        self.proc['revision'] = configured['revision']
        self.assertEqual(len(configured['features']['profiles']), 32)
        self.assertTrue(all(row['width_q02_q98_mm'] is not None
                            for row in configured['features']['profiles']))

    def profiles(self, first=None, last=None, status=None):
        context = self.ws.processing.contexts.get(self.rel)
        if context is None or context['id'] != self.proc['context_id']:
            opened = self.ws.processing.context(self.base)
            self.proc.update(context_id=opened['context_id'], revision=opened['revision'])
        self.proc['revision'] = self.ws.results[self.rel]['processing']['record']['revision']
        payload = dict(self.proc)
        if status is not None:
            payload.update(profile_range=[first, last], status=status)
        response = self.ws.processing.features(payload)
        self.proc['revision'] = response['revision']
        return response

    def mark(self, stage, decision, version=None):
        payload = {**self.base, 'stage': stage, 'decision': decision}
        if version is not None:
            payload['stage_version'] = version
        return self.ws.review_cloud(payload)

    def statuses(self):
        return [row['status'] for row in self.profiles()['features']['profiles']]

    @staticmethod
    def normalized(value):
        if isinstance(value, np.ndarray):
            return {'dtype': str(value.dtype), 'shape': value.shape, 'data': value.tolist()}
        if isinstance(value, dict):
            return {key: ReviewFinalizationTest.normalized(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [ReviewFinalizationTest.normalized(item) for item in value]
        return value

    def snapshot(self):
        """Observe durable files plus all live structures involved in the commit."""
        return {'settings': copy.deepcopy(self.ws.settings),
            'results': copy.deepcopy(self.ws.results),
            'row': copy.deepcopy(self.ws.row(self.base['id'])),
            'contexts': self.normalized(self.ws.processing.contexts),
            'files': {path.relative_to(self.ws.state_dir).as_posix(): sha256(path)
                      for path in self.ws.state_dir.rglob('*') if path.is_file()},
            'paths': sorted(path.relative_to(self.ws.state_dir).as_posix()
                            for path in self.ws.state_dir.rglob('*'))}

    def assert_final_version(self):
        result = self.ws.results[self.rel]
        state = self.ws.row(self.base['id'])['workflow_stages']['review']
        expected = stage_version(result, 'review')
        self.assertEqual(state['decision'], 'keep')
        self.assertFalse(state['stale'])
        self.assertEqual(state['version'], expected)
        self.assertEqual(result['cloud_review']['stages']['review']['version'], expected)
        settings = read_json(self.ws.state_dir/'settings.json')
        saved = settings['cloud_reviews'][str(self.ws.results_root)][self.rel]['stages']['review']
        self.assertEqual(saved['version'], expected)
        self.assertEqual(saved['decision'], 'keep')

    def test_confirming_first_31_profiles_rejects_last_and_repeated_keep_is_idempotent(self):
        selected = self.profiles(0, 30, 'usable')
        self.assertEqual(selected['review_update']['updated'], 31)
        self.assertEqual(selected['review_update']['skipped'], [])
        self.assertEqual(self.statuses(), ['usable']*31+['pending'])
        previous = copy.deepcopy(self.ws.row(self.base['id'])['workflow_stages']['review']['version'])
        revision = self.proc['revision']
        source_hash = sha256(self.ws.results[self.rel]['folder']/'points.npz')

        with patch('pointcloud_workbench.workspace.atomic_json', wraps=atomic_json) as commit:
            response = self.mark('review', 'keep', previous)
        self.assertEqual(commit.call_count, 1)

        self.assertEqual(response['measurement_finalization'], {'auto_rejected_profiles': 1})
        with self.assertRaisesRegex(ValueError, '过期|变化'):
            self.ws.processing.features({**self.proc, 'profile_index': 31, 'status': 'usable'})
        self.assertEqual(self.statuses(), ['usable']*31+['unusable'])
        self.assertNotEqual(self.proc['revision'], revision)
        self.assertNotEqual(stage_version(self.ws.results[self.rel], 'review'), previous)
        row = self.ws.row(self.base['id'])
        self.assertEqual(row['measurement_usable_slices'], 31)
        self.assertEqual(row['measurement_unusable_slices'], 1)
        self.assertEqual(row['measurement_pending_slices'], 0)
        self.assertFalse(row['measurement_ready'])
        self.assert_final_version()

        finalized_revision = self.proc['revision']
        cache_before = self.snapshot()['files']
        again = self.mark('review', 'keep', stage_version(self.ws.results[self.rel], 'review'))

        self.assertEqual(again['measurement_finalization'], {'auto_rejected_profiles': 0})
        self.assertEqual(self.ws.results[self.rel]['processing']['record']['revision'], finalized_revision)
        self.assertEqual(self.ws.processing.contexts[self.rel]['revision'], finalized_revision)
        self.assertEqual(self.statuses(), ['usable']*31+['unusable'])
        cache_after = self.snapshot()['files']
        self.assertEqual({key: value for key, value in cache_before.items() if key.startswith('processing_cache/')},
                         {key: value for key, value in cache_after.items() if key.startswith('processing_cache/')})
        self.assertEqual(sha256(self.ws.results[self.rel]['folder']/'points.npz'), source_hash)
        self.assert_final_version()

    def test_fully_confirmed_profiles_keep_readiness_without_new_processing_revision(self):
        confirmed = self.profiles(0, 31, 'usable')
        self.assertEqual(confirmed['review_update']['updated'], 32)
        self.assertTrue(self.ws.row(self.base['id'])['measurement_ready'])
        revision = self.proc['revision']
        processing = copy.deepcopy(self.ws.results[self.rel]['processing'])
        response = self.mark('review', 'keep')
        self.assertEqual(response['measurement_finalization'], {'auto_rejected_profiles': 0})
        self.assertEqual(self.ws.results[self.rel]['processing'], processing)
        self.assertEqual(self.ws.processing.contexts[self.rel]['revision'], revision)
        self.assertEqual(self.statuses(), ['usable']*32)
        self.assertTrue(self.ws.row(self.base['id'])['measurement_ready'])
        self.assertEqual(self.ws.row(self.base['id'])['measurement_unusable_slices'], 0)
        self.assertEqual(self.ws.row(self.base['id'])['measurement_pending_slices'], 0)
        self.assert_final_version()

    def test_mixed_review_labels_survive_reload_and_split_and_merged_export_masks(self):
        self.profiles(0, 9, 'usable')
        self.profiles(10, 14, 'unusable')
        response = self.mark('review', 'keep')
        self.assertEqual(response['measurement_finalization'], {'auto_rejected_profiles': 17})
        expected_statuses = ['usable']*10+['unusable']*22
        self.assertEqual(self.statuses(), expected_statuses)
        self.assert_final_version()

        restored = Workspace(self.ws.state_dir, initial_results=self.results)
        self.assertEqual(restored.warning, '')
        restored_base = {**self.base, 'dataset_id': restored.dataset_id}
        ctx = restored.processing.context(restored_base)
        restored_profiles = restored.processing.features({**restored_base,
            'context_id': ctx['context_id'], 'revision': ctx['revision']})['features']
        self.assertEqual([row['status'] for row in restored_profiles['profiles']], expected_statuses)
        self.assertEqual(restored.row(self.base['id'])['measurement_usable_slices'], 10)
        self.assertEqual(restored.row(self.base['id'])['measurement_unusable_slices'], 22)
        self.assertEqual(restored.row(self.base['id'])['measurement_pending_slices'], 0)
        self.assertFalse(restored.row(self.base['id'])['measurement_ready'])
        self.assertFalse(restored.row(self.base['id'])['workflow_stages']['review']['stale'])
        expected_arrays = features.arrays(restored_profiles)
        self.assertTrue(expected_arrays['valid_mask'][10:, 0].all())
        self.assertFalse(expected_arrays['usable_mask'][10:].any())
        self.assertFalse(expected_arrays['reviewed_mask'][10:].any())
        self.assertTrue(expected_arrays['reviewed_mask'][:10, 0].all())

        for layout in ('split', 'merged'):
            with self.subTest(layout=layout):
                output = self.root/('finalized-'+layout)
                restored.start({**restored_base, 'ids': [self.base['id']], 'parent': str(self.root),
                    'name': output.name, 'stages': ['review'], 'decisions': ['keep'],
                    'export_layout': layout, 'group_by_decision': False}, 'export')
                restored.worker.join(20)
                self.assertEqual(restored.job['status'], 'completed', restored.job)
                manifest = read_json(output/'pointcloud_dataset.json')
                dest = output/manifest['frames'][0]['storage_relative_dir']
                self.assertEqual(read_json(dest/'geometry_features.json'), restored_profiles)
                summary = read_json(dest/'measurement_summary.json')
                self.assertEqual((summary['usable_slices'], summary['unusable_slices']), (10, 22))
                self.assertFalse(summary['measurement_ready'])
                self.assertEqual(read_json(dest/'workflow_stages.json')['stages']['review']['decision'], 'keep')
                with np.load(dest/'profile_sequence.npz', allow_pickle=False) as saved:
                    for name in ('values', 'valid_mask', 'usable_mask', 'reviewed_mask'):
                        np.testing.assert_array_equal(saved[name], expected_arrays[name])
                with (dest/'profile_sequence.csv').open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([row['status'] for row in rows], expected_statuses)
                for index, row in enumerate(rows):
                    for column, name in enumerate(features.CHANNELS):
                        for kind in ('valid', 'usable', 'reviewed'):
                            self.assertEqual(int(row[kind+'_'+name]), int(expected_arrays[kind+'_mask'][index, column]))

    def test_keep_with_no_usable_profiles_rejects_pending_without_claiming_measurement_ready(self):
        self.assertEqual(self.statuses(), ['pending']*32)
        response = self.mark('review', 'keep')
        self.assertEqual(response['measurement_finalization'], {'auto_rejected_profiles': 32})
        self.assertEqual(self.statuses(), ['unusable']*32)
        row = self.ws.row(self.base['id'])
        self.assertFalse(row['measurement_ready'])
        self.assertEqual(row['measurement_usable_slices'], 0)
        self.assertEqual(row['measurement_unusable_slices'], 32)
        self.assertEqual(row['measurement_pending_slices'], 0)
        summary = self.ws.results[self.rel]['processing']['record']['measurement']['review_summary']
        self.assertEqual(summary['unusable_slices'], 32)
        self.assertFalse(summary['measurement_ready'])
        arrays = features.arrays(self.profiles()['features'])
        self.assertFalse(arrays['usable_mask'].any())
        self.assertFalse(arrays['reviewed_mask'].any())
        self.assert_final_version()

    def test_other_stage_decisions_and_review_pending_or_reject_preserve_profiles(self):
        self.profiles(0, 2, 'usable')
        self.profiles(3, 4, 'unusable')
        expected = self.statuses()
        record = copy.deepcopy(self.ws.results[self.rel]['processing'])
        context = self.normalized(self.ws.processing.contexts)
        for stage, decision in (('cleaning', 'pending'), ('cleaning', 'reject'), ('cleaning', 'keep'),
                                ('alignment', 'pending'), ('alignment', 'reject'), ('alignment', 'keep'),
                                ('review', 'pending'), ('review', 'reject')):
            with self.subTest(stage=stage, decision=decision):
                response = self.mark(stage, decision)
                self.assertNotIn('measurement_finalization', response)
                self.assertEqual(self.ws.results[self.rel]['processing'], record)
                self.assertEqual(self.normalized(self.ws.processing.contexts), context)
        self.assertEqual(self.statuses(), expected)

    def test_stale_review_version_is_rejected_before_any_profile_or_settings_change(self):
        previous = copy.deepcopy(stage_version(self.ws.results[self.rel], 'review'))
        self.profiles(0, 9, 'usable')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '变化'):
            self.mark('review', 'keep', previous)
        self.assertEqual(self.snapshot(), before)

    def test_settings_write_failure_leaves_no_partial_processing_or_stage_commit(self):
        self.profiles(0, 9, 'usable')
        self.profiles(10, 14, 'unusable')
        self.mark('review', 'pending')
        before = self.snapshot()

        def fail_final_settings(path, data):
            review = data.get('cloud_reviews', {}).get(str(self.ws.results_root), {}).get(self.rel, {})
            if review.get('stages', {}).get('review', {}).get('decision') == 'keep':
                raise OSError('synthetic final settings failure')
            return atomic_json(path, data)

        for failure in (OSError('synthetic settings failure'), fail_final_settings):
            with self.subTest(failure=str(failure)):
                with patch('pointcloud_workbench.workspace.atomic_json', side_effect=failure) as commit:
                    with self.assertRaisesRegex(OSError, 'settings failure'):
                        self.mark('review', 'keep', before['row']['workflow_stages']['review']['version'])
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(self.snapshot(), before)
                restored = Workspace(self.ws.state_dir, initial_results=self.results)
                self.assertEqual(restored.settings, before['settings'])
                self.assertEqual(restored.row(self.base['id'])['workflow_stages']['review']['decision'], 'pending')
                self.assertEqual(restored.row(self.base['id'])['processing_revision'],
                                 before['row']['processing_revision'])


if __name__ == '__main__':
    unittest.main()
