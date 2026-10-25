"""Inspect File Transfer Helper's media controls; never select, submit or clear files."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge import WeChat
from wxbridge.media_ui import MediaUI
from wxbridge.winapi import click_point, press_shortcut
from wxbridge.windows import rectangle


def dump(uia, node, depth=0):
    if depth > 30:
        return {}
    rect = node.CurrentBoundingRectangle
    return {
        "name": node.CurrentName,
        "class": node.CurrentClassName,
        "id": node.CurrentAutomationId,
        "type": node.CurrentControlType,
        "runtime_id": list(node.GetRuntimeId() or []),
        "rect": [rect.left, rect.top, rect.right, rect.bottom],
        "children": [dump(uia, child, depth + 1) for child in uia.children(node)],
    }


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--open-file-dialog", action="store_true")
    actions.add_argument("--menu-kind", choices=("file", "image", "voice"))
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    with WeChat(enable_accessibility=True, foreground=True) as wx:
        wx.ChatWith("文件传输助手")

        def inspect():
            backend = wx._backend
            media = MediaUI(backend)
            created, menu = None, None
            try:
                if args.open_file_dialog:
                    composer = backend._chat()[2]
                    if backend._pattern(composer, "Value").CurrentValue:
                        raise RuntimeError("Existing draft; no attachment operation performed")
                    if media._windows("#32770"):
                        raise RuntimeError("An existing dialog must be closed first")
                    media._click(backend._find(cls="mmui::XButton", name="发送文件"))
                    created = media._wait_window("#32770")
                elif args.menu_kind:
                    messages = [m for m in backend.snapshot().messages if m.type == args.menu_kind]
                    if not messages or media._windows("mmui::XMenu"):
                        raise RuntimeError("No sample, or an existing menu is open")
                    _, node, message = media._message(messages[-1])
                    left, top, right, bottom = rectangle(node)
                    scale = backend._scale()
                    x = right - 140 * scale if message.attr == "self" else left + 100 * scale
                    click_point(
                        backend.hwnd, x, top + min(24 * scale, (bottom - top) / 2), right=True
                    )
                    menu = media._wait_window("mmui::XMenu")
                handles = [backend.hwnd]
                if created or menu:
                    handles.append(created or menu)
                trees = [dump(backend.uia, backend.uia.root(handle)) for handle in handles]
                output = Path("data/media-probe.json")
                output.parent.mkdir(exist_ok=True)
                output.write_text(json.dumps(trees, ensure_ascii=False, indent=2), encoding="utf-8")
                print("Saved local UI trees:", len(trees))
            finally:
                if created:
                    media._cancel_picker(created)
                if menu and menu in media._windows("mmui::XMenu"):
                    backend._visible(menu)
                    press_shortcut(0x1B)

        wx._pool.submit(inspect).result()


if __name__ == "__main__":
    main()
