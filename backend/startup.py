"""Identify the running workbench before reusing its browser service."""
from __future__ import annotations

import errno
import hashlib
from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from pointcloud_workbench.paths import PROJECT_ROOT

APP_DIR = PROJECT_ROOT
APP_ID = 'pointcloud_workbench'


def source_revision(root=APP_DIR):
    """Hash source/resources in the project layout, excluding user state and caches."""
    root = Path(root)
    suffixes = {'.py', '.html', '.js', '.mjs', '.cjs', '.css', '.bat', '.command'}
    paths = [path for path in root.iterdir()
             if path.is_file() and not path.is_symlink() and path.suffix in suffixes]
    for name in ('backend', 'frontend', 'scripts'):
        directory = root / name
        if directory.is_symlink():
            continue
        for current, directories, filenames in os.walk(directory):
            directories[:] = sorted(name for name in directories
                                    if not name.startswith('.')
                                    and name not in {'__pycache__', 'node_modules', 'outputs', 'captures'})
            for name in filenames:
                path = Path(current) / name
                if path.suffix in suffixes and not path.is_symlink():
                    paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda path: path.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def _connection_refused(exc):
    return isinstance(exc, ConnectionRefusedError) or isinstance(exc, OSError) and (
        exc.errno in {errno.ECONNREFUSED, 10061} or getattr(exc, 'winerror', None) == 10061)


def _connection_closing(exc):
    return isinstance(exc, (ConnectionResetError, ConnectionAbortedError, RemoteDisconnected, TimeoutError)) or (
        isinstance(exc, OSError) and (exc.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
                                                  10053, 10054, 10060}
                                    or getattr(exc, 'winerror', None) in {10053, 10054, 10060}))


def _same_path(actual, expected):
    return isinstance(actual, str) and Path(actual).is_absolute() and (
        os.path.normcase(str(Path(actual).resolve())) == os.path.normcase(str(Path(expected).resolve())))


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
    except (URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if _connection_refused(reason):
            return False
        raise ValueError(f'无法检查端口 {port} 上的服务：{reason}') from exc
    except ValueError as exc:
        raise ValueError(f'端口 {port} 上的服务未返回有效版本信息，请检查原服务终端。') from exc
    if not isinstance(info, dict) or not isinstance(info.get('revision'), str) or not info['revision']:
        raise ValueError(f'端口 {port} 上的服务未返回有效版本信息，请检查原服务终端。')
    if (info.get('app') != APP_ID or not _same_path(info.get('root'), APP_DIR)
            or not _same_path(info.get('state_dir'), state_dir)):
        raise ValueError(f'端口 {port} 已被其他项目或配置占用，请使用 --port 指定其他端口。')
    if info['revision'] == revision:
        return True
    session = request('/api/session')
    if not isinstance(session, dict) or not isinstance(session.get('token'), str) or not session['token']:
        raise ValueError('原服务未返回有效会话，请检查原服务终端。')
    try:
        request('/api/shutdown', {'revision': info['revision']}, session['token'])
    except HTTPError as exc:
        try:
            body = json.load(exc)
            detail = body.get('error', str(exc)) if isinstance(body, dict) else str(exc)
        except ValueError:
            detail = str(exc)
        raise ValueError(f'代码已更新，原服务暂不能重启：{detail}。请稍后重新启动。') from exc
    except (URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if not (_connection_refused(reason) or _connection_closing(reason)):
            raise ValueError(f'代码已更新，无法请求原服务重启：{reason}') from exc
        # A reset can happen after shutdown was accepted. Confirm the port closes below.
    print('检测到代码更新，正在重新启动空闲的点云工作台…', flush=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            request('/api/runtime')
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if _connection_refused(reason):
                return False
            if not _connection_closing(reason):
                raise ValueError(f'无法确认旧工作台已退出：{reason}') from exc
        time.sleep(.1)
    raise ValueError('旧工作台尚未退出，请检查原服务终端后重试。')
