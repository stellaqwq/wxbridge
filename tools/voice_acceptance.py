"""Read-only voice-to-text acceptance in File Transfer Helper; never sends."""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from probe_media import dump

from wxbridge import WeChat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    report_path = root / "data" / ("voice-acceptance-" + uuid4().hex[:8] + ".json")
    report = {"started_at": datetime.now().astimezone().isoformat(), "sent_count": 0}
    try:
        with WeChat(enable_accessibility=True, foreground=True) as wx:
            wx.ChatWith("文件传输助手")
            print("READY: File Transfer Helper voice transcription only; no replies", flush=True)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                messages = wx.GetAllMessage()
                candidates = [
                    message
                    for message in messages
                    if message.type == "voice"
                    or "Voice" in message.info.get("class", "")
                    or "Audio" in message.info.get("class", "")
                    or (message.type == "other" and message.content.startswith("语音"))
                ]
                if candidates:
                    message = candidates[-1]
                    report.update(
                        voice_class=message.info.get("class"),
                        recognized_type=message.type,
                        role=message.attr,
                    )
                    tree = wx._pool.submit(
                        lambda: dump(wx._backend.uia, wx._backend.uia.root(wx._backend.hwnd))
                    ).result()
                    (root / "data/voice-tree.json").write_text(
                        json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print("VOICE_OBSERVED:", message.info.get("class"), message.type, flush=True)
                    text = wx.TranscribeVoice(message, timeout=30)
                    report.update(state="transcribed", transcript_chars=len(text))
                    report["transcript"] = text
                    print("TRANSCRIBED:", len(text), "characters", flush=True)
                    break
                time.sleep(0.5)
            else:
                report["state"] = "timed_out_without_voice"
    except Exception as exc:
        report.update(state="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        public = {key: value for key, value in report.items() if key != "transcript"}
        print(json.dumps(public, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
