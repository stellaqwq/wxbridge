"""Small, typed wrappers around public Win32 process and window APIs."""

import ctypes as C
from ctypes import wintypes as W
from pathlib import Path

from .errors import WeChatError


class MODULEENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", W.DWORD),
        ("th32ModuleID", W.DWORD),
        ("th32ProcessID", W.DWORD),
        ("GlblcntUsage", W.DWORD),
        ("ProccntUsage", W.DWORD),
        ("modBaseAddr", C.c_void_p),
        ("modBaseSize", W.DWORD),
        ("hModule", W.HMODULE),
        ("szModule", W.WCHAR * 256),
        ("szExePath", W.WCHAR * 260),
    ]


def kernel():
    k = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "ReadProcessMemory": (
            [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)],
            W.BOOL,
        ),
        "WriteProcessMemory": (
            [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)],
            W.BOOL,
        ),
        "CreateToolhelp32Snapshot": ([W.DWORD, W.DWORD], W.HANDLE),
        "Module32FirstW": ([W.HANDLE, C.POINTER(MODULEENTRY32W)], W.BOOL),
        "Module32NextW": ([W.HANDLE, C.POINTER(MODULEENTRY32W)], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(k, name)
        fn.argtypes, fn.restype = args, result
    return k


def weixin_module(pid):
    k = kernel()
    handle = k.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = C.sizeof(entry)
        ok = k.Module32FirstW(handle, C.byref(entry))
        while ok:
            if entry.szModule.lower() == "weixin.dll":
                return entry.modBaseAddr, entry.modBaseSize, Path(entry.szExePath)
            ok = k.Module32NextW(handle, C.byref(entry))
    finally:
        k.CloseHandle(handle)
    raise WeChatError("目标进程没有加载 Weixin.dll")


class ProcessMemory:
    def __init__(self, pid, *, writable=False):
        self.k = kernel()
        self.handle = self.k.OpenProcess(
            0x1000 | 0x10 | (0x20 | 0x8 if writable else 0), False, pid
        )
        if not self.handle:
            raise C.WinError(C.get_last_error())

    def read(self, address, size):
        buffer = C.create_string_buffer(size)
        count = C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, address, buffer, size, C.byref(count)):
            raise C.WinError(C.get_last_error())
        if count.value != size:
            raise WeChatError("读取的进程内存长度不完整")
        return buffer.raw

    def write_byte(self, address, value):
        buffer = C.c_ubyte(value)
        count = C.c_size_t()
        if not self.k.WriteProcessMemory(self.handle, address, C.byref(buffer), 1, C.byref(count)):
            raise C.WinError(C.get_last_error())
        if count.value != 1 or self.read(address, 1) != bytes([value]):
            raise WeChatError("无障碍状态写入未通过回读校验")

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def find_window(hwnd=None):
    import win32gui
    import win32process

    if hwnd is not None:
        if not win32gui.IsWindow(hwnd):
            raise WeChatError("微信窗口已经关闭")
        return hwnd, win32process.GetWindowThreadProcessId(hwnd)[1]
    found = []

    def visit(handle, _):
        if win32gui.IsWindowVisible(handle) and win32gui.GetWindowText(handle) in (
            "微信",
            "Weixin",
            "WeChat",
        ):
            cls = win32gui.GetClassName(handle)
            if cls == "mmui::MainWindow" or (cls.startswith("Qt") and cls.endswith("QWindowIcon")):
                found.append(handle)

    win32gui.EnumWindows(visit, None)
    if len(found) != 1:
        raise WeChatError(f"需要唯一微信主窗口，当前找到 {len(found)} 个；可显式传入 hwnd")
    return found[0], win32process.GetWindowThreadProcessId(found[0])[1]


def press_enter():
    """Submit one atomic down/up pair through the public Windows input API."""
    _press_key(0x0D)


def _press_key(virtual_key):
    press_shortcut(virtual_key)


