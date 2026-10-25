import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.testclient import WebSocketDenialResponse  # noqa: E402

from wxbridge import DeliveryUncertain, WeChat  # noqa: E402
from wxbridge.onebot.config import OneBotConfig, init_config, load_config  # noqa: E402
from wxbridge.onebot.mock import MockBackend  # noqa: E402
from wxbridge.onebot.server import MAX_API_REQUEST, create_app  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return OneBotConfig(
        chat_name="模拟好友",
        access_token="test-token-with-32-characters-1234",
        database=tmp_path / "messages.sqlite3",
        poll_interval=0.1,
    )


@pytest.fixture
def app_client(config):
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        yield app, client


def test_config_init_relative_database_and_no_token_overwrite(tmp_path):
    path = tmp_path / "bot.json"
    init_config(path)
    config = load_config(path)
    assert config.database == tmp_path / "onebot.sqlite3"
    assert len(config.access_token) >= 32
    assert config.enable_accessibility is False
    with pytest.raises(FileExistsError):
        init_config(path)
    assert load_config(path).access_token == config.access_token


@pytest.mark.parametrize(
    "updates",
    [
        {"self_id": 10001},
        {"chat_name": " "},
        {"access_token": "short"},
        {"port": 0},
        {"reverse_ws_urls": ["http://localhost:8080/"]},
        {"http_post_urls": ["file:///C:/secret"]},
        {"poll_interval": float("nan")},
        {"extra_typo": True},
        {"self_id": True},
        {"reverse_ws_urls": ["ws://user:pass@localhost/"]},
    ],
)
def test_config_rejects_bad_or_ambiguous_values(config, updates):
    with pytest.raises(ValueError):
        OneBotConfig.model_validate({**config.model_dump(), **updates})


