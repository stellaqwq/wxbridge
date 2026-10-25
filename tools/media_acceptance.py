"""Bounded media acceptance test, restricted to the user's File Transfer Helper."""

import argparse
import hashlib
import json
import struct
import sys
import zlib
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge import WeChat


def png_bytes():
    def chunk(kind, payload):
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    pixels = b"".join(
        b"\x00" + b"".join(bytes((x * 4, y * 4, 128)) for x in range(64)) for y in range(48)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-self", action="store_true")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    token = uuid4().hex[:8]
    root = Path(__file__).resolve().parents[1]
    directory = root / "data" / "media-test"
    directory.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now().astimezone().isoformat(), "sent": [], "saved": []}
    try:
        with WeChat(enable_accessibility=True, foreground=True, send_timeout=8) as wx:
            wx.ChatWith("文件传输助手")
            if args.send_self:
                text = directory / ("wxbridge-file-" + token + ".txt")
                text.write_text("wxbridge synthetic media acceptance " + token, encoding="utf-8")
                image = directory / ("wxbridge-image-" + token + ".png")
                image.write_bytes(png_bytes())
                for source, sender in ((text, wx.SendFile), (image, wx.SendImage)):
                    result = sender(source, who="文件传输助手")
                    report["sent"].append(asdict(result))
                    print("SEND_CONFIRMED:", result.kind, result.verification, flush=True)
            messages = wx.GetAllMessage()
            report["visible_types"] = dict(Counter(m.type for m in messages))
            report["visible_roles"] = dict(Counter(m.attr for m in messages))
            if args.download:
                # Save the latest visible file and image, without opening either.
                for kind in ("file", "image"):
                    matches = [m for m in wx.GetAllMessage() if m.type == kind]
                    if not matches:
                        raise RuntimeError("No visible " + kind + " sample")
                    saved = wx.DownloadAttachment(matches[-1], directory / "received", timeout=10)
                    data = saved.read_bytes()
                    report["saved"].append(
                        {
                            "kind": kind,
                            "path": str(saved),
                            "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    )
                    print("SAVED:", kind, len(data), "bytes", flush=True)
            report["ok"] = True
    except Exception as exc:
        report.update(ok=False, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        output = root / "data" / ("media-acceptance-" + token + ".json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
