"""Media metadata and local path validation. No client memory or database scraping."""

import re
from pathlib import Path, PureWindowsPath

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def voice_details(name):
    """Only accept a voice header and a duration; never infer from ordinary text."""
    match = re.match(r'^语音(?:消息)?[\s，,:：]*(\d{1,3})\s*(?P<unit>["″]\s*秒?|秒)', name)
    if not match:
        return {}
    details = {"duration_seconds": int(match[1])}
    tail = name[match.end() :].strip(" \n\r，,:：")
    transcript = re.match(r"^(?:转文字(?:结果)?|文字内容)[：:]\s*(.+)", tail, re.S)
    if transcript:
        tail = transcript[1].strip()
    elif not re.fullmatch(r'["″]\s*秒', match["unit"]):
        tail = ""
    status = re.fullmatch(
        r"(?:未读|已读|播放|播放中|转换中|转文字中|正在转换|正在转文字|无法转换|转换失败)[.。…]*",
        tail,
    )
    if tail and not status:
        details["transcript"] = tail
    return details


def message_details(cls, name):
    classes = {
        "mmui::ChatTextItemView": "text",
        "mmui::ChatImageItemView": "image",
        "mmui::ChatFileItemView": "file",
        "mmui::ChatVideoItemView": "video",
        "mmui::ChatVoiceItemView": "voice",
    }
    kind, info = classes.get(cls, "other"), {}
    # Native GIFs use this wrapper and a fixed UI label, unlike quoted text.
    if cls == "mmui::ChatBubbleReferItemView" and name == "动画表情":
        kind, info = "image", {"animated": True}
    lines = name.splitlines()
    if cls == "mmui::ChatBubbleItemView" and len(lines) >= 3 and lines[0] == "文件":
        kind = "file"
        info = {"filename": lines[1], "display_size": lines[2]}
    voice = voice_details(name)
    if kind == "voice" or (cls == "mmui::ChatBubbleItemView" and voice):
        kind, info = "voice", voice
    return kind, info


def identity_content(kind, content, info):
    if kind == "voice":
        # Conversion/unread-state changes must not replay the same voice message.
        return "voice:" + str(info.get("duration_seconds", "unknown"))
    return content


def local_attachment(path, *, image=False):
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("附件必须是本地普通文件")
    if '"' in str(resolved) or "\n" in str(resolved) or "\r" in str(resolved):
        raise ValueError("附件路径包含不支持的字符")
    kind = "image" if resolved.suffix.lower() in IMAGE_SUFFIXES else "file"
    if image and kind != "image":
        raise ValueError("SendImage 需要 PNG/JPG/BMP/GIF/WebP 图片")
    stat = resolved.stat()
    return resolved, kind, (stat.st_size, stat.st_mtime_ns)


def safe_filename(name):
    name = PureWindowsPath(name).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = "attachment"
    if name.split(".")[0].rstrip(" .").upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    if len(name) > 160:
        suffix = Path(name).suffix[:16]
        name = name[: 160 - len(suffix)] + suffix
    return name
