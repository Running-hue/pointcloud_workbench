#!/usr/bin/env python3
"""Loopback-only browser workbench with native filesystem dialogs."""
from __future__ import annotations
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, urlparse, urlencode
import webbrowser

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pointcloud_workbench.workspace import Workspace, RESULT_FILES
from pointcloud_workbench.viewer import render_viewer
from pointcloud_workbench.alignment import render_body_view
from pointcloud_workbench.core import np, read_json

APP_DIR = Path(__file__).resolve().parent


def make_handler(workspace, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='application/json; charset=utf-8', status=200):
            if not isinstance(body, bytes): body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.end_headers()
            self.wfile.write(body)

        def authorized_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def authorized_origin(self):
            origin = self.headers.get('Origin')
            return (not origin or origin in {f'http://127.0.0.1:{self.server.server_port}',
                    f'http://localhost:{self.server.server_port}'}) and self.headers.get('Sec-Fetch-Site') != 'cross-site'

        def do_GET(self):
            try:
                if not self.authorized_host():
                    self.send({'error': '仅限本机访问'}, status=403)
                    return
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path in {'/', '/alignment'}:
                    page = (APP_DIR/('alignment.html' if url.path == '/alignment' else 'index.html')).read_text(encoding='utf-8').replace('__TOKEN__', token)
                    self.send(page.encode('utf-8'), 'text/html; charset=utf-8')
                elif url.path in {'/workbench.js', '/workbench.css', '/alignment.js', '/alignment.css'}:
                    mime = 'text/javascript; charset=utf-8' if url.path.endswith('.js') else 'text/css; charset=utf-8'
                    self.send((APP_DIR/url.path[1:]).read_bytes(), mime)
                elif url.path == '/api/session':
                    if not self.authorized_origin():
                        self.send({'error': '不支持跨站请求'}, status=403)
                        return
                    self.send({'token': token})
                elif url.path == '/api/state': self.send(workspace.state())
                elif url.path == '/api/job':
                    with workspace.lock: self.send({'job': workspace.job})
                elif url.path == '/api/alignment/preview':
                    payload = {key: values[0] for key, values in q.items()}
                    self.send(workspace.alignment.preview(payload).encode('utf-8'), 'text/html; charset=utf-8')
                elif url.path == '/api/validation':
                    with workspace.lock:
                        if q.get('dataset_id', [''])[0] != workspace.dataset_id:
                            raise ValueError('目录已切换，请重新载入核对记录')
                        self.send(workspace.validation_report())
                elif url.path == '/api/media':
                    with workspace.lock:
                        if q.get('dataset_id', [''])[0] != workspace.dataset_id:
                            raise ValueError('目录已切换，请重新选择采集')
                        p = workspace.media(q['id'][0], q['file'][0])
                        if p.name in {'preview.html', 'body_preview.html'}:
                            # Render old and new exports through the current viewer without changing saved files.
                            body = p.name == 'body_preview.html'
                            arrays = workspace.media(q['id'][0], 'body_points.npz' if body else 'points.npz')
                            report = read_json(workspace.media(q['id'][0], 'alignment.json' if body else 'pointcloud.json'))
                        else: data = p.read_bytes()
                    if p.suffix == '.html':
                        if p.name in {'preview.html', 'body_preview.html'}:
                            with np.load(arrays, allow_pickle=False) as saved:
                                page = render_body_view(saved, report) if body else render_viewer(saved, report, embedded=True)
                        else:
                            page = data.decode('utf-8')
                        # Rewrite HTML attributes only. The script may contain selectors or
                        # recorded strings with the same text, which must remain untouched.
                        markup, separator, script = page.partition('<script>')
                        for name in RESULT_FILES:
                            route = '/api/media?' + urlencode({'dataset_id': q['dataset_id'][0], 'id': q['id'][0], 'file': name})
                            markup = markup.replace(f'href="{name}"', f'href="{route}" target="_blank"')
                            markup = markup.replace(f'src="{name}"', f'src="{route}"')
                        page = markup + separator + script
                        self.send(page.encode('utf-8'), 'text/html; charset=utf-8')
                    else: self.send(data, mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
                elif url.path == '/favicon.ico': self.send(b'', 'image/x-icon', 204)
                else: self.send({'error': '页面不存在'}, status=404)
            except (BrokenPipeError, ConnectionResetError): pass
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self.send({'error': str(exc)}, status=400)

        def do_POST(self):
            try:
                if not self.authorized_host():
                    self.send({'error': '请从本机点云工作台操作'}, status=403)
                    return
                if not self.authorized_origin():
                    self.send({'error': '不支持跨站请求'}, status=403)
                    return
                if self.headers.get('X-App-Token') != token:
                    self.send({'error': '页面连接已过期，请重新连接工作台', 'code': 'session_expired'}, status=403)
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1_000_000 or not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('需要有效的 JSON 请求')
                payload = json.loads(self.rfile.read(length))
                if self.path == '/api/choose_directory':
                    from pointcloud_workbench.native_picker import choose_directory
                    self.send(choose_directory(payload.get('path'), payload.get('title', '选择文件夹')))
                elif self.path == '/api/load': self.send(workspace.load(payload))
                elif self.path == '/api/open_results': self.send(workspace.open_results(payload.get('path')))
                elif self.path == '/api/mark_skipped': self.send(workspace.mark_skipped(payload))
                elif self.path == '/api/review_cloud': self.send(workspace.review_cloud(payload))
                elif self.path in {'/api/alignment/context', '/api/alignment/fit', '/api/alignment/save', '/api/alignment/confirm'}:
                    method = self.path.rsplit('/', 1)[1]
                    self.send(getattr(workspace.alignment, method)(payload))
                elif self.path == '/api/generate': self.send(workspace.start(payload, 'generate'))
                elif self.path == '/api/export': self.send(workspace.start(payload, 'export'))
                elif self.path == '/api/stop': self.send(workspace.stop())
                elif self.path == '/api/reveal':
                    with workspace.lock:
                        folder = workspace.last_output or workspace.results_root
                        if not folder or not folder.is_dir(): raise ValueError('还没有可打开的成果文件夹')
                    if sys.platform == 'darwin': subprocess.Popen(['/usr/bin/open', str(folder)])
                    elif sys.platform == 'win32': os.startfile(str(folder))
                    else: subprocess.Popen(['xdg-open', str(folder)])
                    self.send({'path': str(folder)})
                else: self.send({'error': '接口不存在'}, status=404)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self.send({'error': str(exc)}, status=400)
            except Exception as exc:
                self.send({'error': f'操作失败：{exc}'}, status=500)
    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8769)
    p.add_argument('--state', type=Path, default=APP_DIR/'.state')
    p.add_argument('--segmentation', type=Path)
    p.add_argument('--results', type=Path)
    p.add_argument('--open', action='store_true')
    args = p.parse_args()
    first_run = not (args.state/'settings.json').is_file()
    seg = APP_DIR.parent/'outputs/itof_SAM2猪体分割'
    results = APP_DIR.parent/'outputs/itof_多模态毫米点云预览'
    ws = Workspace(args.state,
        initial_segmentation=args.segmentation or (seg if first_run and seg.is_dir() else None),
        initial_results=args.results or (results if first_run and results.is_dir() else None))
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(ws, secrets.token_urlsafe(32)))
    except OSError as exc:
        p.error(f'端口 {args.port} 无法使用：{exc}')
    address = f'http://127.0.0.1:{server.server_port}'
    print(f'多模态点云工作台：{address}\n目录在浏览器选择；请保持本终端开启。', flush=True)
    if args.open: webbrowser.open(address)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        ws.stop()
        if ws.worker: ws.worker.join()
        server.server_close()


if __name__ == '__main__': main()
