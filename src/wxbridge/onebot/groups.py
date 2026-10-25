"""OneBot group aliases and visible @-name conversion, without claiming server wxids."""

import re

from .protocol import escape


def mention_segments(text, name, self_id):
    if not name:
        return [{"type": "text", "data": {"text": text}}], escape(text)
    pattern = re.compile(r"@" + re.escape(name) + r"(?=$|[\s\u2005\u2006\u00a0])")
    segments = []
    raw = []
    start = 0
    for match in pattern.finditer(text):
        if match.start() > start:
            value = text[start : match.start()]
            segments.append({"type": "text", "data": {"text": value}})
            raw.append(escape(value))
        segments.append({"type": "at", "data": {"qq": str(self_id)}})
        raw.append(f"[CQ:at,qq={self_id}]")
        start = match.end()
    if start < len(text):
        segments.append({"type": "text", "data": {"text": text[start:]}})
        raw.append(escape(text[start:]))
    return segments, "".join(raw)


def member_info(group_id, uid, name, last_seen=0):
    return {
        "group_id": group_id,
        "user_id": uid,
        "nickname": name,
        "card": name,
        "sex": "unknown",
        "age": 0,
        "area": "",
        "join_time": 0,
        "last_sent_time": last_seen,
        "level": "",
        "role": "member",
        "unfriendly": False,
        "title": "",
        "title_expire_time": 0,
        "card_changeable": False,
    }
