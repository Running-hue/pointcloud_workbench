"""Regression coverage for stale services after pulling frontend/backend changes."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from pointcloud_workbench.app import make_handler
from pointcloud_workbench.startup import reuse_server, source_revision
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

    def test_matching_service_is_reused_without_losing_state(self):
        self.assertTrue(reuse_server(self.port, self.state_dir, source_revision()))
        self.assertTrue(self.thread.is_alive())

    def test_changed_code_restarts_idle_service(self):
        self.assertFalse(reuse_server(self.port, self.state_dir, 'new-code'))
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_running_job_prevents_restart(self):
        self.ws.job = {'status': 'running'}
        with self.assertRaisesRegex(ValueError, '正在生成或导出'):
            reuse_server(self.port, self.state_dir, 'new-code')
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.ws.job['status'], 'running')

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
            self.assertTrue(self.thread.is_alive())
        self.assertTrue(reuse_server(self.port, self.state_dir, revision))

    def test_runtime_revision_describes_loaded_code(self):
        revision = self.runtime()['revision']
        with patch('pointcloud_workbench.app.source_revision', return_value='edited-on-disk'):
            self.assertEqual(self.runtime()['revision'], revision)

    def test_revision_covers_backend_and_templates_but_not_settings(self):
        root = Path(self.temp.name)
        (root / 'app.py').write_text('first')
        original = source_revision(root)
        (root / 'settings.json').write_text('{}')
        self.assertEqual(source_revision(root), original)
        (root / 'alignment.html').write_text('<canvas>')
        changed = source_revision(root)
        self.assertNotEqual(changed, original)
        (root / 'app.py').write_text('second')
        self.assertNotEqual(source_revision(root), changed)


if __name__ == '__main__':
    unittest.main()
