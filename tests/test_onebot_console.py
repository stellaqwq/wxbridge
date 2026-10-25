import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from wxbridge.onebot.config import init_config, load_config  # noqa: E402
from wxbridge.onebot.server import create_app  # noqa: E402


@pytest.fixture
def console(tmp_path):
    path = tmp_path / "onebot.json"
    init_config(path)
    config = load_config(path)
    app = create_app(config, mock=True, config_path=path)
    with TestClient(app) as client:
        admin = {"Authorization": f"Bearer {app.state.admin_token}"}
        yield app, client, path, admin


def test_unconfigured_start_has_public_page_but_no_secrets_or_native_client(console):
    app, client, path, admin = console
    token = load_config(path).access_token
    assert app.state.gateway is None
    response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "联系人备注名" in response.text
    assert token not in response.text and app.state.admin_token not in response.text
    assert client.get("/console/static/console.js").status_code == 200
    assert client.get("/console/static/console.css").status_code == 200
    assert client.get("/console/api/config").status_code == 401
    assert (
        client.get(
            "/console/api/config",
            headers={
                "Authorization": f"Bearer {token}",
            },
        ).status_code
        == 401
    )  # API clients cannot change the private-chat binding.
    view = client.get("/console/api/config", headers=admin).json()
    assert view["phase"] == "unconfigured" and view["config"]["chat_name"] == ""
    assert view["config"]["access_token"] == token


def test_web_save_connect_disconnect_and_reload_preserve_token_and_relative_path(console):
    app, client, path, admin = console
    before = json.loads(path.read_text(encoding="utf-8"))
    result = client.post(
        "/console/api/config",
        headers=admin,
        json={
            "chat_name": "  网页填写的备注  ",
            "self_id": "10000",
            "peer_id": "10001",
            "enable_accessibility": False,
            "connect": True,
        },
    )
    assert result.status_code == 200
    assert result.json()["saved"] is True and result.json()["phase"] == "connected"
    assert app.state.gateway.config.chat_name == "网页填写的备注"
    assert app.state.gateway.client._backend.sent == []  # Connecting never sends a test message.
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["chat_name"] == "网页填写的备注"
    assert after["access_token"] == before["access_token"]
    assert after["database"] == before["database"]
    assert app.state.gateway.config.database.name.endswith("-mock.sqlite3")
    result = client.post("/console/api/disconnect", headers=admin)
    assert result.json()["connected"] is False and app.state.gateway is None
    result = client.post("/console/api/connect", headers=admin)
    assert result.json()["connected"] is True
    assert load_config(path).chat_name == "网页填写的备注"


def test_invalid_web_settings_keep_existing_config_and_session(console):
    app, client, path, admin = console
    client.post("/console/api/config", headers=admin, json={"chat_name": "测试好友"})
    original = path.read_bytes()
    gateway = app.state.gateway
    for payload in (
        {"chat_name": " "},
        {"self_id": "10001"},
        {"database": "other.sqlite3"},
        {"reverse_ws_urls": ["file:///C:/private"]},
        {"connect": "false"},
    ):
        result = client.post("/console/api/config", headers=admin, json=payload)
        assert result.status_code == 400
        assert path.read_bytes() == original and app.state.gateway is gateway
    assert client.post("/console/api/disconnect").status_code == 401
    assert app.state.gateway is gateway


def test_saving_without_connect_does_not_create_a_wechat_client(console):
    app, client, path, admin = console
    result = client.post(
        "/console/api/config",
        headers=admin,
        json={
            "chat_name": "先保存联系人",
            "connect": False,
        },
    )
    assert result.json()["saved"] is True and result.json()["connected"] is False
    assert app.state.gateway is None
    assert load_config(path).chat_name == "先保存联系人"


def test_connection_failure_stays_editable_and_keeps_saved_settings(tmp_path):
    path = tmp_path / "bot.json"
    init_config(path)

    def fail():
        raise RuntimeError("请让微信保持前台")

    app = create_app(load_config(path), config_path=path, client_factory=fail)
    with TestClient(app) as client:
        admin = {"Authorization": f"Bearer {app.state.admin_token}"}
        result = client.post("/console/api/config", headers=admin, json={"chat_name": "测试好友"})
        assert result.status_code == 200 and result.json()["saved"] is True
        assert result.json()["connected"] is False
        assert "请让微信保持前台" in result.json()["error"]
        assert load_config(path).chat_name == "测试好友"
        assert client.get("/").status_code == 200
        assert client.get("/console/api/config", headers=admin).status_code == 200


def test_web_changing_contact_rotates_database_without_losing_old_records(console):
    app, client, path, admin = console
    client.post("/console/api/config", headers=admin, json={"chat_name": "第一位好友"})
    api_auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    sent = client.post(
        "/send_private_msg", headers=api_auth, json={"user_id": 10001, "message": "仅写入模拟微信"}
    ).json()
    old_database = app.state.gateway.config.database
    result = client.post("/console/api/config", headers=admin, json={"chat_name": "第二位好友"})
    assert result.json()["connected"] is True
    assert app.state.gateway.config.database != old_database and old_database.exists()
    assert (
        client.get(
            "/get_msg", headers=api_auth, params={"message_id": sent["data"]["message_id"]}
        ).json()["status"]
        == "failed"
    )


