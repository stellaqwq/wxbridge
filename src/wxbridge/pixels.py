"""Conservative source classification for the verified default light theme.

Text itself always comes from UIA. Unsupported colours/layouts remain 'other'.
"""

from dataclasses import dataclass


@dataclass
class Frame:
    left: int
    top: int
    width: int
    height: int
    data: bytes

    def rgb(self, x, y):
        x, y = int(x) - self.left, int(y) - self.top
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        at = (y * self.width + x) * 4
        b, g, r = self.data[at : at + 3]
        return r, g, b


def capture(rect):
    import ctypes as C
    from ctypes import wintypes as W

    import win32con
    import win32gui
    import win32ui

    left, top, right, bottom = map(int, rect)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0 or width * height > 16000000:
        raise ValueError("截图区域尺寸无效")
    handle = win32gui.GetDC(0)
    source = win32ui.CreateDCFromHandle(handle)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        previous = target.SelectObject(bitmap)
        target.BitBlt((0, 0), (width, height), source, (left, top), win32con.SRCCOPY)
        target.SelectObject(previous)

        # Request a top-down 32-bit DIB explicitly; DDB byte order is driver-dependent.
        class Header(C.Structure):
            _fields_ = [
                ("size", W.DWORD),
                ("width", W.LONG),
                ("height", W.LONG),
                ("planes", W.WORD),
                ("bits", W.WORD),
                ("compression", W.DWORD),
                ("image_size", W.DWORD),
                ("xppm", W.LONG),
                ("yppm", W.LONG),
                ("used", W.DWORD),
                ("important", W.DWORD),
            ]

        info = Header(40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        buffer = C.create_string_buffer(width * height * 4)
        gdi = C.WinDLL("gdi32", use_last_error=True)
        gdi.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT]
        gdi.GetDIBits.restype = C.c_int
        rows = gdi.GetDIBits(
            source.GetSafeHdc(), bitmap.GetHandle(), 0, height, buffer, C.byref(info), 0
        )
        if rows != height:
            raise OSError("无法读取完整的窗口像素")
        return Frame(left, top, width, height, buffer.raw)
    finally:
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(0, handle)
        win32gui.DeleteObject(bitmap.GetHandle())


def classify(frame, row, scale=1.0):
    left, top, right, bottom = row
    if top < frame.top or bottom > frame.top + frame.height:
        return "other"
    margin = int(56 * scale)
    green = 0
    for y in range(int(top + 8 * scale), int(bottom - 8 * scale), 3):
        for x in range(int(left + margin), int(right - margin), 3):
            color = frame.rgb(x, y)
            if color:
                r, g, b = color
                green += 120 < g < 252 and g > r + 30 and g > b + 30
    if green >= max(16, int(16 * scale * scale)):
        return "self"
    reference = frame.rgb(left + 12 * scale, top + 12 * scale)
    if not reference or not (
        230 <= min(reference) <= max(reference) <= 251 and max(reference) - min(reference) <= 3
    ):
        return "other"
    surface = 0
    for y in range(int(top + 12 * scale), int(min(bottom - 8 * scale, top + 65 * scale)), 3):
        for x in range(int(left + 64 * scale), int(min(right - margin, left + 110 * scale)), 3):
            color = frame.rgb(x, y)
            if color:
                neutral = min(color) >= 230 and max(color) - min(color) <= 4
                contrast = abs(sum(color) - sum(reference)) >= 12
                surface += neutral and contrast
    return "friend" if surface >= max(16, int(16 * scale * scale)) else "other"


def classify_media(frame, row, scale=1.0):
    """Use avatar gutters, never colours inside an image, to infer its side."""
    left, top, right, bottom = row
    if top < frame.top or bottom > frame.top + frame.height:
        return "other"
    reference = frame.rgb(left + 12 * scale, top + 6 * scale)
    if not reference or min(reference) < 230 or max(reference) - min(reference) > 4:
        return "other"
    counts = []
    for x0 in (left + 24 * scale, right - 56 * scale):
        count = 0
        for y in range(int(top + 8 * scale), int(min(bottom - 4, top + 40 * scale)), 2):
            for x in range(int(x0), int(x0 + 28 * scale), 2):
                color = frame.rgb(x, y)
                if color and sum(abs(a - b) for a, b in zip(color, reference, strict=True)) >= 12:
                    count += 1
        counts.append(count)
    threshold = max(20, int(20 * scale * scale))
    if counts[0] >= threshold and counts[1] <= counts[0] / 5:
        return "friend"
    if counts[1] >= threshold and counts[0] <= counts[1] / 5:
        return "self"
    return "other"


def avatar_fingerprint(frame, row, scale=1.0):
    """A local visual alias for a visible left avatar; never a WeChat account ID."""
    import hashlib

    left, top, _, bottom = row
    if top < frame.top or bottom > frame.top + frame.height:
        return ""
    samples = bytearray()
    for dy in range(12, 36, 3):
        for dx in range(26, 50, 3):
            color = frame.rgb(int(left + dx * scale), int(top + dy * scale))
            if color is None:
                return ""
            samples.extend(channel // 16 for channel in color)
    return hashlib.sha256(samples).hexdigest()[:32]
