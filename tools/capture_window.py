import ctypes
import sys
from pathlib import Path

import win32gui
import win32ui

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge.winapi import find_window

hwnd, _ = find_window()
left, top, right, bottom = win32gui.GetWindowRect(hwnd)
width, height = right - left, bottom - top
source = win32gui.GetWindowDC(hwnd)
dc = win32ui.CreateDCFromHandle(source)
memory = dc.CreateCompatibleDC()
bitmap = win32ui.CreateBitmap()
bitmap.CreateCompatibleBitmap(dc, width, height)
memory.SelectObject(bitmap)
ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2)
bitmap.SaveBitmapFile(memory, str(Path("data/print-window.bmp").resolve()))
print("window", [left, top, right, bottom], "foreground", win32gui.GetForegroundWindow())
memory.DeleteDC()
dc.DeleteDC()
win32gui.ReleaseDC(hwnd, source)
win32gui.DeleteObject(bitmap.GetHandle())
