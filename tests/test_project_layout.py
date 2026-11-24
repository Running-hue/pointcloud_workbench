"""Contracts that must survive source/resource directory changes."""
from http.server import ThreadingHTTPServer
import importlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, build_opener, ProxyHandler, urlopen

from pointcloud_workbench.app import make_handler
from pointcloud_workbench.paths import (PROJECT_ROOT, WORKSPACE_ROOT, DEFAULT_STATE_DIR,
    DEFAULT_OUTPUT_ROOT, SHARED_DEPENDENCY_DIR)
from pointcloud_workbench.workspace import Workspace


class ProjectLayoutTest(unittest.TestCase):
    def test_established_imports_still_resolve_to_the_same_public_modules(self):
        for name in ('app', 'batch', 'core', 'rgb', 'registration', 'viewer',
                     'workspace', 'alignment', 'processing', 'native_picker', 'geometry', 'startup'):
            with self.subTest(module=name):
                module = importlib.import_module('pointcloud_workbench.'+name)
                self.assertEqual(module.__name__, 'pointcloud_workbench.'+name)
                self.assertEqual(Path(module.__file__).resolve().parent, PROJECT_ROOT/'backend')

    def test_existing_state_outputs_and_shared_dependencies_keep_their_locations(self):
        self.assertEqual(DEFAULT_STATE_DIR, PROJECT_ROOT/'.state')
        self.assertEqual(DEFAULT_OUTPUT_ROOT, WORKSPACE_ROOT/'outputs')
        self.assertEqual(SHARED_DEPENDENCY_DIR, WORKSPACE_ROOT/'segmentation_workbench'/'.deps')

    def test_installed_dependencies_are_not_overridden_by_shared_mac_binaries(self):
        command = [sys.executable, '-c',
            'import sys; import pointcloud_workbench.core; '
            'from pointcloud_workbench.paths import SHARED_DEPENDENCY_DIR; '
            'assert str(SHARED_DEPENDENCY_DIR) not in sys.path']
        result = subprocess.run(command, cwd=WORKSPACE_ROOT,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_original_http_routes_serve_the_moved_pages_and_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp)/'state')
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(workspace, 'layout-test'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                for route, filename in (('/', 'index.html'), ('/alignment', 'alignment.html'),
                                        ('/processing', 'processing.html')):
                    with self.subTest(route=route), urlopen(url+route) as response:
                        expected = (PROJECT_ROOT/'frontend'/'pages'/filename).read_text(encoding='utf-8')
                        self.assertEqual(response.read().decode(), expected.replace('__TOKEN__', 'layout-test'))
                for group, extension in (('js', 'js'), ('css', 'css')):
                    for name in ('workbench', 'alignment', 'processing'):
                        filename = name+'.'+extension
                        with self.subTest(asset=filename), urlopen(url+'/'+filename) as response:
                            self.assertEqual(response.read(), (PROJECT_ROOT/'frontend'/'assets'/group/filename).read_bytes())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_real_cli_starts_from_unicode_directory_serves_resources_and_exits_cleanly(self):
        with tempfile.TemporaryDirectory(prefix='点云 启动验证 ') as temp:
            folder = Path(temp)
            state_dir = folder/'state'
            state_dir.mkdir()
            (state_dir/'settings.json').write_text('{}', encoding='utf-8')
            output_file = folder/'server.log'
            env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            with output_file.open('wb') as output:
                process = subprocess.Popen([sys.executable, str(PROJECT_ROOT/'app.py'),
                    '--port', '0', '--state', str(state_dir)], cwd=folder,
                    stdout=output, stderr=subprocess.STDOUT, env=env)
                try:
                    deadline = time.monotonic()+20
                    address = None
                    while time.monotonic() < deadline:
                        log = output_file.read_text(encoding='utf-8', errors='replace')
                        match = re.search(r'http://127\.0\.0\.1:\d+', log)
                        if match:
                            address = match.group(0)
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(.05)
                    self.assertIsNotNone(address, log)
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(address+'/api/runtime', timeout=5) as response:
                        runtime = json.load(response)
                    self.assertEqual(runtime['app'], 'pointcloud_workbench')
                    self.assertEqual(Path(runtime['root']).resolve(), PROJECT_ROOT)
                    self.assertEqual(Path(runtime['state_dir']).resolve(), state_dir.resolve())
                    for route in ('/', '/alignment', '/processing', '/workbench.js',
                                  '/alignment.css', '/processing.js'):
                        with self.subTest(route=route), opener.open(address+route, timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.read())
                    with opener.open(address+'/api/session', timeout=5) as response:
                        token = json.load(response)['token']
                    request = Request(address+'/api/shutdown',
                        data=json.dumps({'revision': runtime['revision']}).encode(),
                        headers={'Content-Type': 'application/json', 'X-App-Token': token})
                    with opener.open(request, timeout=5) as response:
                        self.assertEqual(json.load(response)['status'], 'restarting')
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)

    def test_original_cli_paths_and_module_entry_points_still_work(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, option in (('app', '--state'), ('batch', '--output')):
                for script in (PROJECT_ROOT/(name+'.py'), PROJECT_ROOT/'backend'/(name+'.py')):
                    with self.subTest(script=script):
                        result = subprocess.run([sys.executable, str(script), '--help'], cwd=temp,
                            capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn(option, result.stdout)
                with self.subTest(module=name):
                    result = subprocess.run([sys.executable, '-m', 'pointcloud_workbench.'+name, '--help'],
                        cwd=WORKSPACE_ROOT, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(option, result.stdout)


if __name__ == '__main__':
    unittest.main()
