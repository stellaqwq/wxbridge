import re


class ActionError(Exception):
    def __init__(self, message, retcode=1400, *, data=None):
        super().__init__(message)
        self.retcode = retcode
        self.data = data


def response(data=None, *, error=None):
    if error is None:
        return {"status": "ok", "retcode": 0, "data": data}
    return {
        "status": "failed",
        "retcode": error.retcode,
        "data": error.data,
        "message": str(error),
        "wording": str(error),
    }


def number(value, name, *, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ActionError(f"{name} 必须是正整数")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ActionError(f"{name} 必须是正整数")
    try:
        result = int(value)
    except ValueError as exc:
        raise ActionError(f"{name} 必须是正整数") from exc
    if not 0 < result <= maximum:
        raise ActionError(f"{name} 超出范围")
    return result


def boolean(value):
    if value is True or value == 1 or value in ("true", "yes", "1"):
        return True
    if value is False or value == 0 or value in ("false", "no", "0"):
        return False
    raise ActionError("布尔参数只能使用 true/false、yes/no 或 1/0")


def escape(text):
    return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def unescape(text):
    # Decode exactly once, with ampersand last; literal escaped entities must survive.
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def outgoing_message(message, auto_escape=False):
    """Allow one image or a text-only message; validate everything before a UI action."""
    segments = [message] if isinstance(message, dict) else message
    if isinstance(segments, list) and any(
        isinstance(segment, dict) and segment.get("type") == "image" for segment in segments
    ):
        if len(segments) != 1:
            raise ActionError("图片需要单独发送，请将文字与图片拆成独立 API 调用", 100)
        data = segments[0].get("data")
        if not isinstance(data, dict) or not isinstance(data.get("file"), str) or not data["file"]:
            raise ActionError("image 消息段需要非空 data.file 字符串")
        if data.get("type"):
            raise ActionError("微信不支持 OneBot 闪照等图片类型", 100)
        return "image", data["file"]
    return "text", text_message(message, auto_escape)


def text_message(message, auto_escape=False):
    """Accept the three v11 message forms; reject unsupported segments before sending."""
    if isinstance(message, str):
        if not auto_escape and "[CQ:" in message:
            raise ActionError("当前适配层只支持纯文本，不支持 CQ 图片、引用等消息段", 100)
        text = message if auto_escape else unescape(message)
    else:
        segments = [message] if isinstance(message, dict) else message
        if not isinstance(segments, list) or len(segments) > 4000:
            raise ActionError("message 需要字符串、消息段对象或消息段数组")
        parts = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ActionError("消息段必须是对象")
            if segment.get("type") != "text":
                raise ActionError("当前适配层只支持 text 消息段", 100)
            data = segment.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise ActionError("text 消息段需要 data.text 字符串")
            parts.append(data["text"])
        text = "".join(parts)
    if not text.strip() or len(text) > 4000:
        raise ActionError("文字必须非空且不超过 4000 字符")
    return text
