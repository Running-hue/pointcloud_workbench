"""Identify the running workbench before reusing its browser service."""
from __future__ import annotations

import hashlib
from http.client import RemoteDisconnected
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

APP_DIR = Path(__file__).resolve().parent
APP_ID = 'pointcloud_workbench'


def source_revision(root=APP_DIR):
    digest = hashlib.sha256()
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in {'.py', '.html', '.js', '.css', '.bat'}:
            digest.update(path.name.encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def reuse_server(port, state_dir, revision):
    """Reuse matching code, or gracefully close an idle older instance."""
    if not port:
        return False
    address = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-App-Token'] = token
        req = Request(address + path, headers=headers,
                      data=json.dumps(payload).encode() if payload is not None else None)
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    try:
        info = request('/api/runtime')
    except HTTPError as exc:
        raise ValueError(f'端口 {port} 的服务不支持版本检查。请关闭旧工作台终端后重新启动。') from exc
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False
        raise ValueError(f'无法检查端口 {port} 上的服务：{exc.reason}') from exc
    except (TimeoutError, ValueError) as exc:
        raise ValueError(f'端口 {port} 上的服务未返回有效版本信息，请检查原服务终端。') from exc
    if (info.get('app') != APP_ID or info.get('root') != str(APP_DIR)
            or info.get('state_dir') != str(Path(state_dir).resolve())):
        raise ValueError(f'端口 {port} 已被其他项目或配置占用，请使用 --port 指定其他端口。')
    if info.get('revision') == revision:
        return True
    token = request('/api/session')['token']
    try:
        request('/api/shutdown', {'revision': info['revision']}, token)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get('error', str(exc))
        except ValueError:
            detail = str(exc)
        raise ValueError(f'代码已更新，原服务暂不能重启：{detail}。请稍后重新启动。') from exc
    print('检测到代码更新，正在重新启动空闲的点云工作台…', flush=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            request('/api/runtime')
        except URLError as exc:
            if isinstance(exc.reason, ConnectionRefusedError):
                return False
        except (ConnectionResetError, RemoteDisconnected, TimeoutError):
            pass  # Windows may reset an in-flight connection as the listener closes.
        time.sleep(.1)
    raise ValueError('旧工作台尚未退出，请检查原服务终端后重试。')
