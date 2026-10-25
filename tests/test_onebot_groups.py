import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from wxbridge import WeChat  # noqa: E402
from wxbridge.onebot.config import OneBotConfig, init_config, load_config  # noqa: E402
from wxbridge.onebot.gateway import Gateway  # noqa: E402
from wxbridge.onebot.groups import mention_segments  # noqa: E402
from wxbridge.onebot.mock import MockBackend  # noqa: E402
from wxbridge.onebot.monitor import MessageMonitor  # noqa: E402
from wxbridge.onebot.server import create_app  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


@pytest.fixture
def config(tmp_path):
    return OneBotConfig(
        chat_type="group",
        chat_name="测试群(1)",
        group_id=20001,
        group_mention_name="@测试机器人",
        access_token="group-test-token-with-32-characters",
        database=tmp_path / "groups.sqlite3",
        poll_interval=0.1,
    )


def test_group_events_at_members_and_persistence(config):
    auth = {"Authorization": f"Bearer {config.access_token}"}
    with TestClient(create_app(config, mock=True), headers=auth) as client:
        with client.websocket_connect("/event") as socket:
            assert socket.receive_json()["sub_type"] == "connect"
            assert client.get("/get_friend_list").json()["data"] == []
            assert client.get("/get_group_list").json()["data"] == [
                {"group_id": config.group_id, "group_name": config.chat_name}
            ]
            events = []
            for sender in ("成员甲", "成员乙", "成员甲"):
                client.post(
                    "/wxbridge_mock_receive",
                    json={
                        "message": "@测试机器人\u2005你好 [x]",
                        "sender": sender,
                    },
                )
                events.append(socket.receive_json())
            first = events[0]
            assert first["message_type"] == "group" and first["sub_type"] == "normal"
            assert first["group_id"] == config.group_id and first["anonymous"] is None
            assert first["sender"]["role"] == "member"
            assert first["user_id"] == first["sender"]["user_id"]
            assert first["user_id"] not in {config.peer_id, config.self_id}
            assert events[0]["user_id"] == events[2]["user_id"] != events[1]["user_id"]
            assert first["message"][0] == {"type": "at", "data": {"qq": str(config.self_id)}}
            assert first["raw_message"].endswith("你好 &#91;x&#93;")
            members = client.get(
                "/get_group_member_list", params={"group_id": config.group_id}
            ).json()["data"]
            assert len(members) == 3  # Only observed members and self, not the full roster.
            for member in members:
                result = client.get(
                    "/get_group_member_info",
                    params={
                        "group_id": config.group_id,
                        "user_id": member["user_id"],
                    },
                ).json()
                assert result["data"]["nickname"] == member["nickname"]
            assert (
                client.get(
                    "/get_group_member_info",
                    params={
                        "group_id": config.group_id,
                        "user_id": 99999,
                    },
                ).json()["status"]
                == "failed"
            )
    with TestClient(create_app(config, mock=True), headers=auth) as client:
        with client.websocket_connect("/event") as socket:
            socket.receive_json()
            client.post("/wxbridge_mock_receive", json={"message": "重启后", "sender": "成员甲"})
            assert socket.receive_json()["user_id"] == first["user_id"]


def test_group_send_text_image_and_no_self_echo(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        backend = app.state.gateway.client._backend
        text = client.post(
            "/send_group_msg", json={"group_id": config.group_id, "message": "群回复"}
        ).json()
        image = client.post(
            "/send_msg",
            json={
                "group_id": config.group_id,
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "base64://" + base64.b64encode(PNG).decode()},
                    }
                ],
            },
        ).json()
        assert text["status"] == image["status"] == "ok"
        assert backend.sent == ["群回复"] and len(backend.sent_images) == 1
        stored = client.get("/get_msg", params={"message_id": text["data"]["message_id"]}).json()[
            "data"
        ]
        assert stored["message_type"] == "group" and stored["group_id"] == config.group_id
        with client.websocket_connect("/event") as socket:
            socket.receive_json()
            client.post("/wxbridge_mock_receive", json={"message": "下一条"})
            assert socket.receive_json()["raw_message"] == "下一条"


@pytest.mark.parametrize(
    "action,params",
    [
        ("send_private_msg", {"user_id": 10001}),
        ("send_group_msg", {"group_id": 20002}),
        ("send_group_msg", {"group_id": 20001, "user_id": 10001}),
        ("send_msg", {"group_id": 20001, "message_type": "private"}),
        ("send_msg", {"group_id": 20001, "message_type": "other"}),
    ],
)
def test_group_rejects_wrong_target_before_caching_or_ui(config, action, params):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:

        def forbidden(*_):
            pytest.fail("An invalid target must be rejected before touching media or UI")

        app.state.gateway.media.prepare_image = forbidden
        app.state.gateway.client.ChatInfo = forbidden
        result = client.post(
            "/" + action,
            json={
                **params,
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "base64://invalid"},
                    }
                ],
            },
        ).json()
        assert result["status"] == "failed"


