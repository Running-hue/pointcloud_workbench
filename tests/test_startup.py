"""Regression coverage for stale services after pulling frontend/backend changes."""
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from io import BytesIO
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from pointcloud_workbench import app
from pointcloud_workbench.app import make_handler
from pointcloud_workbench.paths import PROJECT_ROOT
from pointcloud_workbench.startup import APP_ID, reuse_server, source_revision
from pointcloud_workbench.workspace import Workspace


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / 'state'
        self.ws = Workspace(self.state_dir)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.ws, 'test-token'))
        self.port = self.server.server_port
        self.address = f'http://127.0.0.1:{self.port}'
        def serve():
            try:
                self.server.serve_forever(poll_interval=.02)
            finally:
                self.server.server_close()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.temp.cleanup()

    def runtime(self):
        with urlopen(self.address + '/api/runtime') as response:
            return json.load(response)

    def post(self, route, payload):
        req = Request(self.address + route, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'X-App-Token': 'test-token'})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_matching_service_is_reused_without_losing_state(self):
        self.assertTrue(reuse_server(self.port, self.state_dir, source_revision()))
        self.assertTrue(self.thread.is_alive())

    def test_changed_code_restarts_idle_service(self):
        self.assertFalse(reuse_server(self.port, self.state_dir, 'new-code'))
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_running_job_prevents_restart(self):
        for status in ('running', 'stopping'):
            with self.subTest(status=status):
                self.ws.job = {'status': status}
                with self.assertRaisesRegex(ValueError, '正在生成或导出'):
                    reuse_server(self.port, self.state_dir, 'new-code')
                self.assertTrue(self.thread.is_alive())
                self.assertEqual(self.ws.job['status'], status)

    def test_live_worker_prevents_restart_even_after_job_status_changes(self):
        released = threading.Event()
        self.ws.worker = threading.Thread(target=released.wait, daemon=True)
        self.ws.worker.start()
        try:
            self.ws.job = {'status': 'done'}
            with self.assertRaisesRegex(ValueError, '正在处理操作'):
                reuse_server(self.port, self.state_dir, 'new-code')
            self.assertTrue(self.thread.is_alive())
        finally:
            released.set()
            self.ws.worker.join(3)

    def test_processing_post_prevents_restart_until_the_request_finishes(self):
        entered, released = threading.Event(), threading.Event()
        def detect(payload):
            entered.set()
            if not released.wait(5):
                raise ValueError('test processing request timed out')
            return {'status': 'processed'}
        with patch.object(self.ws.processing, 'detect', side_effect=detect), ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.post, '/api/processing/detect', {})
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(ValueError, '正在处理操作'):
                    reuse_server(self.port, self.state_dir, 'new-code')
                self.assertTrue(self.thread.is_alive())
            finally:
                released.set()
            self.assertEqual(future.result(5), {'status': 'processed'})
        self.assertFalse(reuse_server(self.port, self.state_dir, 'new-code'))

    def test_failed_processing_post_releases_the_restart_guard(self):
        with patch.object(self.ws.processing, 'detect', side_effect=ValueError('invalid capture')):
            with self.assertRaises(HTTPError) as caught:
                self.post('/api/processing/detect', {})
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.assertFalse(reuse_server(self.port, self.state_dir, 'new-code'))

    def test_processing_routes_remain_available_after_startup_merge(self):
        for route in ('context', 'detect', 'select', 'clear', 'edit', 'image_points', 'features'):
            with self.subTest(route=route), patch.object(self.ws.processing, route, return_value={'route': route}) as method:
                self.assertEqual(self.post('/api/processing/' + route, {'id': 'capture'}), {'route': route})
                method.assert_called_once_with({'id': 'capture'})
        with patch.object(self.ws.processing, 'image', return_value=b'png') as method:
            with urlopen(self.address + '/api/processing/image?id=capture', timeout=3) as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
                self.assertEqual(response.read(), b'png')
            method.assert_called_once_with({'id': 'capture'})

    def test_other_configuration_is_not_replaced(self):
        with self.assertRaisesRegex(ValueError, '其他项目或配置'):
            reuse_server(self.port, self.state_dir / 'different', 'new-code')
        self.assertTrue(self.thread.is_alive())

    def test_shutdown_requires_current_token_origin_and_revision(self):
        revision = self.runtime()['revision']
        for token, origin, expected, status in [
            ('wrong', self.address, revision, 403),
            ('test-token', 'https://other.example', revision, 403),
            ('test-token', self.address, 'wrong-revision', 400),
        ]:
            req = Request(self.address + '/api/shutdown',
                          data=json.dumps({'revision': expected}).encode(),
                          headers={'Content-Type': 'application/json', 'X-App-Token': token, 'Origin': origin})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req)
            self.assertEqual(caught.exception.code, status)
            caught.exception.close()
            self.assertTrue(self.thread.is_alive())
        self.assertTrue(reuse_server(self.port, self.state_dir, revision))

    def test_runtime_revision_describes_loaded_code(self):
        revision = self.runtime()['revision']
        with patch('pointcloud_workbench.app.source_revision', return_value='edited-on-disk'):
            self.assertEqual(self.runtime()['revision'], revision)

    def test_runtime_paths_still_identify_the_project_and_existing_state(self):
        info = self.runtime()
        self.assertEqual(info['root'], str(PROJECT_ROOT))
        self.assertEqual(info['state_dir'], str(self.state_dir.resolve()))


class SourceRevisionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, content='first'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_revision_tracks_added_modified_and_removed_source_in_all_source_directories(self):
        for relative in ('app.py', 'backend/app.py', 'backend/services/helper.py',
                         'frontend/pages/processing.html', 'frontend/templates/body_view.html',
                         'frontend/assets/js/processing.js', 'frontend/assets/css/processing.css',
                         'scripts/启动点云工作台.bat', 'scripts/启动点云工作台.command'):
            with self.subTest(source=relative):
                before = source_revision(self.root)
                path = self.write(relative)
                added = source_revision(self.root)
                self.assertNotEqual(added, before)
                path.write_text('second', encoding='utf-8')
                self.assertNotEqual(source_revision(self.root), added)
                path.unlink()
                self.assertEqual(source_revision(self.root), before)

    def test_revision_excludes_user_state_outputs_and_caches(self):
        self.write('backend/app.py')
        original = source_revision(self.root)
        for relative in ('settings.json', '.state/settings.json', '.state/preview.html',
                         'outputs/preview.html', 'captures/processing.js',
                         'backend/__pycache__/cached.py', 'backend/.state/preview.html',
                         'frontend/node_modules/library/index.js', 'scripts/.venv/helper.py',
                         'tests/test_startup.py'):
            with self.subTest(ignored=relative):
                self.write(relative)
                self.assertEqual(source_revision(self.root), original)

    def test_revision_includes_relative_paths_when_identical_content_moves(self):
        path = self.write('frontend/pages/index.html', 'same content')
        original = source_revision(self.root)
        path.rename(path.with_name('processing.html'))
        self.assertNotEqual(source_revision(self.root), original)


class ReuseConnectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name).resolve()
        self.info = {'app': APP_ID, 'root': str(PROJECT_ROOT),
                     'state_dir': str(self.state_dir), 'revision': 'old-code'}
        self.opener = Mock()
        self.open_patch = patch('pointcloud_workbench.startup.build_opener', return_value=self.opener)
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def response(self, data):
        return BytesIO(json.dumps(data).encode())

    def handshake(self):
        return [self.response(self.info), self.response({'token': 'token'})]

    def test_reset_and_disconnect_during_shutdown_and_polling_wait_for_refused_connection(self):
        errors = (ConnectionResetError(10054, 'reset'), URLError(ConnectionResetError(10054, 'reset')),
                  RemoteDisconnected('closed'), TimeoutError('timed out'), URLError(OSError(10053, 'aborted')))
        for error in errors:
            with self.subTest(error=repr(error)), patch('pointcloud_workbench.startup.time.sleep'):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = self.handshake() + [error, error,
                    self.response(self.info), URLError(ConnectionRefusedError(10061, 'closed'))]
                self.assertFalse(reuse_server(8769, self.state_dir, 'new-code'))
                self.assertEqual(self.opener.open.call_count, 6)

    def test_reset_does_not_permit_restart_while_the_old_listener_is_still_open(self):
        self.opener.open.side_effect = self.handshake() + [ConnectionResetError(10054, 'reset'),
            self.response(self.info)]
        with patch('pointcloud_workbench.startup.time.monotonic', side_effect=[0, 1, 9]), \
             patch('pointcloud_workbench.startup.time.sleep'):
            with self.assertRaisesRegex(ValueError, '旧工作台尚未退出'):
                reuse_server(8769, self.state_dir, 'new-code')

    def test_refused_connection_and_dynamic_port_start_a_new_service(self):
        for error in (URLError(ConnectionRefusedError(10061, 'closed')), URLError(OSError(10061, 'closed'))):
            with self.subTest(error=repr(error)):
                self.opener.open.side_effect = error
                self.assertFalse(reuse_server(8769, self.state_dir, 'new-code'))
        self.opener.open.reset_mock()
        self.assertFalse(reuse_server(0, self.state_dir, 'new-code'))
        self.opener.open.assert_not_called()

    def test_initial_reset_is_reported_without_assuming_the_port_is_free(self):
        self.opener.open.side_effect = ConnectionResetError(10054, 'reset')
        with self.assertRaisesRegex(ValueError, '无法检查端口'):
            reuse_server(8769, self.state_dir, 'new-code')

    def test_foreign_service_is_never_sent_a_shutdown_request(self):
        for changed in ({'app': 'another-app'}, {'root': str(PROJECT_ROOT.parent)},
                        {'state_dir': str(self.state_dir / 'other')}):
            with self.subTest(changed=changed):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [self.response({**self.info, **changed})]
                with self.assertRaisesRegex(ValueError, '其他项目或配置'):
                    reuse_server(8769, self.state_dir, 'new-code')
                self.assertEqual(self.opener.open.call_count, 1)

    def test_invalid_runtime_information_has_a_clear_error(self):
        for info in ([], {}, {**self.info, 'revision': None}):
            with self.subTest(info=info):
                self.opener.open.side_effect = [self.response(info)]
                with self.assertRaisesRegex(ValueError, '未返回有效版本信息'):
                    reuse_server(8769, self.state_dir, 'new-code')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows path comparison')
    def test_windows_path_case_does_not_prevent_reusing_the_same_service(self):
        info = {**self.info, 'root': str(PROJECT_ROOT).swapcase(),
                'state_dir': str(self.state_dir).swapcase()}
        self.opener.open.side_effect = [self.response(info)]
        self.assertTrue(reuse_server(8769, self.state_dir, 'old-code'))


class StartupEntryTest(unittest.TestCase):
    def test_open_reuses_service_before_loading_workspace(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(sys, 'argv', ['app.py', '--open', '--state', temp, '--port', '12345']), \
             patch.object(app, 'source_revision', return_value='current-code'), \
             patch.object(app, 'reuse_server', return_value=True) as reuse, \
             patch.object(app, 'Workspace') as workspace, \
             patch.object(app.webbrowser, 'open') as browser:
            app.main()
            reuse.assert_called_once_with(12345, Path(temp), 'current-code')
            workspace.assert_not_called()
            browser.assert_called_once_with('http://127.0.0.1:12345')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows batch launchers')
    def test_both_windows_launchers_forward_arguments_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            for relative in ('启动点云工作台.bat', 'scripts/启动点云工作台.bat'):
                with self.subTest(launcher=relative):
                    result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c',
                        str(PROJECT_ROOT / relative), '--help'], cwd=temp, input='\n',
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('--state', result.stdout)
                    self.assertIn('--port', result.stdout)


if __name__ == '__main__':
    unittest.main()
