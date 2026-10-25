"""Read-only diagnosis of source classification; does not enable or submit anything."""

import argparse
import ctypes
import json
import struct
import sys
from collections import Counter
from pathlib import Path

import win32gui

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge.accessibility import AccessibilityLease
from wxbridge.pixels import capture, classify
from wxbridge.uia import UIA
from wxbridge.winapi import find_window
from wxbridge.windows import rectangle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-accessibility", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    hwnd, pid = find_window()
    if args.show:
        win32gui.SetForegroundWindow(hwnd)
    uia = UIA()
    lease = AccessibilityLease(pid, hwnd, uia)
    try:
        if args.enable_accessibility:
            lease.enable()
        message_list = next(
            n for n in uia.nodes(hwnd) if n.CurrentAutomationId == "chat_message_list"
        )
        frame = capture(rectangle(message_list))
        data = frame.data
        header = b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54)
        header += struct.pack(
            "<IiiHHIIiiII", 40, frame.width, -frame.height, 1, 32, 0, len(data), 0, 0, 0, 0
        )
        Path("data/role-frame.bmp").write_bytes(header + data)
        rows = []
        for node in uia.children(message_list):
            if node.CurrentClassName != "mmui::ChatTextItemView":
                continue
            rect = rectangle(node)
            left, top, right, bottom = rect
            colors = Counter(
                frame.rgb(x, y)
                for y in range(max(frame.top, top), min(frame.top + frame.height, bottom), 2)
                for x in range(left + 56, right - 56, 2)
            )
            rows.append(
                {
                    "prefix": node.CurrentName.startswith("/ai "),
                    "chars": len(node.CurrentName),
                    "role": classify(frame, rect),
                    "rect": rect,
                    "colors": [(str(color), count) for color, count in colors.most_common(8)],
                }
            )
        print(
            json.dumps(
                {"foreground": win32gui.GetForegroundWindow(), "hwnd": hwnd, "rows": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        lease.close()
        uia.close()


if __name__ == "__main__":
    main()
