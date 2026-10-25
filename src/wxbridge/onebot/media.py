"""Registered attachment downloads for OneBot consumers, independent of API credentials."""

import base64
import binascii
import logging
import mimetypes
import secrets
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from .protocol import ActionError

MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
log = logging.getLogger(__name__)


def image_suffix(data):
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"BM", ".bmp"),
    )
    for signature, suffix in signatures:
        if data.startswith(signature):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ActionError("图片内容不是支持的 PNG/JPEG/GIF/BMP/WebP 格式", 100)


def wechat_image(data):
    """Weixin's file picker treats WebP as a file; convert the actual bytes, not its suffix."""
    suffix = image_suffix(data)
    if suffix != ".webp":
        return data, suffix
    from PIL import Image

    output = BytesIO()
    frames = []
    try:
        with Image.open(BytesIO(data), formats=["WEBP"]) as source:
            count = getattr(source, "n_frames", 1)
            pixels = source.width * source.height
            if pixels > 16_000_000 or count > 200 or pixels * count > 32_000_000:
                raise ActionError("WebP 图片尺寸或动画帧数过大，请缩小后发送", 100)
            if count == 1:
                with source.convert("RGBA") as frame:
                    frame.save(output, format="PNG")
                suffix = ".png"
            else:
                durations = []
                for index in range(count):
                    source.seek(index)
                    frames.append(source.convert("RGBA"))
                    durations.append(max(10, source.info.get("duration", 100)))
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=source.info.get("loop", 0),
                    disposal=2,
                )
                suffix = ".gif"
        converted = output.getvalue()
        if len(converted) > MAX_IMAGE_BYTES:
            raise ActionError("WebP 转换后的图片超过 8 MiB，请缩小后发送", 100)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ActionError("WebP 图片解码或转换失败，未提交微信发送", 100) from exc
    finally:
        for frame in frames:
            frame.close()
        output.close()
    log.info("WebP 已转换为 %s，保留 %s 帧后交给微信图片发送", suffix[1:].upper(), count)
    return converted, suffix


class MediaCache:
    def __init__(self, config, store):
        self.store = store
        self.directory = config.database.with_name(config.database.stem + "-media").resolve()
        host = config.host if config.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        self.base_url = config.media_base_url.rstrip("/") or f"http://{host}:{config.port}"

    def prepare_image(self, reference):
        if reference.startswith("base64://"):
            encoded = reference[len("base64://") :]
            if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ActionError("发送图片超过 8 MiB，请压缩后发送", 100)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ActionError("图片 Base64 编码无效", 100) from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ActionError("发送图片为空或超过 8 MiB", 100)
            return self._save_image(data)
        # Arbitrary paths and URLs are not opened. AstrBot already sends base64.
        asset = self.describe(reference)
        if asset["size"] > MAX_IMAGE_BYTES:
            raise ActionError("发送图片超过 8 MiB，请压缩后发送", 100)
        with Path(asset["path"]).open("rb") as handle:
            suffix = image_suffix(handle.read(12))
        if suffix == ".webp":
            return self._save_image(Path(asset["path"]).read_bytes())
        return reference, asset

    def _save_image(self, data):
        data, suffix = wechat_image(data)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (secrets.token_hex(16) + suffix)
        path.write_bytes(data)
        try:
            return self.register(path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def register(self, path, name=None):
        path = Path(path)
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(self.directory):
            raise ValueError("附件必须位于当前会话的媒体缓存目录")
        if not resolved.is_file():
            raise ValueError("微信未保存可读取的附件")
        size = resolved.stat().st_size
        if not 0 < size <= MAX_MEDIA_BYTES:
            raise ValueError("附件为空或超过 100 MiB 上报限制")
        token = secrets.token_urlsafe(32)
        asset = {"path": str(resolved), "name": name or resolved.name, "size": size}
        self.store.add_asset(token, asset)
        return token, self.describe(token)

    def describe(self, token):
        asset = self.store.get_asset(token) if isinstance(token, str) else None
        if asset is None:
            raise ActionError("附件不在当前会话媒体缓存中", 100)
        path = Path(asset["path"])
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not resolved.is_relative_to(self.directory)
            or not resolved.is_file()
            or resolved.stat().st_size != asset["size"]
        ):
            raise ActionError("缓存附件已丢失或改变，请重新发送附件", 100)
        return {
            **asset,
            "url": f"{self.base_url}/media/{token}/{quote(asset['name'], safe='')}",
            "content_type": mimetypes.guess_type(asset["name"])[0] or "application/octet-stream",
        }
