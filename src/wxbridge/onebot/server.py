import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import configured_chat
from .manager import ConnectionManager
from .protocol import ActionError, response

log = logging.getLogger(__name__)
MAX_REQUEST = 65536
MAX_API_REQUEST = 16 * 1024 * 1024


def auth_status(config, connection):
    header = connection.headers.get("authorization", "")
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return 403
    else:
        token = connection.query_params.get("access_token", "")
    if not token:
        return 401
    return 0 if hmac.compare_digest(token.encode(), config.access_token.encode()) else 403


class Transports:
    def __init__(self, gateway):
        self.gateway = gateway
        self.config = gateway.config
        self.listeners = set()
        self.tasks = []
        self.closed = asyncio.Event()
        self.sessions = set()
        self.http = httpx.AsyncClient(timeout=self.config.http_timeout, trust_env=False)

    async def start(self):
        self.gateway.emit(self.gateway.lifecycle("enable"))
        self.tasks.append(asyncio.create_task(self._publisher()))
        self.tasks.extend(
            asyncio.create_task(self._reverse(url)) for url in self.config.reverse_ws_urls
        )

    async def close(self):
        self.closed.set()
        for close in tuple(self.sessions):
            with contextlib.suppress(Exception):
                await close(1001)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self._http_report(self.gateway.lifecycle("disable"))
        await self.http.aclose()

    async def _publisher(self):
        while True:
            event = await self.gateway.outbox.get()
            try:
                # Match v11's HTTP quick-operation-before-WebSocket ordering.
                await self._http_report(event)
                if event.get("meta_event_type") == "lifecycle":
                    continue  # enable/disable belong exclusively to HTTP POST.
                for queue in tuple(self.listeners):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        self.listeners.discard(queue)
                        queue.get_nowait()
                        queue.put_nowait(None)
                        log.warning("WebSocket 接收端过慢，已断开该订阅")
            finally:
                self.gateway.outbox.task_done()

    async def _http_report(self, event):
        async def post(url):
            body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json", "X-Self-ID": str(self.config.self_id)}
            if self.config.http_post_secret:
                digest = hmac.new(
                    self.config.http_post_secret.encode(), body, hashlib.sha1
                ).hexdigest()
                headers["X-Signature"] = f"sha1={digest}"
            try:
                result = await self.http.post(url, content=body, headers=headers)
                result.raise_for_status()
                if not result.content or event.get("post_type") != "message":
                    return
                operation = result.json()
                if isinstance(operation, dict) and "reply" in operation:
                    target = (
                        {"group_id": event["group_id"]}
                        if event["message_type"] == "group"
                        else {"user_id": event["user_id"]}
                    )
                    reply = await self.gateway.dispatch(
                        {
                            "action": "send_group_msg"
                            if event["message_type"] == "group"
                            else "send_private_msg",
                            "params": {
                                **target,
                                "message": operation["reply"],
                                "auto_escape": operation.get("auto_escape", False),
                            },
                        }
                    )
                    if reply["status"] == "failed":
                        log.error("HTTP 快速回复失败，retcode=%s；不会重试", reply["retcode"])
            except (httpx.HTTPError, ValueError) as exc:
                # Do not log URLs, tokens, event bodies or private message contents.
                log.warning("HTTP 事件上报失败 (%s)，不会重试", type(exc).__name__)

        await asyncio.gather(*(post(url) for url in self.config.http_post_urls))

    async def socket_session(self, send, receive, close, *, role="Universal"):
        queue = asyncio.Queue(maxsize=128)
        send_lock = asyncio.Lock()

        async def write(data):
            async with send_lock:
                await send(json.dumps(data, ensure_ascii=False))

        async def events():
            await write(self.gateway.lifecycle("connect"))
            while True:
                event = await queue.get()
                if event is None:
                    await close(1013)
                    return
                await write(event)

        async def calls():
            while True:
                raw = await receive()
                if role == "Event":
                    continue  # Event-only sockets never execute actions.
                try:
                    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_API_REQUEST:
                        raise ValueError("请求超出大小或类型限制")
                    request = json.loads(raw)
                except (ValueError, UnicodeError):
                    await write(response(error=ActionError("WebSocket 消息需要有效 JSON")))
                    continue
                await write(await self.gateway.dispatch(request))

        tasks = [asyncio.create_task(self.closed.wait())]
        self.sessions.add(close)
        if role != "API":
            self.listeners.add(queue)
            tasks.append(asyncio.create_task(events()))
        tasks.append(asyncio.create_task(calls()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.sessions.discard(close)
            self.listeners.discard(queue)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reverse(self, url):
        headers = {
            "X-Self-ID": str(self.config.self_id),
            "X-Client-Role": "Universal",
            "Authorization": f"Bearer {self.config.access_token}",
        }
        while True:
            try:
                async with connect(
                    url,
                    additional_headers=headers,
                    max_size=MAX_API_REQUEST,
                    max_queue=4,
                    open_timeout=self.config.http_timeout,
                    close_timeout=2,
                    proxy=None,
                ) as socket:
                    log.info("反向 WebSocket 已连接")
                    await self.socket_session(socket.send, socket.recv, socket.close)
            except Exception as exc:
                code = None
                if isinstance(exc, ConnectionClosed):
                    code = getattr(exc.rcvd, "code", None) or getattr(exc.sent, "code", None)
                log.warning(
                    "反向 WebSocket 连接中断 (%s, code=%s)，稍后重连", type(exc).__name__, code
                )
            await asyncio.sleep(self.config.reconnect_interval)


def create_app(config, *, mock=False, client_factory=None, config_path=None, admin_token=None):
    @asynccontextmanager
    async def lifespan(app):
        manager = ConnectionManager(
            config,
            app,
            Transports,
            config_path=config_path,
            mock=mock,
            client_factory=client_factory,
        )
        app.state.manager = manager
        try:
            if configured_chat(config.chat_name):
                await manager.connect()
            log.info("OneBot Web 管理页已就绪，模式=%s", "模拟" if mock else "微信")
            yield
        finally:
            await manager.disconnect()

    app = FastAPI(
        title="wxbridge OneBot v11",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.admin_token = admin_token or secrets.token_urlsafe(32)
    static = Path(__file__).parent / "static"
    app.mount("/console/static", StaticFiles(directory=static), name="console-static")

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        path = request.url.path
        if request.method == "GET" and (
            path in {"/", "/console", "/console/"} or path.startswith("/console/static/")
        ):
            return await call_next(request)
        if request.method in {"GET", "HEAD"} and path.startswith("/media/"):
            # Each registered attachment has its own random capability, never an API token.
            return await call_next(request)
        if path.startswith("/console/api/"):
            # A separate, ephemeral admin token prevents API consumers editing contacts.
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if (
                scheme.lower() != "bearer"
                or not token
                or not hmac.compare_digest(token.encode(), app.state.admin_token.encode())
            ):
                return JSONResponse({"error": "请从启动窗口的管理页面链接打开"}, status_code=401)
            result = await call_next(request)
            result.headers["Cache-Control"] = "no-store"
            return result
        status = auth_status(app.state.manager.config, request)
        if status:
            error = ActionError("缺少或错误的 access_token", 1000 + status)
            return JSONResponse(response(error=error), status_code=status)
        return await call_next(request)

    async def parse_params(request, maximum=MAX_REQUEST):
        params = {k: v for k, v in request.query_params.items() if k != "access_token"}
        if request.method != "POST":
            return params
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > maximum:
                raise ActionError(f"请求正文超过 {maximum // 1024} KiB", 1413)
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in {"application/json", "application/x-www-form-urlencoded"}:
            raise ActionError("POST 仅接受 JSON 或 urlencoded 表单", 1406)
        try:
            if kind == "application/json":
                body = json.loads(chunks)
                if not isinstance(body, dict):
                    raise ValueError("正文不是对象")
            else:
                body = dict(parse_qsl(chunks.decode("utf-8"), keep_blank_values=True))
            params.update(body)
        except (ValueError, UnicodeError) as exc:
            raise ActionError("无法解析请求正文") from exc
        return params

    async def http_api(request, action=None):
        try:
            params = await parse_params(request, MAX_API_REQUEST)
            payload = {"action": action, "params": params} if action else params
            result = await app.state.manager.dispatch(payload)
        except ActionError as exc:
            result = response(error=exc)
        retcode = result["retcode"]
        status = retcode - 1000 if retcode in {1400, 1404, 1406, 1413} else 200
        return JSONResponse(result, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    @app.get("/console/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(
            (static / "index.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @app.get("/console/api/config")
    async def console_config():
        return app.state.manager.view()

    @app.get("/console/api/messages")
    async def console_messages(
        after: int = Query(default=0, ge=0, le=2**53 - 1),
        session: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        return app.state.manager.messages(after=after, session=session, limit=limit)

    @app.post("/console/api/config")
    async def console_save(request: Request):
        try:
            payload = await parse_params(request)
            connect_after_save = payload.pop("connect", True)
            if not isinstance(connect_after_save, bool):
                raise ValueError("connect 必须是布尔值")
            result = await app.state.manager.configure(payload, connect=connect_after_save)
            return {**result, "saved": True}
        except (ValueError, ActionError) as exc:
            if isinstance(exc, ValidationError):
                errors = exc.errors(include_input=False, include_url=False)
                detail = "；".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
            else:
                detail = str(exc)
            return JSONResponse({"error": detail}, status_code=400)
        except OSError:
            return JSONResponse(
                {"error": "配置文件保存失败，请检查路径和写入权限"}, status_code=500
            )

    @app.post("/console/api/connect")
    async def console_connect():
        await app.state.manager.connect()
        return app.state.manager.view()

    @app.post("/console/api/disconnect")
    async def console_disconnect():
        await app.state.manager.disconnect()
        return app.state.manager.view()

    @app.post("/")
    async def root_action(request: Request):
        return await http_api(request)

    @app.api_route("/media/{token}/{name}", methods=["GET", "HEAD"])
    async def media_download(token: str, name: str):
        gateway = app.state.manager.gateway
        if gateway is None:
            return JSONResponse({"error": "微信未连接或媒体缓存不可用"}, status_code=404)
        try:
            asset = gateway.media.describe(token)
            if name != asset["name"]:
                raise ActionError("附件名称不匹配", 100)
        except (ActionError, OSError, ValueError):
            return JSONResponse({"error": "附件不存在或已不可用"}, status_code=404)
        return FileResponse(
            asset["path"],
            filename=asset["name"],
            media_type=asset["content_type"],
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.api_route("/{action}", methods=["GET", "POST"])
    @app.api_route("/{action}/", methods=["GET", "POST"])
    async def action_route(action: str, request: Request):
        return await http_api(request, action)

    @app.websocket("/")
    @app.websocket("/ws")
    @app.websocket("/api")
    @app.websocket("/api/")
    @app.websocket("/event")
    @app.websocket("/event/")
    async def websocket_route(socket: WebSocket):
        status = auth_status(app.state.manager.config, socket)
        if status:
            denial = JSONResponse({"error": "缺少或错误的 access_token"}, status_code=status)
            await socket.send_denial_response(denial)
            return
        if app.state.transports is None:
            await socket.close(code=1013, reason="请先在 Web 管理页配置并连接微信")
            return
        await socket.accept()
        path = socket.url.path.rstrip("/")
        role = {"/api": "API", "/event": "Event"}.get(path, "Universal")
        transports = app.state.transports
        with contextlib.suppress(WebSocketDisconnect, ConnectionClosed):
            await transports.socket_session(
                socket.send_text, socket.receive_text, socket.close, role=role
            )

    return app
