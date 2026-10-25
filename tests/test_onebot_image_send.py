import asyncio
import base64
import json
import random
import struct
import zlib
from io import BytesIO

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from wxbridge import DeliveryUncertain  # noqa: E402
from wxbridge.onebot.config import OneBotConfig  # noqa: E402
from wxbridge.onebot.protocol import ActionError, outgoing_message  # noqa: E402
from wxbridge.onebot.server import create_app  # noqa: E402


def image_bytes():
    """A valid, incompressible PNG whose base64 request exceeds the old 64 KiB limit."""
    size = 192
    pixels = random.Random(17).randbytes(size * size * 3)
    rows = b"".join(
        b"\0" + pixels[start : start + size * 3] for start in range(0, len(pixels), size * 3)
    )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def image_segment(data):
    return {"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode()}}


@pytest.fixture
def config(tmp_path):
    return OneBotConfig(
        chat_name="表情测试好友",
        access_token="image-test-token-1234567890",
        database=tmp_path / "images.sqlite3",
        poll_interval=0.1,
    )


def test_reverse_ws_large_astrbot_image_gets_echo_and_keeps_connection(config):
    async def run():
        connections = asyncio.Queue()

        async def handler(socket):
            await connections.put(socket)
            await socket.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as listener:
            config.reverse_ws_urls = [f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/ws"]
            app = create_app(config, mock=True)
            async with app.router.lifespan_context(app):
                socket = await asyncio.wait_for(connections.get(), 3)
                assert json.loads(await socket.recv())["sub_type"] == "connect"
                data = image_bytes()
                request = json.dumps(
                    {
                        "action": "send_private_msg",
                        "params": {"user_id": config.peer_id, "message": [image_segment(data)]},
                        "echo": {"meme": "streaming"},
                    }
                )
                assert len(request.encode()) > 65536
                await socket.send(request)
                result = json.loads(await asyncio.wait_for(socket.recv(), 3))
                assert result["status"] == "ok" and result["echo"] == {"meme": "streaming"}
                assert app.state.gateway.client._backend.sent_images == [data]
                assert app.state.gateway.client._backend.sent == []
                mid = result["data"]["message_id"]
                saved = app.state.gateway.store.get(mid)
                assert saved["message"][0]["type"] == "image"
                assert "base64://" not in json.dumps(saved)
                await socket.send(json.dumps({"action": "get_login_info", "echo": "still-open"}))
                assert json.loads(await asyncio.wait_for(socket.recv(), 3))["echo"] == "still-open"
                assert connections.empty()

    asyncio.run(run())


def test_http_and_forward_ws_images_are_sent_and_displayed_once(config):
    app = create_app(config, mock=True, admin_token="admin-image-test-token")
    auth = {"Authorization": f"Bearer {config.access_token}"}
    data = image_bytes()
    with TestClient(app, headers=auth) as client:
        assert client.get("/can_send_image").json()["data"]["yes"] is True
        with client.websocket_connect("/api") as socket:
            socket.send_json(
                {
                    "action": "send_private_msg",
                    "params": {"user_id": config.peer_id, "message": [image_segment(data)]},
                    "echo": "forward-image",
                }
            )
            result = socket.receive_json()
            assert result["status"] == "ok" and result["echo"] == "forward-image"
        saved = client.get("/get_msg", params={"message_id": result["data"]["message_id"]}).json()[
            "data"
        ]
        segment = saved["message"][0]
        assert client.get(segment["data"]["url"]).content == data
        again = client.post(
            "/send_msg", json={"user_id": config.peer_id, "message": segment}
        ).json()
        assert again["status"] == "ok"
        # Native self echoes must not add duplicate Web rows or incoming model events.
        client.portal.call(app.state.gateway.poll_once)
        page = client.get(
            "/console/api/messages", headers={"Authorization": "Bearer admin-image-test-token"}
        ).json()
        assert len(page["messages"]) == 2
        assert all(
            row["kind"] == "image" and row["forward_status"] == "sent" for row in page["messages"]
        )
        assert app.state.gateway.client._backend.sent_images == [data, data]


@pytest.mark.parametrize(
    "message",
    [
        [image_segment(b"not an image")],
        [{"type": "image", "data": {"file": "base64://%%%"}}],
        [{"type": "text", "data": {"text": "不要部分发送"}}, image_segment(b"bad")],
        [{"type": "image", "data": {"file": "file:///C:/Windows/win.ini"}}],
        [{"type": "image", "data": {"file": "http://127.0.0.1/internal"}}],
        [{"type": "image", "data": {}}],
        [{"type": "image", "data": {"file": "base64://", "type": "flash"}}],
    ],
)
def test_invalid_image_requests_return_failure_without_submitting_or_timing_out(config, message):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        with client.websocket_connect("/api") as socket:
            socket.send_json(
                {
                    "action": "send_private_msg",
                    "params": {"user_id": config.peer_id, "message": message},
                    "echo": "invalid-image",
                }
            )
            result = socket.receive_json()
            assert result["status"] == "failed" and result["echo"] == "invalid-image"
            socket.send_json({"action": "get_status", "echo": "alive"})
            assert socket.receive_json()["echo"] == "alive"
        assert app.state.gateway.client._backend.sent_images == []
        assert app.state.gateway.client._backend.sent == []


def test_uncertain_image_send_is_not_retried(config, monkeypatch):
    app = create_app(config, mock=True)
    calls = []
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:

        def uncertain(*args, **kwargs):
            calls.append(1)
            raise DeliveryUncertain("图片已提交，结果不确定")

        monkeypatch.setattr(app.state.gateway.client, "SendImage", uncertain)
        result = client.post(
            "/send_msg", json={"user_id": config.peer_id, "message": image_segment(image_bytes())}
        ).json()
        assert result["retcode"] == 1200 and result["data"]["retryable"] is False
        assert calls == [1]


def test_image_limit_is_enforced_before_ui_submission(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        result = client.post(
            "/send_msg",
            json={
                "user_id": config.peer_id,
                "message": image_segment(b"GIF89a" + b"x" * (8 * 1024 * 1024)),
            },
        ).json()
        assert result["retcode"] == 100 and "8 MiB" in result["message"]
        assert app.state.gateway.client._backend.sent_images == []


def test_gif_meme_has_gif_extension_for_native_wechat(config):
    data = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    auth = {"Authorization": f"Bearer {config.access_token}"}
    with TestClient(create_app(config, mock=True), headers=auth) as client:
        result = client.post(
            "/send_private_msg", json={"user_id": config.peer_id, "message": [image_segment(data)]}
        ).json()
        assert result["status"] == "ok"
        saved = client.get("/get_msg", params={"message_id": result["data"]["message_id"]}).json()[
            "data"
        ]
        image = client.get(
            "/get_image", params={"file": saved["message"][0]["data"]["file"]}
        ).json()["data"]
        assert image["filename"].endswith(".gif")
        assert client.get(image["url"]).content == data


def webp_image(*, animated=False):
    from PIL import Image

    output = BytesIO()
    first = Image.new("RGBA", (8, 6), (240, 20, 10, 128 if not animated else 255))
    second = Image.new("RGBA", (8, 6), (10, 240, 20, 255))
    try:
        if animated:
            first.save(
                output,
                "WEBP",
                lossless=True,
                save_all=True,
                append_images=[second],
                duration=[80, 160],
                loop=3,
            )
        else:
            first.save(output, "WEBP", lossless=True)
        return output.getvalue()
    finally:
        first.close()
        second.close()
        output.close()


@pytest.mark.parametrize("animated", [False, True])
def test_webp_is_converted_to_native_image_instead_of_sending_a_webp_file(config, animated):
    from PIL import Image

    app = create_app(config, mock=True)
    auth = {"Authorization": f"Bearer {config.access_token}"}
    with TestClient(app, headers=auth) as client:
        result = client.post(
            "/send_private_msg",
            json={
                "user_id": config.peer_id,
                "message": image_segment(webp_image(animated=animated)),
            },
        ).json()
        assert result["status"] == "ok"
        sent = app.state.gateway.client._backend.sent_images
        assert len(sent) == 1
        with Image.open(BytesIO(sent[0])) as image:
            assert image.size == (8, 6)
            assert image.format == ("GIF" if animated else "PNG")
            if animated:
                assert image.n_frames == 2 and image.info["loop"] == 3
                assert image.info["duration"] == 80
                image.seek(1)
                assert image.info["duration"] == 160
            else:
                assert image.convert("RGBA").getpixel((0, 0)) == (240, 20, 10, 128)
        saved = client.get("/get_msg", params={"message_id": result["data"]["message_id"]}).json()[
            "data"
        ]
        info = client.get(
            "/get_image", params={"file": saved["message"][0]["data"]["file"]}
        ).json()["data"]
        assert info["filename"].endswith(".gif" if animated else ".png")
        assert client.get(info["url"]).content == sent[0]


def test_forwarding_an_existing_webp_token_also_converts_without_changing_original(config):
    app = create_app(config, mock=True)
    data = webp_image()
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        cache = app.state.gateway.media
        cache.directory.mkdir()
        path = cache.directory / "received.webp"
        path.write_bytes(data)
        token, _ = client.portal.call(cache.register, path)
        result = client.post(
            "/send_msg",
            json={"user_id": config.peer_id, "message": {"type": "image", "data": {"file": token}}},
        ).json()
        assert result["status"] == "ok" and path.read_bytes() == data
        assert app.state.gateway.client._backend.sent_images[0].startswith(b"\x89PNG\r\n\x1a\n")


def test_invalid_webp_is_rejected_before_any_native_submission(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        result = client.post(
            "/send_msg",
            json={
                "user_id": config.peer_id,
                "message": image_segment(b"RIFF\x20\0\0\0WEBPbroken-data"),
            },
        ).json()
        assert result["retcode"] == 100 and "WebP" in result["message"]
        assert not app.state.gateway.client._backend.sent_images
        assert not app.state.gateway.media.directory.exists()


def test_converted_webp_must_still_fit_the_image_size_limit(config, monkeypatch):
    from wxbridge.onebot import media

    app = create_app(config, mock=True)
    data = webp_image()
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", len(data) + 1)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        result = client.post(
            "/send_msg", json={"user_id": config.peer_id, "message": image_segment(data)}
        ).json()
        assert result["retcode"] == 100 and "转换后" in result["message"]
        assert not app.state.gateway.client._backend.sent_images


def test_wrong_peer_rejected_before_image_cache_write(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        result = client.post(
            "/send_msg",
            json={"user_id": config.peer_id + 1, "message": image_segment(image_bytes())},
        ).json()
        assert result["status"] == "failed"
        assert not app.state.gateway.media.directory.exists()


def test_mixed_images_fail_before_partial_send():
    with pytest.raises(ActionError, match="单独发送"):
        outgoing_message([image_segment(b"x"), {"type": "record", "data": {"file": "x"}}])