def test_http_auth_and_status_codes(app_client):
    _, client = app_client
    assert client.get("/get_status", headers={"Authorization": ""}).status_code == 401
    assert client.get("/get_status", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/get_status", headers={"Authorization": "Basic wrong"}).status_code == 403
    assert client.get("/unknown_api").status_code == 404
    assert client.post("/send_private_msg", content="not json").status_code == 406
    assert (
        client.post(
            "/send_private_msg",
            content="{",
            headers={
                "Content-Type": "application/json",
            },
        ).status_code
        == 400
    )
    assert client.post("/send_private_msg", json=[]).status_code == 400
    assert (
        client.post("/send_private_msg", json={"message": "x" * MAX_API_REQUEST}).status_code == 413
    )


def test_http_actions_and_persistent_message_ids(config):
    auth = {"Authorization": f"Bearer {config.access_token}"}
    with TestClient(create_app(config, mock=True), headers=auth) as client:
        assert "微信接入控制台" in client.get("/").text
        assert client.get("/get_login_info/").json()["data"]["user_id"] == config.self_id
        assert client.get("/get_friend_list").json()["data"][0]["user_id"] == config.peer_id
        assert client.get("/get_group_list").json()["data"] == []
        assert client.get("/get_version_info").json()["data"]["protocol_version"] == "v11"
        assert client.get("/get_status").json()["data"]["good"] is True
        assert client.get("/can_send_image").json()["data"] == {"yes": True}
        sent = client.post(
            "/send_private_msg",
            data={
                "user_id": str(config.peer_id),
                "message": "你好 &amp; &#91;x&#93;",
                "auto_escape": "false",
            },
        ).json()
        assert sent["status"] == "ok"
        mid = sent["data"]["message_id"]
        assert 0 < mid < 2**31
    with TestClient(create_app(config, mock=True), headers=auth) as client:
        saved = client.get("/get_msg", params={"message_id": mid}).json()["data"]
        assert saved["message"] == [{"type": "text", "data": {"text": "你好 & [x]"}}]
        assert saved["sender"]["user_id"] == config.self_id
        assert saved["real_id"] == mid
        again = client.post("/send_msg", json={"user_id": config.peer_id, "message": "第二次"})
        assert again.json()["data"]["message_id"] > mid


def test_root_json_and_query_auth(app_client, config):
    _, client = app_client
    result = client.post("/", json={"action": "get_login_info", "echo": {"id": [1, None]}})
    assert result.json()["echo"] == {"id": [1, None]}
    result = client.get(
        "/get_login_info",
        params={"access_token": config.access_token},
        headers={"Authorization": ""},
    )
    assert result.status_code == 200


def test_wrong_target_or_unsupported_segments_never_send(app_client, config):
    app, client = app_client
    requests = [
        {"user_id": config.peer_id + 1, "message": "错误对象"},
        {"user_id": config.peer_id, "message": "先不发送[CQ:image,file=x]"},
        {"user_id": config.peer_id, "message": [{"type": "reply", "data": {"id": "1"}}]},
        {"user_id": config.peer_id, "message": "文字", "message_type": "group"},
        {"user_id": config.peer_id, "message": "文字", "group_id": 1},
        {"user_id": config.peer_id, "message": "x" * 4001},
    ]
    for params in requests:
        assert client.post("/send_msg", json=params).json()["status"] == "failed"
    assert client.get("/delete_msg", params={"message_id": 1}).status_code == 404
    assert app.state.gateway.client._backend.sent == []


def test_unknown_delivery_never_retries(app_client, config, monkeypatch):
    app, client = app_client
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(1)
        raise DeliveryUncertain("already submitted")

    monkeypatch.setattr(app.state.gateway.client, "SendMsg", uncertain)
    result = client.post(
        "/send_private_msg",
        json={
            "user_id": config.peer_id,
            "message": "只提交一次",
        },
    ).json()
    assert result["retcode"] == 1200
    assert result["data"] == {"delivery_status": "unknown", "retryable": False}
    assert calls == [1]


def test_changed_chat_is_blocked_before_send(app_client, config):
    app, client = app_client
    backend = app.state.gateway.client._backend
    backend.state = replace(backend.state, chat_name="另一个人")
    result = client.post(
        "/send_private_msg",
        json={
            "user_id": config.peer_id,
            "message": "禁止误发",
        },
    ).json()
    assert result["status"] == "failed"
    assert backend.sent == []


def test_forward_ws_events_actions_and_arbitrary_echo(app_client, config):
    app, client = app_client
    with client.websocket_connect(f"/?access_token={config.access_token}") as socket:
        lifecycle = socket.receive_json()
        assert lifecycle["meta_event_type"] == "lifecycle"
        assert lifecycle["sub_type"] == "connect"
        assert lifecycle["self_id"] == config.self_id
        # Startup history was not pushed. The next item must be our API result.
        socket.send_json({"action": "get_login_info", "echo": None})
        result = socket.receive_json()
        assert result["status"] == "ok" and "echo" in result and result["echo"] is None
        socket.send_text("{")
        assert socket.receive_json()["retcode"] == 1400
        socket.send_json({"action": "unknown", "echo": [1, {"x": "值"}]})
        assert socket.receive_json() == {
            "status": "failed",
            "retcode": 1404,
            "data": None,
            "message": "当前 wxbridge 适配层未实现此 API",
            "wording": "当前 wxbridge 适配层未实现此 API",
            "echo": [1, {"x": "值"}],
        }
        text = "收到 [CQ:image,file=x] & 好友文字"
        assert client.post("/wxbridge_mock_receive", json={"message": text}).status_code == 200
        event = socket.receive_json()
        assert event["post_type"] == "message" and event["message_type"] == "private"
        assert event["user_id"] == event["sender"]["user_id"] == config.peer_id
        assert event["message"] == [{"type": "text", "data": {"text": text}}]
        assert "&#91;CQ:image" in event["raw_message"]
        socket.send_json(
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": config.peer_id,
                    "message": [{"type": "text", "data": {"text": "回复"}}],
                },
                "echo": "send",
            }
        )
        sent = socket.receive_json()
        assert sent["echo"] == "send" and sent["status"] == "ok"
        assert sent["data"]["message_id"] != event["message_id"]
        assert app.state.gateway.client._backend.sent == ["回复"]


def test_api_only_socket_and_denial(app_client):
    _, client = app_client
    with client.websocket_connect("/api") as socket:
        socket.send_json({"action": "get_login_info", "echo": 99})
        assert socket.receive_json()["echo"] == 99  # No lifecycle on API-only socket.
    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect("/", headers={"Authorization": "Bearer wrong"}):
            pass
    assert exc.value.status_code == 403


def test_real_mode_has_no_mock_injection_endpoint(config):
    def factory():
        return WeChat(backend_factory=lambda: MockBackend(config.chat_name))

    with TestClient(
        create_app(config, client_factory=factory),
        headers={
            "Authorization": f"Bearer {config.access_token}",
        },
    ) as client:
        assert client.post("/wxbridge_mock_receive", json={"message": "x"}).status_code == 404
        assert client.get("/get_status").json()["data"]["online"] is None


