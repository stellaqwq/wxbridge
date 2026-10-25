"""Check the Web form in a headless Edge, with an isolated --mock CLI only."""

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from wxbridge.onebot.config import init_config


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def browser_check(debug_port, url, output, config_path):
    async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
        for _ in range(100):
            try:
                targets = (await http.get(f"http://127.0.0.1:{debug_port}/json/list")).json()
                pages = [target for target in targets if target["type"] == "page"]
                if pages:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("Headless Edge did not start")
    async with connect(pages[0]["webSocketDebuggerUrl"], proxy=None, max_size=10_000_000) as ws:
        sequence = 0

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            await ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            while True:
                result = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if result.get("id") == sequence:
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    return result.get("result", {})

        async def evaluate(expression):
            result = await call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            if "exceptionDetails" in result:
                raise RuntimeError(result["exceptionDetails"]["text"])
            return result.get("result", {}).get("value")

        async def wait_for(expression):
            for _ in range(100):
                if await evaluate(expression):
                    return
                await asyncio.sleep(0.1)
            raise TimeoutError(f"Browser condition not met: {expression}")

        await call("Page.enable")
        await call("Page.navigate", {"url": url})
        await wait_for(
            "document.getElementById('settings') && !document.getElementById('settings').hidden"
        )
        assert await evaluate("document.getElementById('chat-name').value") == ""
        assert await evaluate("location.hash") == ""
        await evaluate("document.getElementById('chat-name').value = '网页测试好友'")
        await evaluate("document.getElementById('save-connect').click()")
        await wait_for("document.getElementById('status-label').textContent === '模拟模式已连接'")
        assert json.loads(config_path.read_text(encoding="utf-8"))["chat_name"] == "网页测试好友"
        assert "已连接" in await evaluate("document.getElementById('notice').textContent")
        assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
        await wait_for("document.getElementById('monitor-state').textContent === '正在监听'")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{config['port']}",
            trust_env=False,
            headers={"Authorization": f"Bearer {config['access_token']}"},
        ) as http:
            text = '网页收到的文字 <img src=x onerror="window.monitorXss=true">'
            (await http.post("/wxbridge_mock_receive", json={"message": text})).raise_for_status()
            await wait_for("document.getElementById('message-count').textContent === '1'")
            assert (
                await evaluate(
                    "document.querySelector('#message-list .message-content p').textContent"
                )
                == text
            )
            assert not await evaluate("Boolean(window.monitorXss)")
            assert await evaluate("document.querySelectorAll('#message-list img').length") == 0
            (
                await http.post(
                    "/send_private_msg",
                    json={
                        "user_id": config["peer_id"],
                        "message": "模拟发送的回复",
                        "auto_escape": True,
                    },
                )
            ).raise_for_status()
            await wait_for("document.getElementById('sent-count').textContent === '1'")
            await evaluate("document.getElementById('toggle-monitor').click()")
            (
                await http.post("/wxbridge_mock_receive", json={"message": "暂停期间收到的新消息"})
            ).raise_for_status()
            await asyncio.sleep(1.2)
            assert await evaluate("document.getElementById('message-count').textContent") == "2"
            await evaluate("document.getElementById('toggle-monitor').click()")
            await wait_for("document.getElementById('message-count').textContent === '3'")
        await evaluate(
            "document.getElementById('message-monitor').scrollIntoView({block: 'center'})"
        )
        screenshot = await call("Page.captureScreenshot", {"format": "png"})
        output.write_bytes(base64.b64decode(screenshot["data"]))
        await call("Page.reload")
        await wait_for(
            "document.getElementById('chat-name') && "
            "document.getElementById('chat-name').value === '网页测试好友'"
        )
        await call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )
        await asyncio.sleep(0.2)
        assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
        await wait_for("document.getElementById('message-count').textContent === '3'")
        await evaluate(
            "document.getElementById('message-monitor').scrollIntoView({block: 'center'})"
        )
        screenshot = await call("Page.captureScreenshot", {"format": "png"})
        output.with_stem(output.stem + "-mobile").write_bytes(base64.b64decode(screenshot["data"]))
        await evaluate("document.getElementById('clear-messages').click()")
        assert await evaluate("document.getElementById('message-count').textContent") == "0"
        await call("Page.reload")
        await wait_for(
            "document.getElementById('monitor-state') && "
            "document.getElementById('monitor-state').textContent === '正在监听'"
        )
        assert await evaluate("document.getElementById('message-count').textContent") == "0"
        await call("Page.navigate", {"url": url + "&view=messages"})
        await wait_for(
            "location.hash === '#message-monitor' && document.getElementById('monitor-state') && "
            "document.getElementById('monitor-state').textContent === '正在监听'"
        )
        assert await evaluate("location.hash") == "#message-monitor"
        assert (
            await evaluate("document.querySelector('.nav-item.active').getAttribute('href')")
            == "#message-monitor"
        )
        await evaluate("document.getElementById('toggle-monitor').click()")
        await evaluate(
            "appendMessage({entry_id:'media-fixture',time:1700000000,direction:'received',"
            "sender:'模拟好友',kind:'image',text:'[图片]',forward_status:'waiting',"
            "detail:'等待微信前台'}); updateCounts()"
        )
        assert await evaluate("document.querySelectorAll('.media-status.waiting').length") == 1
        await evaluate(
            "appendMessage({entry_id:'media-fixture',message_id:42,time:1700000000,"
            "direction:'received',sender:'模拟好友',kind:'image',text:'[图片]',"
            "forward_status:'forwarded',detail:'已交给 OneBot'}); updateCounts()"
        )
        assert await evaluate("document.getElementById('message-count').textContent") == "1"
        assert await evaluate("document.querySelectorAll('.media-status.forwarded').length") == 1
        assert await evaluate("document.querySelectorAll('.media-status.waiting').length") == 0
        assert await evaluate("document.querySelector('.message-id').textContent") == "#42"
        await evaluate("document.getElementById('chat-type').value = 'group'; chatMode()")
        assert await evaluate("!document.getElementById('group-fields').hidden")
        assert await evaluate("document.getElementById('private-fields').hidden")
        await evaluate("document.getElementById('chat-name').value = '网页测试群'")
        await evaluate("document.getElementById('group-id').value = '9007199254740993'")
        await evaluate("document.getElementById('group-mention-name').value = '测试机器人'")
        await evaluate("document.getElementById('save-connect').click()")
        await wait_for(
            "document.getElementById('notice').textContent.includes('已连接') && "
            "document.getElementById('chat-kind-summary').textContent.includes('群聊')"
        )
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["chat_type"] == "group" and saved["group_id"] == 9007199254740993
        assert saved["group_mention_name"] == "测试机器人"
        # The monitor was paused above. Resume and verify the new group's visible @ text.
        await evaluate("document.getElementById('toggle-monitor').click()")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{saved['port']}",
            trust_env=False,
            headers={"Authorization": f"Bearer {saved['access_token']}"},
        ) as http:
            result = await http.post(
                "/wxbridge_mock_receive",
                json={
                    "message": "@测试机器人\u2005群消息",
                    "sender": "模拟群成员甲",
                },
            )
            assert result.json()["status"] == "ok"
        await wait_for(
            "document.querySelector('#message-list .message-content p')?.textContent === "
            "'@测试机器人\u2005群消息'"
        )
        assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
        await call("Emulation.clearDeviceMetricsOverride")
        await evaluate("document.getElementById('settings').scrollIntoView({block:'start'})")
        screenshot = await call("Page.captureScreenshot", {"format": "png"})
        output.with_stem(output.stem + "-group").write_bytes(base64.b64decode(screenshot["data"]))
        await call("Page.reload")
        await wait_for("document.getElementById('group-id')?.value === '9007199254740993'")
        assert await evaluate("document.getElementById('chat-type').value") == "group"
        await evaluate("document.getElementById('disconnect').click()")
        await wait_for("document.getElementById('status-label').textContent === '尚未连接'")
        await ws.send(json.dumps({"id": 999999, "method": "Browser.close"}))


