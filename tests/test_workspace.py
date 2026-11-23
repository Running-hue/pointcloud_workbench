import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from pointcloud_workbench.tests import test_geometry
from pointcloud_workbench.core import read_json, write_json, sha256, np, Image, required_result_files
from pointcloud_workbench.workspace import Workspace, RESULT_FILES
from pointcloud_workbench.app import make_handler
from pointcloud_workbench import native_picker


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_geometry.WorkflowTest(); self.fixture.setUp()
        self.root = self.fixture.root.resolve()
        self.source, self.seg = self.root/'raw', self.root/'masks'
        self.rel = 'pig_test/capture'
        (self.source/'pig_test').mkdir(parents=True)
        (self.seg/'pig_test').mkdir(parents=True)
        shutil.move(self.fixture.source, self.source/self.rel)
        shutil.move(self.fixture.seg, self.seg/self.rel)
        write_json(self.seg/'dataset.json', {'source':'Z:\\old\\raw', 'frames':[
            {'relative_dir':self.rel, 'source_dir':'Z:\\old\\raw\\pig_test\\capture',
             'id':'old-id', 'pig_id':'test', 'capture_id':'capture'}]})
        self.ws = Workspace(self.root/'settings')
        self.ws.load({'source':str(self.source), 'segmentation':str(self.seg)})

    def tearDown(self):
        if self.ws.worker: self.ws.worker.join(10)
        self.fixture.tearDown()

    def payload(self, name='generated', **extra):
        return {'dataset_id':self.ws.dataset_id, 'ids':[self.ws.state()['frames'][0]['id']],
                'parent':str(self.root), 'name':name, **extra}

    def generate(self):
        self.ws.start(self.payload(allow_draft=True), 'generate')
        self.ws.worker.join(10)
        self.assertFalse(self.ws.worker.is_alive())
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        return self.root/'generated'

    def test_relocated_inputs_generate_and_folder_export_preserve_all_data(self):
        before = {str(p):sha256(p) for root in [self.source,self.seg] for p in root.rglob('*') if p.is_file()}
        row = self.ws.state()['frames'][0]
        self.assertFalse(row['missing'])
        generated = self.generate()
        report = read_json(generated/self.rel/'pointcloud.json')
        self.assertTrue(all((generated/self.rel/n).is_file() for n in required_result_files(report)))
        self.assertFalse((generated/self.rel/'cloud_rgb_mm.ply').exists())
        with np.load(generated/self.rel/'points.npz') as points:
            self.assertEqual(len(points['xyz_mm']), 34)
            np.testing.assert_array_equal(points['xyz_mm'][:,2], 1000)
        self.ws.start(self.payload(name='copied'), 'export'); self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        for name in required_result_files(report):
            self.assertEqual(sha256(generated/self.rel/name), sha256(self.root/'copied'/self.rel/name))
        self.assertEqual(before, {str(p):sha256(p) for root in [self.source,self.seg] for p in root.rglob('*') if p.is_file()})
        manifest = read_json(self.root/'copied/pointcloud_dataset.json')
        self.assertEqual(manifest['frames'][0]['relative_dir'], self.rel)
        self.assertTrue(manifest['frames'][0]['has_cloud'])
        self.assertEqual(manifest['frames'][0]['primary_ply'],'cloud_ir_mm.ply')
        restored = Workspace(self.root/'settings')
        self.assertEqual(restored.state()['clouds'], 1)
        self.assertEqual(restored.source, self.source)

    def test_review_gate_and_latest_mask_state_are_enforced(self):
        with self.assertRaisesRegex(ValueError, '没有可生成项'):
            self.ws.start(self.payload(), 'generate')
        meta = read_json(self.seg/self.rel/'segmentation.json')
        meta.update(reviewed=True,status='reviewed')
        write_json(self.seg/self.rel/'segmentation.json',meta)
        Image.new('L',(7,5),255).save(self.seg/self.rel/'trunk_mask.png')
        self.ws.load({'source':str(self.source),'segmentation':str(self.seg)})
        meta.update(reviewed=False,status='draft')
        write_json(self.seg/self.rel/'segmentation.json',meta)
        self.ws.start(self.payload(),'generate'); self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'failed')
        self.assertEqual(len(self.ws.job['skipped']),1)
        self.assertFalse((self.root/'generated').exists())

    def test_cloud_quality_review_is_reversible_persistent_and_portable(self):
        generated=self.generate()
        frame=self.ws.state()['frames'][0]
        before={str(p):sha256(p) for root in [self.source,self.seg,generated] for p in root.rglob('*') if p.is_file()}
        for decision in ['reject','pending','keep']:
            state=self.ws.review_cloud({'dataset_id':self.ws.dataset_id,'id':frame['id'],'decision':decision})
            self.assertEqual(state['frames'][0]['cloud_decision'],decision)
        self.assertEqual(before,{str(p):sha256(p) for root in [self.source,self.seg,generated] for p in root.rglob('*') if p.is_file()})
        restored=Workspace(self.root/'settings')
        self.assertEqual(restored.state()['frames'][0]['cloud_decision'],'keep')
        self.ws.start(self.payload('reviewed-export'),'export');self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        exported=self.root/'reviewed-export'
        self.assertEqual(read_json(exported/self.rel/'cloud_review.json')['decision'],'keep')
        portable=Workspace(self.root/'new-state',initial_results=exported)
        self.assertEqual(portable.state()['frames'][0]['cloud_decision'],'keep')
        record=read_json(exported/self.rel/'cloud_review.json');record['input_mode']='rgb'
        write_json(exported/self.rel/'cloud_review.json',record)
        portable=Workspace(self.root/'third-state',initial_results=exported)
        self.assertEqual(portable.state()['frames'][0]['cloud_decision'],'pending')
        self.assertIn('恢复为待定',portable.state()['warning'])
        with self.assertRaisesRegex(ValueError,'目录已切换'):
            self.ws.review_cloud({'dataset_id':'stale','id':frame['id'],'decision':'reject'})
        with self.assertRaisesRegex(ValueError,'只能是'):
            self.ws.review_cloud({'dataset_id':self.ws.dataset_id,'id':frame['id'],'decision':'other'})

    def test_camera_classification_uses_capture_record_before_generation(self):
        path = self.source/self.rel/'reconstruction.json'
        record = read_json(path)
        for model, kind in [('Orbbec Femto Bolt', 'itof'), ('Orbbec Gemini 336L', 'stereo')]:
            record['runtime']['device'] = {'name': model}
            write_json(path, record)
            state = self.ws.load({'source': str(self.source), 'segmentation': str(self.seg)})
            self.assertEqual(state['frames'][0]['camera']['type'], kind)

    def test_legacy_screening_requires_version_confirmation(self):
        generated = self.generate()
        frame = self.ws.state()['frames'][0]
        self.ws.review_cloud({'dataset_id': self.ws.dataset_id, 'id': frame['id'], 'decision': 'keep'})
        settings = read_json(self.ws.state_dir/'settings.json')
        record = settings['cloud_reviews'][str(generated)][self.rel]
        record['schema_version'] = 1
        record.pop('processing_version')
        write_json(self.ws.state_dir/'settings.json', settings)
        restored = Workspace(self.ws.state_dir)
        row = restored.state()['frames'][0]
        self.assertEqual(row['cloud_decision'], 'pending')
        self.assertTrue(row['cloud_review_stale'])
        restored.review_cloud({'dataset_id': restored.dataset_id, 'id': row['id'], 'decision': 'keep'})
        self.assertFalse(restored.state()['frames'][0]['cloud_review_stale'])
        self.assertEqual(Workspace(self.ws.state_dir).state()['frames'][0]['cloud_decision'], 'keep')

    def test_grouped_full_export_copies_rejected_cloud_and_validates_manifest_paths(self):
        generated=self.generate();frame=self.ws.state()['frames'][0]
        self.ws.review_cloud({'dataset_id':self.ws.dataset_id,'id':frame['id'],'decision':'reject'})
        self.ws.start(self.payload('grouped-reject',decisions=['reject','pending'],group_by_decision=True),'export')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        output=self.root/'grouped-reject';dest=output/'淘汰'/self.rel
        self.assertEqual(sha256(dest/'points.npz'),sha256(generated/self.rel/'points.npz'))
        self.assertTrue((output/'待定').is_dir())
        portable=Workspace(self.root/'new-grouped-state',initial_results=output)
        self.assertEqual(portable.state()['frames'][0]['relative_dir'],self.rel)
        self.assertEqual(portable.state()['frames'][0]['cloud_decision'],'reject')
        manifest=read_json(output/'pointcloud_dataset.json')
        manifest['frames'][0]['storage_relative_dir']='../escape'
        write_json(output/'pointcloud_dataset.json',manifest)
        with self.assertRaisesRegex(ValueError,'非法'):
            self.ws.open_results(str(output))
        for decisions in ([],['unknown'],['reject','reject'],'reject'):
            with self.assertRaisesRegex(ValueError,'导出状态'):
                self.ws.start(self.payload('invalid-groups',decisions=decisions,group_by_decision=True),'export')
        with self.assertRaisesRegex(ValueError,'未勾选'):
            self.ws.start(self.payload('unselected',decisions=['keep'],group_by_decision=True),'export')

    def test_export_updates_legacy_preview_without_changing_measured_files(self):
        generated = self.generate()
        original = generated/self.rel/'preview.html'
        original.write_text('<html>legacy dark preview</html>')
        self.ws.start(self.payload('updated-preview'), 'export'); self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'completed', self.ws.job)
        copied = self.root/'updated-preview'/self.rel
        self.assertIn('color-scheme:light', (copied/'preview.html').read_text())
        self.assertIn('legacy dark preview', original.read_text())
        for name in ['points.npz', 'cloud_ir_mm.ply', 'pointcloud.json']:
            self.assertEqual(sha256(generated/self.rel/name), sha256(copied/name))

    def test_reopen_restores_terminal_task_details_without_restoring_a_running_job(self):
        generated = self.generate()
        restored = Workspace(self.root/'settings')
        self.assertEqual(restored.state()['job']['status'], 'completed')
        self.assertEqual(restored.state()['job']['success'], 1)
        self.assertIsNone(restored.worker)
        report = read_json(generated/'export.json')
        for change in [{'status':'running'}, {'status':'stopping'}, {'errors':None}]:
            write_json(generated/'export.json', {**report, **change})
            self.assertIsNone(restored.open_results(str(generated))['job'])
        write_json(generated/'export.json', report)

    def test_protect_sources_existing_outputs_and_stale_page(self):
        for parent, name in [(self.source,'output'),(self.seg,'output'),(self.root,'raw')]:
            with self.assertRaises(ValueError): self.ws.start({**self.payload(allow_draft=True),'parent':str(parent),'name':name},'generate')
        with self.assertRaisesRegex(ValueError,'目录已切换'):
            self.ws.start({**self.payload(), 'dataset_id':'old'},'generate')
        self.generate()
        with self.assertRaises(ValueError): self.ws.start(self.payload(allow_draft=True),'generate')

    def test_failed_folder_copy_does_not_publish_partial_capture(self):
        generated = self.generate()
        (generated/self.rel/'escape').symlink_to(self.source, target_is_directory=True)
        self.ws.start(self.payload('badcopy'),'export'); self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'failed')
        self.assertIn('符号链接',self.ws.job['errors'][0]['error'])
        self.assertFalse((self.root/'badcopy').exists())
        self.assertFalse(list(self.root.glob('.pointcloud-export-*')))

    def test_failed_generation_can_reload_same_inputs_and_retry(self):
        with patch('pointcloud_workbench.workspace.reconstruct', side_effect=ValueError('输入未对齐')):
            self.ws.start(self.payload(allow_draft=True), 'generate')
            self.ws.worker.join(10)
        self.assertEqual(self.ws.state()['job']['status'], 'failed')
        self.ws.stop_event.set()
        previous_id = self.ws.dataset_id
        state = self.ws.load({'source':str(self.source), 'segmentation':str(self.seg)})
        self.assertIsNone(state['job'])
        self.assertNotEqual(previous_id, state['dataset_id'])
        self.assertFalse(self.ws.stop_event.is_set())
        self.assertTrue(self.generate().is_dir())

    def test_invalid_reload_keeps_failure_and_active_jobs_cannot_be_reset(self):
        self.ws.job = {'status':'failed', 'error':'原失败记录'}
        with self.assertRaises(ValueError):
            self.ws.load({'source':str(self.root/'absent'), 'segmentation':str(self.seg)})
        self.assertEqual(self.ws.job['error'], '原失败记录')
        for status in ['running', 'stopping']:
            self.ws.job = {'status':status}
            with self.assertRaisesRegex(ValueError, '正在生成或导出'):
                self.ws.load({'source':str(self.source), 'segmentation':str(self.seg)})
            self.assertEqual(self.ws.job['status'], status)
        self.ws.job = None

    def test_switch_inputs_clears_old_failure_and_result_location(self):
        generated = self.generate()
        old_seg = self.root/'other-masks'
        shutil.copytree(self.seg, old_seg)
        state = self.ws.load({'source':str(self.source), 'segmentation':str(old_seg)})
        self.assertIsNone(state['job'])
        self.assertEqual(state['clouds'], 0)
        self.assertEqual(state['results'], '')
        self.assertEqual(state['last_output'], '')
        self.assertTrue(generated.is_dir())

    def test_subset_export_preserves_loaded_clouds_and_stop_publishes_complete_folders(self):
        generated = self.generate()
        shutil.copytree(generated/self.rel, generated/'pig_test/second')
        report = read_json(generated/'pig_test/second/pointcloud.json')
        report['capture_id'] = 'second'
        write_json(generated/'pig_test/second/pointcloud.json', report)
        self.ws.open_results(str(generated))
        self.ws.start(self.payload('subset'), 'export'); self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'completed')
        self.assertEqual(self.ws.state()['clouds'], 2)
        self.assertEqual(self.ws.results_root, generated)
        self.assertEqual(self.ws.last_output, self.root/'subset')
        from pointcloud_workbench.workspace import safe_copy_tree
        def copy_then_stop(source, target):
            safe_copy_tree(source, target)
            self.ws.stop()
        ids = [r['id'] for r in self.ws.state()['frames'] if r['has_cloud']]
        with patch('pointcloud_workbench.workspace.safe_copy_tree', side_effect=copy_then_stop):
            self.ws.start({**self.payload('stopped'), 'ids':ids}, 'export')
            self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'], 'stopped')
        self.assertEqual(self.ws.job['success'], 1)
        self.assertEqual(self.ws.state()['clouds'], 2)
        self.assertEqual(len(list((self.root/'stopped').rglob('points.npz'))), 1)

    def test_open_results_rejects_symlink_ancestor(self):
        generated = self.generate()
        other = self.root/'links'; other.mkdir()
        (other/'pig_test').symlink_to(generated/'pig_test',target_is_directory=True)
        # pathlib does not descend directory symlinks; a root containing only links is not a dataset.
        with self.assertRaises(ValueError): self.ws.open_results(str(other))

    def test_dataset_mode_cannot_silently_use_a_different_frame_mode(self):
        dataset=read_json(self.seg/'dataset.json')
        dataset['input_mode']='rgb'
        write_json(self.seg/'dataset.json',dataset)
        state = self.ws.load({'source':str(self.source),'segmentation':str(self.seg)})
        self.assertEqual(state['validation']['failed'], 1)
        self.assertIn('输入模式不一致', state['frames'][0]['validation_errors'][0])
        with self.assertRaisesRegex(ValueError, '没有可生成项'):
            self.ws.start(self.payload(allow_draft=True), 'generate')

    def test_open_other_segmentation_results_cannot_generate_from_current_inputs(self):
        generated=self.generate()
        report=read_json(generated/self.rel/'pointcloud.json')
        report['segmentation_json_sha256']='other-camera-or-segmentation'
        write_json(generated/self.rel/'pointcloud.json',report)
        state=self.ws.open_results(str(generated))
        self.assertFalse(state['results_match_inputs'])
        with self.assertRaisesRegex(ValueError,'不是同一批输入'):
            self.ws.start(self.payload('must-not-generate',allow_draft=True),'generate')
        state=self.ws.load({'source':str(self.source),'segmentation':str(self.seg)})
        self.assertTrue(state['results_match_inputs'])
        self.assertEqual(state['clouds'],0)
        self.assertEqual(state['results'],'')

    def test_http_preview_links_and_post_authentication(self):
        self.generate()
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.ws,'test-token'))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        try:
            request = Request(address+'/api/export',data=json.dumps(self.payload('http-export')).encode(),headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught: urlopen(request)
            self.assertEqual(caught.exception.code,403)
            request.add_header('X-App-Token','test-token')
            request.add_header('Origin','https://other.example')
            with self.assertRaises(HTTPError) as caught: urlopen(request)
            self.assertEqual(caught.exception.code,403)
            from urllib.parse import urlencode
            query = urlencode({'dataset_id':self.ws.dataset_id,'id':self.payload()['ids'][0],'file':'preview.html'})
            page = urlopen(address+'/api/media?'+query).read().decode()
            self.assertIn('file=mask_depth_check.png',page)
            self.assertNotIn('href="mask_depth_check.png"',page)
            self.assertIn('<body class="embedded">',page)
            self.assertIn('id="notesDialog"',page)
            self.assertNotIn('src="mask_depth_check.png"',page)
            request = Request(address+'/api/export', data=json.dumps(self.payload('http-export')).encode(),headers={'Content-Type':'application/json','X-App-Token':'test-token'})
            self.assertEqual(urlopen(request).status,200)
            self.ws.worker.join(10)
            self.assertEqual(self.ws.job['status'],'completed')
        finally: server.shutdown(); server.server_close(); thread.join(2)

    def test_expired_page_can_reconnect_without_weakening_origin_checks(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.ws,'new-server-token'))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        def choose(token, **headers):
            return Request(address+'/api/choose_directory',data=json.dumps({'path':str(self.source)}).encode(),
                headers={'Content-Type':'application/json','X-App-Token':token,**headers})
        try:
            with patch.object(native_picker,'choose_directory',return_value={'path':str(self.source),'cancelled':False}) as picker:
                with self.assertRaises(HTTPError) as caught: urlopen(choose('old-page-token'))
                self.assertEqual(json.load(caught.exception)['code'],'session_expired')
                picker.assert_not_called()
                session = json.load(urlopen(address+'/api/session'))
                self.assertEqual(json.load(urlopen(choose(session['token'])))['path'],str(self.source))
                picker.assert_called_once()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(choose('new-server-token',Origin='https://other.example'))
                self.assertEqual(caught.exception.code,403)
                picker.assert_called_once()
                request = Request(address+'/api/session',headers={'Origin':'https://other.example'})
                with self.assertRaises(HTTPError) as caught: urlopen(request)
                self.assertEqual(caught.exception.code,403)
                request = Request(address+'/api/session',headers={'Sec-Fetch-Site':'cross-site'})
                with self.assertRaises(HTTPError) as caught: urlopen(request)
                self.assertEqual(caught.exception.code,403)
        finally: server.shutdown(); server.server_close(); thread.join(2)


class NativePickerTest(unittest.TestCase):
    def test_native_dispatch_cancel_and_unicode_path(self):
        with tempfile.TemporaryDirectory(prefix='点云 ') as root:
            for platform, output in [('darwin',root+'\n'),('win32',json.dumps({'path':root}))]:
                with self.subTest(platform=platform), patch.object(native_picker.sys,'platform',platform), patch.object(native_picker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,output,'')) as run:
                    self.assertEqual(native_picker.choose_directory(root)['path'],str(Path(root).resolve()))
                    args = run.call_args.args[0]
                    self.assertIn('/usr/bin/osascript' if platform == 'darwin' else '--windows-helper',args)
            with patch.object(native_picker.sys,'platform','darwin'), patch.object(native_picker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'\n','')):
                self.assertTrue(native_picker.choose_directory(root)['cancelled'])

    def test_picker_failure_releases_lock(self):
        with patch.object(native_picker.sys,'platform','darwin'), patch.object(native_picker.subprocess,'run',side_effect=subprocess.TimeoutExpired('picker',600)):
            with self.assertRaisesRegex(ValueError,'超时'): native_picker.choose_directory()
        self.assertFalse(native_picker.PICKER_LOCK.locked())


if __name__ == '__main__': unittest.main()