def test_database_cannot_silently_rebind_to_another_contact(config):
    with TestClient(create_app(config, mock=True)):
        pass
    config.chat_name = "另外一个人"
    app = create_app(config, mock=True)
    with TestClient(app, headers={"Authorization": f"Bearer {config.access_token}"}) as client:
        assert "微信接入控制台" in client.get("/").text
        assert client.get("/get_status").json()["data"]["good"] is False
        assert "消息库绑定" in app.state.manager.error
        assert app.state.gateway is None


def test_event_only_socket_ignores_api_calls_and_reports_heartbeat(config):
    config.heartbeat_interval = 1
    with (
        TestClient(
            create_app(config, mock=True),
            headers={
                "Authorization": f"Bearer {config.access_token}",
            },
        ) as client,
        client.websocket_connect("/event/") as socket,
    ):
        assert socket.receive_json()["sub_type"] == "connect"
        socket.send_json(
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": config.peer_id,
                    "message": "事件连接不能发送",
                },
                "echo": "must-not-execute",
            }
        )
        heartbeat = socket.receive_json()
        assert heartbeat["meta_event_type"] == "heartbeat"
        assert heartbeat["interval"] == 1000 and heartbeat["status"]["good"] is True
        assert client.get("/get_msg", params={"message_id": 1}).json()["status"] == "failed"


def test_async_rate_limited_calls_finish_once_and_return_ids(config):
    async def run():
        app = create_app(config, mock=True)
        async with app.router.lifespan_context(app):
            gateway = app.state.gateway
            for suffix in ("_async", "_rate_limited", "_rate_limited"):
                result = await gateway.dispatch(
                    {
                        "action": "send_private_msg" + suffix,
                        "params": {"user_id": config.peer_id, "message": suffix},
                        "echo": suffix,
                    }
                )
                assert result["status"] == "async" and result["retcode"] == 1
                assert result["echo"] == suffix
            await asyncio.wait_for(asyncio.gather(*tuple(gateway.jobs)), 3)
            assert gateway.client._backend.sent == ["_async", "_rate_limited", "_rate_limited"]
            assert gateway.store.get(1)["sender"]["user_id"] == config.self_id
            assert gateway.store.get(3)["message_id"] == 3

    asyncio.run(run())


def test_poll_filters_self_other_and_nontext_and_deduplicates(config):
    from wxbridge.models import Message
    from wxbridge.onebot.gateway import Gateway

    class Client:
        def GetNextNewMessage(self, who):
            return [
                Message("a", "自己的文字", attr="self"),
                Message("b", "未知方向", attr="other"),
                Message("c", "图片占位", attr="friend", type="image"),
                Message("d", "好友新文字", attr="friend"),
            ]

        def close(self):
            pass

    async def run():
        gateway = Gateway(config, Client(), mock=True)
        try:
            await gateway.poll_once()
            await gateway.poll_once()
            event = gateway.outbox.get_nowait()
            assert gateway.outbox.empty()
            assert event["message"][0]["data"]["text"] == "好友新文字"
        finally:
            await gateway.close()

    asyncio.run(run())


def test_webhook_signature_quick_reply_and_event_order(config):
    async def run():
        app = create_app(config, mock=True)
        received = []

        def report(request):
            signature = hmac.new(b"webhook-secret", request.content, hashlib.sha1).hexdigest()
            assert request.headers["X-Signature"] == f"sha1={signature}"
            assert request.headers["X-Self-ID"] == str(config.self_id)
            event = json.loads(request.content)
            received.append(event)
            if event.get("post_type") == "message":
                return httpx.Response(200, json={"reply": "快速回复", "auto_escape": True})
            return httpx.Response(204)

        async with app.router.lifespan_context(app):
            transports = app.state.transports
            await transports.http.aclose()
            transports.http = httpx.AsyncClient(transport=httpx.MockTransport(report))
            transports.config.http_post_urls = ["http://test/events"]
            transports.config.http_post_secret = "webhook-secret"
            queue = asyncio.Queue()
            transports.listeners.add(queue)
            await app.state.gateway.dispatch(
                {"action": "wxbridge_mock_receive", "params": {"message": "触发快速操作"}}
            )
            event = await asyncio.wait_for(queue.get(), 3)
            assert event["post_type"] == "message"
            assert app.state.gateway.client._backend.sent == ["快速回复"]
            assert any(item.get("post_type") == "message" for item in received)
        assert received[-1]["sub_type"] == "disable"

    asyncio.run(run())