def press_shortcut(*keys):
    """Send a complete shortcut in one SendInput call; reject held modifiers."""
    import win32api

    for key in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        if win32api.GetAsyncKeyState(key) & 0x8000:
            raise WeChatError("检测到修饰键按下，已取消提交")

    class MouseInput(C.Structure):
        _fields_ = [
            ("dx", W.LONG),
            ("dy", W.LONG),
            ("mouseData", W.DWORD),
            ("dwFlags", W.DWORD),
            ("time", W.DWORD),
            ("dwExtraInfo", C.c_size_t),
        ]

    class KeyboardInput(C.Structure):
        _fields_ = [
            ("wVk", W.WORD),
            ("wScan", W.WORD),
            ("dwFlags", W.DWORD),
            ("time", W.DWORD),
            ("dwExtraInfo", C.c_size_t),
        ]

    class Payload(C.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]

    class Input(C.Structure):
        _fields_ = [("type", W.DWORD), ("payload", Payload)]

    if not keys or any(not isinstance(key, int) or not 0 < key < 256 for key in keys):
        raise ValueError("无效的快捷键")
    events = (Input * (len(keys) * 2))()
    sequence = [(key, 0) for key in keys] + [(key, 2) for key in reversed(keys)]
    for index, (key, flags) in enumerate(sequence):
        events[index].type = 1
        events[index].payload.ki = KeyboardInput(key, 0, flags, 0, 0)
    user = C.WinDLL("user32", use_last_error=True)
    user.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
    user.SendInput.restype = W.UINT
    if user.SendInput(len(events), events, C.sizeof(Input)) != len(events):
        raise WeChatError("系统没有完整接收提交按键")


def click_point(hwnd, x, y, *, right=False, double=False, foreground_hwnd=None):
    """Click only an unobscured point in the expected foreground window."""
    import win32api
    import win32gui

    point = (int(x), int(y))
    expected_foreground = hwnd if foreground_hwnd is None else foreground_hwnd
    if foreground_root() != expected_foreground:
        raise WeChatError("目标窗口失去前台，已取消点击")
    target = win32gui.WindowFromPoint(point)
    if not target or win32gui.GetAncestor(target, 2) != hwnd:
        raise WeChatError("目标控件被遮挡，已取消点击")
    for key in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        if win32api.GetAsyncKeyState(key) & 0x8000:
            raise WeChatError("检测到修饰键按下，已取消点击")
    win32api.SetCursorPos(point)
    if foreground_root() != expected_foreground:
        raise WeChatError("移动鼠标期间窗口改变，已取消点击")
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    for _ in range(2 if double else 1):
        win32api.mouse_event(down, 0, 0, 0, 0)
        win32api.mouse_event(up, 0, 0, 0, 0)


def show_window(hwnd):
    import time

    import win32gui

    win32gui.ShowWindow(hwnd, 9)
    deadline = time.monotonic() + 0.5
    while foreground_root() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if foreground_root() == hwnd:
        return
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Explicit foreground=True callers consent to a normal desktop activation.
        # Windows releases its foreground lock on Alt; never type text into the old app.
        _press_key(0x12)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as exc:
            raise WeChatError("Windows 拒绝激活微信，请手动将微信置于前台") from exc
    deadline = time.monotonic() + 0.3
    while foreground_root() != hwnd and time.monotonic() < deadline:
        time.sleep(0.02)
    if foreground_root() != hwnd:
        raise WeChatError("微信仍未获得前台焦点，请手动激活窗口")


def foreground_root():
    import win32gui

    handle = win32gui.GetForegroundWindow()
    if not handle or not win32gui.IsWindow(handle):
        return None
    try:
        return win32gui.GetAncestor(handle, 2)
    except Exception:
        return None


def owned_by(hwnd, owner):
    """Qt tool windows need an explicit GW_OWNER walk, not GA_ROOTOWNER."""
    import win32gui

    visited = set()
    while hwnd and hwnd not in visited:
        if hwnd == owner:
            return True
        visited.add(hwnd)
        hwnd = win32gui.GetWindow(hwnd, 4)
    return False