def test_group_send_rejects_same_name_private_chat(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        backend = app.state.gateway.client._backend
        backend.state = replace(backend.state, chat_type="friend")
        result = client.post(
            "/send_group_msg", json={"group_id": config.group_id, "message": "不得错发"}
        ).json()
        assert result["status"] == "failed" and backend.sent == []


def test_group_media_keep_sender_identity_and_real_content(config):
    class Backend(MockBackend):
        def download_attachment(self, message, directory, timeout):
            path = Path(directory) / ("pic.png" if message.type == "image" else "report.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG if message.type == "image" else b"report body")
            return path

        def transcribe_voice(self, *_):
            return "群语音转写"

    backend = Backend(config.chat_name, "group")
    backend.state = replace(backend.state, info={"member_count": 7})
    monitor = MessageMonitor()
    gateway = Gateway(config, WeChat(backend_factory=lambda: backend), on_message=monitor.record)

    async def run():
        gateway._check_chat(gateway.client.ChatInfo())
        gateway.client.GetNextNewMessage(config.chat_name)
        for kind in ("image", "file", "voice"):
            backend.inject("[媒体]", kind=kind, sender="同一位成员")
        await gateway.poll_once()
        for _ in range(3):
            await gateway.process_media_once()
        events = [gateway.outbox.get_nowait() for _ in range(3)]
        assert all(
            e["message_type"] == "group" and e["group_id"] == config.group_id for e in events
        )
        assert len({e["user_id"] for e in events}) == 1
        assert [e["message"][0]["type"] for e in events] == ["image", "file", "text"]
        assert events[-1]["message"][0]["data"]["text"] == "群语音转写"
        assert all(m["forward_status"] == "forwarded" for m in monitor.messages)
        file = events[1]["message"][0]["data"]
        result = await gateway.dispatch(
            {
                "action": "get_group_file_url",
                "params": {
                    "group_id": config.group_id,
                    "file_id": file["file_id"],
                },
            }
        )
        assert result["data"]["url"] == file["url"]
        result = await gateway.dispatch(
            {"action": "get_group_info", "params": {"group_id": config.group_id}}
        )
        assert result["data"]["member_count"] == 7

    try:
        asyncio.run(run())
    finally:
        asyncio.run(gateway.close())


def test_web_switch_type_and_group_id_isolates_history(config, tmp_path):
    path = tmp_path / "onebot.json"
    init_config(path)
    app = create_app(load_config(path), mock=True, config_path=path)
    with TestClient(app) as client:
        admin = {"Authorization": f"Bearer {app.state.admin_token}"}
        auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
        client.post("/console/api/config", headers=admin, json={"chat_name": config.chat_name})
        client.post("/wxbridge_mock_receive", headers=auth, json={"message": "私聊历史"})
        old = app.state.gateway.config.database
        result = client.post(
            "/console/api/config",
            headers=admin,
            json={
                "chat_type": "group",
                "group_id": "9007199254740993",
                "group_mention_name": "@测试机器人",
            },
        ).json()
        assert result["connected"] is True and app.state.gateway.config.database != old
        assert result["config"]["group_id"] == "9007199254740993"
        assert load_config(path).group_mention_name == "测试机器人"
        client.post(
            "/wxbridge_mock_receive", headers=auth, json={"message": "@测试机器人\u2005群正文"}
        )
        monitor = client.get("/console/api/messages", headers=admin).json()
        assert monitor["chat_type"] == "group"
        assert [m["text"] for m in monitor["messages"]] == ["@测试机器人\u2005群正文"]
        group_database = app.state.gateway.config.database
        client.post("/console/api/config", headers=admin, json={"group_id": "20002"})
        assert app.state.gateway.config.database != group_database
        assert client.get("/console/api/messages", headers=admin).json()["messages"] == []


def test_group_http_quick_reply_targets_group(config):
    async def run():
        app = create_app(config, mock=True)
        async with app.router.lifespan_context(app):
            transports = app.state.transports
            await transports.http.aclose()

            def report(request):
                event = json.loads(request.content)
                return (
                    httpx.Response(200, json={"reply": "群快速回复", "at_sender": False})
                    if event.get("post_type") == "message"
                    else httpx.Response(204)
                )

            transports.http = httpx.AsyncClient(transport=httpx.MockTransport(report))
            transports.config.http_post_urls = ["http://test/group"]
            queue = asyncio.Queue()
            transports.listeners.add(queue)
            await app.state.gateway.dispatch(
                {"action": "wxbridge_mock_receive", "params": {"message": "触发群快速回复"}}
            )
            assert (await asyncio.wait_for(queue.get(), 3))["group_id"] == config.group_id
            assert app.state.gateway.client._backend.sent == ["群快速回复"]

    asyncio.run(run())


def test_group_mentions_require_exact_visible_name_boundary():
    assert mention_segments("@机器人甲 hi", "机器人", 10000)[0] == [
        {"type": "text", "data": {"text": "@机器人甲 hi"}}
    ]
    segments, raw = mention_segments("[x] @机器人\u2005test @机器人", "机器人", 10000)
    assert [s["type"] for s in segments] == ["text", "at", "text", "at"]
    assert raw == "&#91;x&#93; [CQ:at,qq=10000]\u2005test [CQ:at,qq=10000]"
