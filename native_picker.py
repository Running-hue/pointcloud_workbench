"""Native folder dialogs in a helper process, never a browser file upload."""
from pathlib import Path
import json
import queue
import subprocess
import sys
import threading

PICKER_LOCK = threading.Lock()
MAC_SCRIPT = r'''
ObjC.import('AppKit');
function run(argv) {
    var app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    var panel = $.NSOpenPanel.openPanel;
    panel.canChooseFiles = false;
    panel.canChooseDirectories = true;
    panel.allowsMultipleSelection = false;
    panel.canCreateDirectories = true;
    panel.title = argv[0];
    panel.prompt = '选择文件夹';
    panel.directoryURL = $.NSURL.fileURLWithPath(argv[1]);
    app.activateIgnoringOtherApps(true);
    if (panel.runModal === $.NSModalResponseOK) return ObjC.unwrap(panel.URL.path);
    return '';
}
'''


def supported():
    return sys.platform in {'darwin', 'win32'}


def initial_directory(value=None):
    """Ignore paths copied from another OS instead of starting at a drive root."""
    from pathlib import PurePosixPath, PureWindowsPath
    value = value.strip() if isinstance(value, str) else ''
    initial = Path(value).expanduser() if value else Path.home()
    if not initial.is_absolute() and (PurePosixPath(value or '').is_absolute() or
                                      PureWindowsPath(value or '').is_absolute()):
        initial = Path.home()
    while not initial.is_dir() and initial != initial.parent:
        initial = initial.parent
    return initial.resolve()


def run_windows_picker(command):
    """Bound initialization separately from the time spent choosing a folder."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    ready = queue.Queue()
    def read_ready():
        try:
            ready.put(process.stdout.readline())
        except (OSError, ValueError):
            ready.put('')
    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        try:
            line = ready.get(timeout=15)
        except queue.Empty as exc:
            raise ValueError('系统目录窗口启动超时。请关闭工作台服务，在本机双击“启动点云工作台.bat”重新启动；也可直接填写目录。') from exc
        if line.strip() != '{"ready": true}':
            _, error = process.communicate(timeout=5)
            raise ValueError('系统文件夹窗口未能打开：' + error.strip()[:500])
        output, error = process.communicate(timeout=600)
        return subprocess.CompletedProcess(command, process.returncode, output, error)
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(timeout=1)
        process.communicate()


def choose_directory(value=None, title='选择文件夹'):
    if not supported():
        raise ValueError('当前原生目录选择支持 macOS 和 Windows；其他系统可直接填写路径')
    if not PICKER_LOCK.acquire(blocking=False):
        raise ValueError('已有文件夹选择窗口，请先选择或取消')
    try:
        initial = initial_directory(value)
        if sys.platform == 'darwin':
            command = ['/usr/bin/osascript', '-l', 'JavaScript', '-e', MAC_SCRIPT,
                       str(title)[:200], str(initial.resolve())]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), '--windows-helper',
                       str(title)[:200], str(initial.resolve())]
        if sys.platform == 'win32':
            result = run_windows_picker(command)
        else:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    timeout=600, check=False)
        if result.returncode:
            raise ValueError('系统文件夹窗口未能打开：'+result.stderr.strip()[:500])
        selected = result.stdout.rstrip('\r\n') if sys.platform == 'darwin' else json.loads(result.stdout)['path']
        if not selected:
            return {'cancelled': True, 'path': None}
        path = Path(selected).resolve()
        if not path.is_dir():
            raise ValueError('所选文件夹不存在')
        return {'cancelled': False, 'path': str(path)}
    except subprocess.TimeoutExpired as exc:
        raise ValueError('选择文件夹超时，请重新选择') from exc
    finally:
        PICKER_LOCK.release()


def windows_folder(title, initial, on_ready=None):
    """Open the Windows shell folder picker on the helper's main GUI thread."""
    import ctypes as c
    from ctypes import wintypes as w

    shell = c.WinDLL('shell32', use_last_error=True)
    ole = c.WinDLL('ole32', use_last_error=True)
    user = c.WinDLL('user32', use_last_error=True)
    callback_type = c.WINFUNCTYPE(c.c_int, w.HWND, w.UINT, w.LPARAM, w.LPARAM)

    class BROWSEINFO(c.Structure):
        _fields_ = [('hwndOwner', w.HWND), ('pidlRoot', c.c_void_p),
                    ('pszDisplayName', w.LPWSTR), ('lpszTitle', w.LPCWSTR),
                    ('ulFlags', w.UINT), ('lpfn', callback_type),
                    ('lParam', w.LPARAM), ('iImage', c.c_int)]

    shell.SHBrowseForFolderW.argtypes = [c.POINTER(BROWSEINFO)]
    shell.SHBrowseForFolderW.restype = c.c_void_p
    shell.SHGetPathFromIDListEx.argtypes = [c.c_void_p, w.LPWSTR, w.DWORD, w.DWORD]
    shell.SHGetPathFromIDListEx.restype = w.BOOL
    ole.CoInitializeEx.argtypes = [c.c_void_p, w.DWORD]
    ole.CoInitializeEx.restype = c.c_long
    ole.CoTaskMemFree.argtypes = [c.c_void_p]
    ole.CoTaskMemFree.restype = None
    user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.SendMessageW.restype = w.LPARAM
    user.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
    start = c.create_unicode_buffer(initial)

    @callback_type
    def initialized(hwnd, message, param, data):
        if message == 1:  # BFFM_INITIALIZED: the dialog really exists now.
            user.SetWindowTextW(hwnd, title)
            if on_ready:
                on_ready()
            user.SendMessageW(hwnd, 0x467, 1, c.cast(start, c.c_void_p).value)
            user.SetWindowPos(hwnd, w.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user.SetForegroundWindow(hwnd)
        return 0

    status = ole.CoInitializeEx(None, 2)
    if status < 0:
        raise OSError(f'Windows 目录窗口初始化失败：0x{status & 0xffffffff:08x}')
    item = None
    try:
        display = c.create_unicode_buffer(260)
        info = BROWSEINFO(None, None, c.cast(display, w.LPWSTR), title, 0x0001 | 0x0010 | 0x0040,
                          initialized, 0, 0)
        item = shell.SHBrowseForFolderW(c.byref(info))
        if not item:
            return None
        selected = c.create_unicode_buffer(32768)
        if not shell.SHGetPathFromIDListEx(item, selected, len(selected), 0):
            raise ValueError('请选择本机或网络上的文件夹')
        return selected.value
    finally:
        if item:
            ole.CoTaskMemFree(item)
        ole.CoUninitialize()


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--windows-helper':
    # ASCII JSON is independent of the active Windows console code page.
    def ready():
        print(json.dumps({'ready': True}), flush=True)
    try:
        selected = windows_folder(sys.argv[2], sys.argv[3], ready)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
    print(json.dumps({'path': selected}, ensure_ascii=True), flush=True)
