"""Launch the real CLI and exercise HTTP/WS on loopback, using --mock exclusively."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect


async def exercise(port, token):
    auth = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=auth, timeout=5, trust_env=False
        ) as http,
        connect(f"ws://127.0.0.1:{port}/", additional_headers=auth, proxy=None) as ws,
    ):
        event = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert event["sub_type"] == "connect"
        await ws.send(json.dumps({"action": "get_version_info", "echo": "version"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert result["data"]["protocol_version"] == "v11"
        assert result["echo"] == "version"
        result = await http.post("/wxbridge_mock_receive", json={"message": "模拟好友发来的文字"})
        result.raise_for_status()
        event = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert event["post_type"] == "message"
        assert event["message"][0]["data"]["text"] == "模拟好友发来的文字"
        await ws.send(
            json.dumps(
                {
                    "action": "send_private_msg",
                    "params": {
                        "user_id": event["user_id"],
                        "message": "协议收发闭环通过",
                    },
                    "echo": "send",
                }
            )
        )
        result = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert result["status"] == "ok" and result["echo"] == "send"
        mid = result["data"]["message_id"]
        saved = await http.get("/get_msg", params={"message_id": mid})
        assert saved.json()["data"]["message"][0]["data"]["text"] == "协议收发闭环通过"
        assert (await http.get("/get_status")).json()["data"]["good"] is True


def main():
    project = Path(__file__).resolve().parents[1]
    root = project / "data" / "onebot-checks"
    root.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=root) as directory:
        token = secrets.token_urlsafe(32)
        config = Path(directory) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "chat_name": "模拟好友",
                    "access_token": token,
                    "database": "smoke.sqlite3",
                    "port": port,
                }
            ),
            encoding="utf-8",
        )
        with (Path(directory) / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "wxbridge.onebot.cli",
                    "--mock",
                    "--config",
                    str(config),
                ],
                cwd=project,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 15
                with httpx.Client(trust_env=False, timeout=0.5) as http:
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError("模拟 CLI 进程提前退出")
                        try:
                            response = http.get(
                                f"http://127.0.0.1:{port}/get_status",
                                headers={"Authorization": f"Bearer {token}"},
                            )
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() >= deadline:
                            raise TimeoutError("模拟服务未能在 15 秒内启动")
                        time.sleep(0.1)
                asyncio.run(exercise(port, token))
                print("PASS: mock CLI -> HTTP -> WebSocket event -> send -> get_msg")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
