from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from pointcloud_workbench.core import read_json, write_json, sha256
from pointcloud_workbench.tests import test_geometry, test_rgb
from pointcloud_workbench.workspace import Workspace


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_geometry.WorkflowTest()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.raw, self.seg = self.root/'raw', self.root/'masks'
        self.entries = []
        for name in ['good', 'missing_meta', 'bad_json', 'wrong_mode', 'missing_depth', 'manual']:
            rel = 'pig_test/'+name
            shutil.copytree(self.fixture.source, self.raw/rel)
            shutil.copytree(self.fixture.seg, self.seg/rel)
            self.entries.append({'relative_dir': rel, 'pig_id': 'test', 'capture_id': name})
        (self.seg/'pig_test/missing_meta/segmentation.json').unlink()
        (self.seg/'pig_test/bad_json/segmentation.json').write_text('{broken')
        meta = read_json(self.seg/'pig_test/wrong_mode/segmentation.json')
        meta['input_mode'] = 'rgb'
        write_json(self.seg/'pig_test/wrong_mode/segmentation.json', meta)
        (self.raw/'pig_test/missing_depth/depth_sensor_code_u16.npy').unlink()
        write_json(self.seg/'dataset.json', {'input_mode': 'infrared', 'frames': self.entries})
        self.ws = Workspace(self.root/'settings')
        self.reload()

    def tearDown(self):
        if self.ws.worker: self.ws.worker.join(10)
        self.fixture.tearDown()

    def reload(self):
        return self.ws.load({'source': str(self.raw), 'segmentation': str(self.seg)})

    def row(self, name):
        return next(r for r in self.ws.state()['frames'] if r['capture_id'] == name)

    def mark(self, names, excluded=True):
        return self.ws.mark_skipped({'dataset_id': self.ws.dataset_id,
                                    'ids': [self.row(n)['id'] for n in names], 'excluded': excluded})

    def payload(self, name='generated'):
        return {'dataset_id': self.ws.dataset_id, 'ids': [r['id'] for r in self.ws.rows],
                'parent': str(self.root), 'name': name, 'allow_draft': True}

    def test_missing_malformed_mismatched_frames_load_with_specific_reasons(self):
        state = self.ws.state()
        self.assertEqual(state['validation'], {'total': 6, 'passed': 2, 'failed': 4, 'excluded': 0})
        self.assertIn('缺少分割记录', self.row('missing_meta')['validation_errors'][0])
        self.assertNotIn('输入模式', ' '.join(self.row('missing_meta')['validation_errors']))
        self.assertIn('无法读取', self.row('bad_json')['validation_errors'][0])
        self.assertIn('输入模式不一致', self.row('wrong_mode')['validation_errors'][0])
        self.assertEqual(self.row('wrong_mode')['input_mode'], 'rgb')
        self.assertIn('depth_sensor_code_u16.npy', self.row('missing_depth')['missing'])

    def test_batch_skips_bad_and_marked_frames_and_preserves_sources(self):
        before = {str(p): sha256(p) for root in [self.raw, self.seg] for p in root.rglob('*') if p.is_file()}
        self.mark(['manual'])
        self.ws.start(self.payload(), 'generate')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        self.assertEqual(self.ws.job['success'], 1)
        self.assertEqual(len(self.ws.job['skipped']), 5)
        self.assertEqual(self.ws.job['errors'], [])
        self.assertEqual(self.ws.job['done'], 6)
        report = read_json(self.root/'generated/input_validation.json')
        self.assertEqual(report['summary']['failed'], 4)
        self.assertEqual(report['summary']['excluded'], 1)
        self.assertEqual(len(report['issues']), 5)
        self.assertTrue((self.root/'generated/pig_test/good/points.npz').is_file())
        self.assertEqual(len(list((self.root/'generated').rglob('points.npz'))), 1)
        self.assertEqual(before, {str(p): sha256(p) for root in [self.raw, self.seg]
                                  for p in root.rglob('*') if p.is_file()})

    def test_marks_survive_reload_restart_and_do_not_authorize_failed_input(self):
        self.mark(['manual', 'wrong_mode'])
        self.reload()
        self.assertTrue(self.row('manual')['excluded'])
        restored = Workspace(self.root/'settings')
        self.assertEqual(restored.state()['validation']['excluded'], 2)
        self.mark(['wrong_mode'], False)
        self.assertTrue(self.row('wrong_mode')['validation_errors'])
        with self.assertRaisesRegex(ValueError, '没有可生成项'):
            self.ws.start({**self.payload(), 'ids': [self.row('wrong_mode')['id']]}, 'generate')
        self.mark(['manual'], False)
        self.assertFalse(self.row('manual')['excluded'])
        other_seg = self.root/'other-masks'
        shutil.copytree(self.seg, other_seg)
        state = self.ws.load({'source': str(self.raw), 'segmentation': str(other_seg)})
        self.assertEqual(state['validation']['excluded'], 0)

    def test_stale_requests_and_marks_during_running_jobs_are_rejected(self):
        payload = {'dataset_id': 'old', 'ids': [self.row('good')['id']], 'excluded': True}
        with self.assertRaisesRegex(ValueError, '目录已切换'): self.ws.mark_skipped(payload)
        for status in ['running', 'stopping']:
            self.ws.job = {'status': status}
            with self.assertRaisesRegex(ValueError, '正在生成或导出'): self.mark(['good'])
        self.ws.job = None
        self.assertFalse(self.row('good')['excluded'])

    def test_fix_then_reload_rechecks_frame_and_runtime_failure_does_not_stop_other_frames(self):
        shutil.copy2(self.seg/'pig_test/good/segmentation.json', self.seg/'pig_test/missing_meta/segmentation.json')
        self.reload()
        self.assertEqual(self.row('missing_meta')['validation_errors'], [])
        from pointcloud_workbench.workspace import reconstruct
        def fail_one(source, segmentation, allow_draft):
            if Path(source).name == 'good': raise ValueError('深度校验失败')
            return reconstruct(source, segmentation, allow_draft)
        with patch('pointcloud_workbench.workspace.reconstruct', side_effect=fail_one):
            self.ws.start(self.payload(), 'generate')
            self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'completed_with_errors', self.ws.job)
        self.assertEqual(self.ws.job['success'], 2)
        self.assertEqual(len(self.ws.job['errors']), 1)
        self.assertIn('深度校验失败', self.row('good')['validation_errors'][0])
        self.reload()
        self.assertEqual(self.row('good')['validation_errors'], [])


