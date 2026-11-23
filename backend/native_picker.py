"""Native folder dialogs in a helper process, never a browser file upload."""
from pathlib import Path
import json
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


def choose_directory(value=None, title='选择文件夹'):
    if not supported():
        raise ValueError('当前原生目录选择支持 macOS 和 Windows；其他系统可直接填写路径')
    if not PICKER_LOCK.acquire(blocking=False):
        raise ValueError('已有文件夹选择窗口，请先选择或取消')
    try:
        initial = Path(value).expanduser() if isinstance(value, str) and value.strip() else Path.home()
        while not initial.is_dir() and initial != initial.parent:
            initial = initial.parent
        if sys.platform == 'darwin':
            command = ['/usr/bin/osascript', '-l', 'JavaScript', '-e', MAC_SCRIPT,
                       str(title)[:200], str(initial.resolve())]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), '--windows-helper',
                       str(title)[:200], str(initial.resolve())]
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


def windows_folder(title, initial):
    """Windows Common Item Dialog (IFileOpenDialog + FOS_PICKFOLDERS)."""
    import ctypes as c
    import uuid
    from ctypes import wintypes as w

    class GUID(c.Structure):
        _fields_ = [('data', c.c_ubyte*16)]
        @classmethod
        def parse(cls, value):
            return cls((c.c_ubyte*16).from_buffer_copy(uuid.UUID(value).bytes_le))

    ole, shell = c.WinDLL('ole32'), c.WinDLL('shell32')
    hr = c.c_long
    ole.CoInitializeEx.argtypes = [c.c_void_p, w.DWORD]
    ole.CoInitializeEx.restype = hr
    ole.CoCreateInstance.argtypes = [c.POINTER(GUID), c.c_void_p, w.DWORD, c.POINTER(GUID), c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = hr
    ole.CoTaskMemFree.argtypes = [c.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [w.LPCWSTR, c.c_void_p, c.POINTER(GUID), c.POINTER(c.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = hr

    def check(result):
        if result < 0:
            raise OSError(f'Windows 系统目录窗口错误：0x{result & 0xffffffff:08x}')

    def call(obj, index, types, *args):
        vtable = c.cast(obj, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(hr, c.c_void_p, *types)(vtable[index])(obj, *args)

    check(ole.CoInitializeEx(None, 2))  # STA; helper process has no previous apartment.
    dialog, folder, result_item = c.c_void_p(), c.c_void_p(), c.c_void_p()
    name = c.c_void_p()
    try:
        clsid = GUID.parse('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
        iid = GUID.parse('D57C7288-D4AD-4768-BE02-9D969532D960')
        shell_iid = GUID.parse('43826D1E-E718-42EE-BC55-A1E261C37BFE')
        check(ole.CoCreateInstance(c.byref(clsid), None, 1, c.byref(iid), c.byref(dialog)))
        options = w.DWORD()
        check(call(dialog, 10, [c.POINTER(w.DWORD)], c.byref(options)))
        check(call(dialog, 9, [w.DWORD], options.value | 0x20 | 0x40 | 0x800 | 0x2000000))
        check(call(dialog, 17, [w.LPCWSTR], title))
        if shell.SHCreateItemFromParsingName(initial, None, c.byref(shell_iid), c.byref(folder)) >= 0:
            check(call(dialog, 12, [c.c_void_p], folder))
        status = call(dialog, 3, [w.HWND], None)
        if status & 0xffffffff == 0x800704c7:  # User cancellation.
            return None
        check(status)
        check(call(dialog, 20, [c.POINTER(c.c_void_p)], c.byref(result_item)))
        check(call(result_item, 5, [w.DWORD, c.POINTER(c.c_void_p)], 0x80058000, c.byref(name)))
        return c.wstring_at(name)
    finally:
        if name: ole.CoTaskMemFree(name)
        for item in [result_item, folder, dialog]:
            if item: call(item, 2, [])
        ole.CoUninitialize()


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--windows-helper':
    # ASCII JSON is independent of the active Windows console code page.
    print(json.dumps({'path': windows_folder(sys.argv[2], sys.argv[3])}, ensure_ascii=True))