def main():
    project = Path(__file__).resolve().parents[1]
    edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    edge /= "Microsoft/Edge/Application/msedge.exe"
    if not edge.is_file():
        raise RuntimeError("This check needs the installed Microsoft Edge browser")
    root = project / "data" / "onebot-checks"
    root.mkdir(parents=True, exist_ok=True)
    server_port, debug_port = free_port(), free_port()
    with tempfile.TemporaryDirectory(prefix="console-", dir=root) as directory:
        temporary = Path(directory).resolve()
        assert temporary.is_relative_to(project.resolve())
        config_path = temporary / "config.json"
        init_config(config_path)  # Deliberately leave chat_name unconfigured.
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["port"] = server_port
        config_path.write_text(json.dumps(config), encoding="utf-8")
        log_path = temporary / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "wxbridge.onebot.cli",
                    "--mock",
                    "--config",
                    str(config_path),
                ],
                cwd=project,
                stdout=log,
                stderr=log,
                creationflags=flags,
            )
            browser = None
            try:
                deadline = time.monotonic() + 15
                with httpx.Client(trust_env=False, timeout=0.5) as http:
                    while True:
                        try:
                            if http.get(f"http://127.0.0.1:{server_port}/").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if server.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Mock console did not start")
                        time.sleep(0.1)
                token = re.search(r"#admin=([A-Za-z0-9_-]+)", log_path.read_text(encoding="utf-8"))
                assert token is not None
                url = f"http://127.0.0.1:{server_port}/#admin={token.group(1)}"
                browser = subprocess.Popen(
                    [
                        str(edge),
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        f"--remote-debugging-port={debug_port}",
                        f"--user-data-dir={temporary / 'edge-profile'}",
                        "--window-size=1440,1100",
                        "about:blank",
                    ],
                    stdout=log,
                    stderr=log,
                    creationflags=flags,
                )
                asyncio.run(
                    browser_check(debug_port, url, root / "console-desktop.png", config_path)
                )
                print(
                    "PASS: unconfigured Web -> fill contact -> save/connect -> reload -> disconnect"
                )
                print("PASS: desktop and mobile layouts; screenshots in data/onebot-checks/")
                print("PASS: Web messages, received/sent, pause/resume, clear/reload, literal HTML")
                print("PASS: media status updates replace the existing row and keep counts correct")
                print("PASS: group form, int64 ID persistence, group monitor, @ text, reload")
            finally:
                for process in (browser, server):
                    if process is not None:
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == "__main__":
    main()