class MissingRGBMetadataTest(unittest.TestCase):
    def test_rgb_missing_metadata_is_not_misclassified_as_ir_or_block_other_frames(self):
        fixture = test_rgb.RGBCloudTest()
        fixture.setUp()
        try:
            raw, seg = fixture.root/'dataset', fixture.root/'dataset-masks'
            entries = []
            for name in ['missing', 'good']:
                rel = 'itof-1110/captures/pig_test/'+name
                shutil.copytree(fixture.source, raw/rel)
                shutil.copytree(fixture.seg, seg/rel)
                entries.append({'relative_dir': rel, 'pig_id': 'test', 'capture_id': name})
            (seg/entries[0]['relative_dir']/'segmentation.json').unlink()
            write_json(seg/'dataset.json', {'input_mode': 'rgb', 'frames': entries})
            ws = Workspace(fixture.root/'rgb-settings')
            state = ws.load({'source': str(raw), 'segmentation': str(seg)})
            self.assertEqual(state['validation']['passed'], 1)
            self.assertEqual(state['validation']['failed'], 1)
            self.assertEqual(state['frames'][0]['input_mode'], 'rgb')
            self.assertNotIn('模式不一致', ' '.join(state['frames'][0]['validation_errors']))
            ws.start({'dataset_id': state['dataset_id'], 'ids': [r['id'] for r in state['frames']],
                      'parent': str(fixture.root), 'name': 'rgb-output', 'allow_draft': True}, 'generate')
            ws.worker.join(10)
            self.assertFalse(ws.worker.is_alive())
            self.assertEqual(ws.job['success'], 1, ws.job)
            self.assertEqual(len(ws.job['skipped']), 1)
        finally:
            fixture.tearDown()


if __name__ == '__main__': unittest.main()
