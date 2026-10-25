import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from wxbridge import WeChat, WeChatError  # noqa: E402
from wxbridge.onebot.config import OneBotConfig  # noqa: E402
from wxbridge.onebot.gateway import Gateway  # noqa: E402
from wxbridge.onebot.mock import MockBackend  # noqa: E402
from wxbridge.onebot.monitor import MessageMonitor  # noqa: E402
from wxbridge.onebot.server import create_app  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


class MediaBackend(MockBackend):
    def __init__(self):
        super().__init__("测试好友")
        self.downloads = []
        self.voices = []
        self.failure = None

    def inject_media(self, kind, *, attr="friend", info=None):
        self.inject(f"[{kind}]", attr, kind)
        message = replace(self.state.messages[-1], info=info or {})
        self.state = replace(self.state, messages=(*self.state.messages[:-1], message))
        return message

    def download_attachment(self, message, directory, timeout):
        self.downloads.append(message.id)
        if self.failure:
            raise self.failure
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = "测试图片.png" if message.type == "image" else "测试报告.txt"
        path = directory / name
        path.write_bytes(PNG if message.type == "image" else "文件测试正文".encode())
        return path

    def transcribe_voice(self, message, timeout):
        self.voices.append(message.id)
        if self.failure:
            raise self.failure
        return "这是微信识别的语音内容。"


@pytest.fixture
def config(tmp_path):
    return OneBotConfig(
        chat_name="测试好友",
        access_token="test-media-access-token-12345678",
        database=tmp_path / "media.sqlite3",
        poll_interval=0.1,
    )


def test_media_events_have_real_payloads_and_persistent_downloads(config):
    monitor = MessageMonitor()
    backend = MediaBackend()
    wx = WeChat(backend_factory=lambda: backend)
    gateway = Gateway(config, wx, on_message=monitor.record)

    async def run():
        wx.GetNextNewMessage(config.chat_name)
        for kind in ("image", "file", "voice"):
            backend.inject_media(kind, info={"filename": "报告.txt"} if kind == "file" else {})
        await gateway.poll_once()
        assert gateway.outbox.empty()  # Never forward just a media label.
        for _ in range(3):
            await gateway.process_media_once()
        assert len(monitor.messages) == 3
        assert all(row["forward_status"] == "forwarded" for row in monitor.messages)
        events = [gateway.outbox.get_nowait() for _ in range(3)]
        assert [event["message"][0]["type"] for event in events] == ["image", "file", "text"]
        assert all(event["post_type"] == "message" for event in events)
        assert all(event["user_id"] == config.peer_id for event in events)
        assert events[2]["message"][0]["data"]["text"] == "这是微信识别的语音内容。"
        assert events[2]["wxbridge"]["original_type"] == "voice"
        image = events[0]["message"][0]["data"]
        file = events[1]["message"][0]["data"]
        assert config.access_token not in image["url"]
        assert Path(gateway.media.describe(image["file"])["path"]).read_bytes() == PNG
        assert file["file_name"] == "报告.txt" and file["file_size"] > 0
        assert (await gateway.dispatch({"action": "get_image", "params": {"file": image["file"]}}))[
            "data"
        ]["url"] == image["url"]
        assert (
            await gateway.dispatch(
                {"action": "get_private_file_url", "params": {"file_id": file["file_id"]}}
            )
        )["data"]["url"] == file["url"]
        await gateway.poll_once()
        assert not gateway.pending_media and gateway.outbox.empty()
        assert backend.sent == [] and len(backend.downloads) == 2 and len(backend.voices) == 1
        return image, events[0]["message_id"]

    try:
        image, mid = asyncio.run(run())
    finally:
        asyncio.run(gateway.close())
    reopened = Gateway(config, WeChat(backend_factory=MediaBackend))
    try:
        assert reopened.media.describe(image["file"])["url"] == image["url"]
        assert reopened.store.get(mid)["message"][0]["type"] == "image"
    finally:
        asyncio.run(reopened.close())


