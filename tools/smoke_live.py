"""An explicitly requested, one-shot self-send test. No third-party recipients."""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import win32gui

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge import WeChat
from wxbridge.winapi import find_window


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-self", action="store_true")
    parser.add_argument("--clear-known-draft")
    args = parser.parse_args()
    hwnd, _ = find_window()
    win32gui.ShowWindow(hwnd, 9)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    report = {"checked_at": datetime.now().astimezone().isoformat()}
    try:
        with WeChat(hwnd=hwnd, enable_accessibility=True) as wx:
            report["chat"] = wx.ChatInfo()
            if report["chat"]["chat_name"] != "文件传输助手":
                raise RuntimeError("Self-test only permits File Transfer Helper")
            if args.clear_known_draft:

                def clear_known():
                    backend = wx._backend
                    _, _, composer = backend._chat()
                    value = backend._pattern(composer, "Value")
                    if value.CurrentValue != args.clear_known_draft:
                        raise RuntimeError(
                            "Draft differs from the failed self-test; leaving it intact"
                        )
                    value.SetValue("")

                wx._pool.submit(clear_known).result()
            messages = wx.GetAllMessage()
            report["visible_messages"] = len(messages)
            report["roles"] = dict(Counter(m.attr for m in messages))
            report["baseline_new"] = len(wx.GetNextNewMessage("文件传输助手"))
            if args.send_self:
                text = "wxbridge 自检 " + uuid4().hex[:8]
                report["self_test_text"] = text
                sent = wx.SendMsg(text, who="文件传输助手")
                report["send_status"] = sent.status
                report["self_test_text"] = text
                report["echo_id"] = sent.message_id
            report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        raise
    finally:
        path = Path("data/live-smoke.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
