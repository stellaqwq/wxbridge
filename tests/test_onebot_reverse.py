import asyncio
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

from wxbridge.onebot.config import OneBotConfig  # noqa: E402
from wxbridge.onebot.server import create_app  # noqa: E402


def test_reverse_ws_real_tcp_headers_send_receive_and_reconnect(tmp_path):
    async def run():
        connections = asyncio.Queue()

        async def handler(socket):
            await connections.put(socket)
            await socket.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            config = OneBotConfig(
                chat_name="模拟好友",
                access_token="reverse-test-token-1234567890",
                database=tmp_path / "reverse.sqlite3",
                reconnect_interval=0.1,
                reverse_ws_urls=[f"ws://127.0.0.1:{port}/onebot/v11/ws"],
                poll_interval=0.1,
            )
            app = create_app(config, mock=True)
            async with app.router.lifespan_context(app):
                socket = await asyncio.wait_for(connections.get(), 5)
                assert socket.request.headers["X-Self-ID"] == str(config.self_id)
                assert socket.request.headers["X-Client-Role"] == "Universal"
                assert socket.request.headers["Authorization"] == f"Bearer {config.access_token}"
                event = json.loads(await asyncio.wait_for(socket.recv(), 3))
                assert event["sub_type"] == "connect"
                await socket.send(
                    json.dumps(
                        {
                            "action": "send_private_msg",
                            "params": {
                                "user_id": config.peer_id,
                                "message": "反向 WS 回复",
                            },
                            "echo": {"request": [1, 2]},
                        }
                    )
                )
                result = json.loads(await asyncio.wait_for(socket.recv(), 3))
                assert result["status"] == "ok" and result["echo"] == {"request": [1, 2]}
                assert app.state.gateway.client._backend.sent == ["反向 WS 回复"]
                await app.state.gateway.dispatch(
                    {"action": "wxbridge_mock_receive", "params": {"message": "新好友消息"}}
                )
                event = json.loads(await asyncio.wait_for(socket.recv(), 3))
                assert event["post_type"] == "message"
                assert event["user_id"] == config.peer_id
                await socket.close()
                socket2 = await asyncio.wait_for(connections.get(), 5)
                event = json.loads(await asyncio.wait_for(socket2.recv(), 3))
                assert event["sub_type"] == "connect"
                await socket2.send(json.dumps({"action": "get_login_info", "echo": "again"}))
                result = json.loads(await asyncio.wait_for(socket2.recv(), 3))
                assert result["echo"] == "again"  # No old message replay after reconnect.

    asyncio.run(run())