def test_focus_wait_is_retried_without_losing_text_or_duplicating_media(config):
    monitor = MessageMonitor()
    backend = MediaBackend()
    gateway = Gateway(config, WeChat(backend_factory=lambda: backend), on_message=monitor.record)

    async def run():
        gateway.client.GetNextNewMessage(config.chat_name)
        backend.inject_media("image")
        await gateway.poll_once()
        backend.failure = WeChatError("目标窗口失去前台，已取消点击")
        await gateway.process_media_once()
        assert list(monitor.messages)[0]["forward_status"] == "waiting"
        assert len(gateway.pending_media) == 1 and gateway.outbox.empty()
        backend.inject("媒体处理中也能收到文字")
        await gateway.poll_once()
        assert gateway.outbox.get_nowait()["message"][0]["data"]["text"] == "媒体处理中也能收到文字"
        backend.failure = None
        next(iter(gateway.pending_media.values()))["due"] = 0
        await gateway.process_media_once()
        await gateway.process_media_once()
        assert gateway.outbox.qsize() == 1 and not gateway.pending_media
        assert len(monitor.messages) == 2

    try:
        asyncio.run(run())
    finally:
        asyncio.run(gateway.close())


def test_unavailable_media_and_own_media_are_never_faked_as_incoming_text(config):
    monitor = MessageMonitor()
    backend = MediaBackend()
    gateway = Gateway(config, WeChat(backend_factory=lambda: backend), on_message=monitor.record)

    async def run():
        gateway.client.GetNextNewMessage(config.chat_name)
        backend.inject_media("voice", attr="self")
        backend.inject_media("image")
        await gateway.poll_once()
        backend.failure = WeChatError("附件消息已经离开可见列表或内容改变")
        await gateway.process_media_once()
        assert [row["forward_status"] for row in monitor.messages] == ["display_only", "failed"]
        assert "离开可见列表" in monitor.messages[-1]["detail"]
        assert not gateway.pending_media and gateway.outbox.empty()
        assert not backend.voices and len(backend.downloads) == 1

    try:
        asyncio.run(run())
    finally:
        asyncio.run(gateway.close())


def test_existing_voice_transcript_needs_no_ui_or_raw_audio_export(config):
    backend = MediaBackend()
    gateway = Gateway(config, WeChat(backend_factory=lambda: backend))

    async def run():
        gateway.client.GetNextNewMessage(config.chat_name)
        backend.inject_media("voice", info={"transcript": "已转换的语音正文"})
        await gateway.poll_once()
        await gateway.process_media_once()
        assert gateway.outbox.get_nowait()["message"][0]["data"]["text"] == "已转换的语音正文"
        assert backend.voices == []

    try:
        asyncio.run(run())
    finally:
        asyncio.run(gateway.close())


def test_ws_media_event_download_capability_and_cache_isolation(config, tmp_path):
    app = create_app(config, mock=True, client_factory=lambda: WeChat(backend_factory=MediaBackend))
    auth = {"Authorization": f"Bearer {config.access_token}"}
    with TestClient(app, headers=auth) as client:
        gateway = app.state.gateway
        with client.websocket_connect("/event") as socket:
            assert socket.receive_json()["sub_type"] == "connect"
            gateway.client._backend.inject_media("image")
            client.portal.call(gateway.poll_once)
            event = socket.receive_json()
            data = event["message"][0]["data"]
            # Download clients need no global OneBot credential, only this unguessable URL.
            result = client.get(data["url"], headers={"Authorization": ""})
            assert result.status_code == 200 and result.content == PNG
            assert result.headers["content-type"] == "image/png"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert client.head(data["url"], headers={"Authorization": ""}).status_code == 200
            assert (
                client.get("/media/unknown/test.png", headers={"Authorization": ""}).status_code
                == 404
            )
            assert (
                client.get("/get_image", params={"file": "C:/Windows/win.ini"}).json()["status"]
                == "failed"
            )
            outside = tmp_path / "outside.txt"
            outside.write_text("unrelated", encoding="utf-8")
            with pytest.raises(ValueError, match="缓存目录"):
                gateway.media.register(outside)
            asset = client.portal.call(gateway.media.describe, data["file"])
            Path(asset["path"]).unlink()
            assert client.get(data["url"]).status_code == 404
            assert gateway.client._backend.sent == []


@pytest.mark.parametrize(
    "url", ["file:///D:/", "http://user:pass@localhost", "http://host/path", "http://host/?key=x"]
)
def test_media_base_rejects_credentials_and_paths(config, url):
    with pytest.raises(ValueError):
        OneBotConfig.model_validate({**config.model_dump(), "media_base_url": url})


def test_media_url_can_point_at_lan_address(config):
    config.media_base_url = "http://192.168.1.8:6700"
    gateway = Gateway(config, WeChat(backend_factory=MediaBackend))
    try:
        gateway.media.directory.mkdir()
        path = gateway.media.directory / "测试.png"
        path.write_bytes(PNG)
        _, asset = gateway.media.register(path)
        assert asset["url"].startswith("http://192.168.1.8:6700/media/")
    finally:
        asyncio.run(gateway.close())