def test_protocol_status_and_send_report_disconnected_before_setup(console):
    _, client, path, _ = console
    auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    result = client.get("/get_status", headers=auth)
    assert result.status_code == 200 and result.json()["data"]["good"] is False
    assert client.get("/get_login_info", headers=auth).json()["data"]["user_id"] == 10000
    result = client.post(
        "/send_private_msg", headers=auth, json={"user_id": 10001, "message": "不得发送"}
    )
    assert result.json()["status"] == "failed" and "Web" in result.json()["message"]


def test_web_message_monitor_requires_admin_and_shows_no_startup_history(console):
    app, client, path, admin = console
    api_auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    assert client.get("/console/api/messages").status_code == 401
    assert client.get("/console/api/messages", headers=api_auth).status_code == 401
    result = client.get("/console/api/messages", headers=admin).json()
    assert result["messages"] == [] and result["connected"] is False
    client.post("/console/api/config", headers=admin, json={"chat_name": "监听好友"})
    result = client.get("/console/api/messages", headers=admin).json()
    assert result["messages"] == [] and result["ready"] is True
    assert app.state.gateway.client._backend.sent == []


def test_web_messages_include_incoming_and_sent_once_without_consuming_onebot_events(console):
    _, client, path, admin = console
    client.post("/console/api/config", headers=admin, json={"chat_name": "监听好友"})
    api_auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    with client.websocket_connect("/event", headers=api_auth) as socket:
        assert socket.receive_json()["sub_type"] == "connect"
        client.post("/wxbridge_mock_receive", headers=api_auth, json={"message": "收到一条消息"})
        first = client.get("/console/api/messages", headers=admin).json()
        assert len(first["messages"]) == 1
        assert first["messages"][0]["direction"] == "received"
        assert (
            client.get("/console/api/messages", headers=admin).json()["messages"]
            == first["messages"]
        )
        assert socket.receive_json()["message"][0]["data"]["text"] == "收到一条消息"
        client.post(
            "/send_private_msg",
            headers=api_auth,
            json={"user_id": 10001, "message": "模拟发出的文字"},
        )
        client.post("/wxbridge_mock_receive", headers=api_auth, json={"message": "第二条新消息"})
        page = client.get(
            "/console/api/messages",
            headers=admin,
            params={
                "session": first["session"],
                "after": first["cursor"],
            },
        ).json()
        assert [item["direction"] for item in page["messages"]] == ["sent", "received"]
        assert [item["text"] for item in page["messages"]] == ["模拟发出的文字", "第二条新消息"]
        assert socket.receive_json()["message"][0]["data"]["text"] == "第二条新消息"
        empty = client.get(
            "/console/api/messages",
            headers=admin,
            params={
                "session": page["session"],
                "after": page["cursor"],
            },
        ).json()
        assert empty["messages"] == []


def test_web_monitor_reset_hides_old_contact_and_reports_disconnect(console):
    _, client, path, admin = console
    api_auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    client.post("/console/api/config", headers=admin, json={"chat_name": "第一位好友"})
    client.post("/wxbridge_mock_receive", headers=api_auth, json={"message": "第一位的消息"})
    first = client.get("/console/api/messages", headers=admin).json()
    client.post("/console/api/disconnect", headers=admin)
    disconnected = client.get("/console/api/messages", headers=admin).json()
    assert disconnected["connected"] is False and disconnected["messages"] == first["messages"]
    client.post("/console/api/config", headers=admin, json={"chat_name": "第二位好友"})
    page = client.get(
        "/console/api/messages",
        headers=admin,
        params={"session": first["session"], "after": first["cursor"]},
    ).json()
    assert page["reset"] is True and page["messages"] == []
    assert page["chat_name"] == "第二位好友"


def test_web_media_are_queued_instead_of_faking_onebot_text(console):
    app, client, path, admin = console
    client.post("/console/api/config", headers=admin, json={"chat_name": "监听好友"})
    api_auth = {"Authorization": f"Bearer {load_config(path).access_token}"}
    wx = app.state.gateway.client
    wx._call("inject", "[图片]", "friend", "image")
    wx._call("inject", "报告.pdf", "friend", "file")
    wx._call("inject", "3 秒语音", "friend", "voice")
    client.post("/wxbridge_mock_receive", headers=api_auth, json={"message": "触发模拟轮询"})
    page = client.get("/console/api/messages", headers=admin).json()
    assert [item["kind"] for item in page["messages"]] == ["image", "file", "voice", "text"]
    assert all(item["message_id"] is None for item in page["messages"][:3])
    assert all(
        item["forward_status"] in {"pending", "processing", "failed"}
        for item in page["messages"][:3]
    )
    assert app.state.gateway.client._backend.sent == []


@pytest.mark.parametrize("params", [{"after": -1}, {"limit": 201}, {"after": "bad"}])
def test_web_monitor_rejects_invalid_cursor_or_page_size(console, params):
    _, client, _, admin = console
    assert client.get("/console/api/messages", headers=admin, params=params).status_code == 422
