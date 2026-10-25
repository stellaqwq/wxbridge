import asyncio
import hashlib
import json
import logging
from contextlib import ExitStack
from importlib.metadata import version

from wxbridge import WeChat
from wxbridge.storage import exclusive_ledger

from .config import OneBotConfig, configured_chat, save_config
from .gateway import Gateway
from .identity import identity_scope
from .mock import MockBackend
from .monitor import MessageMonitor
from .protocol import ActionError, number, response

log = logging.getLogger(__name__)


class ConnectionManager:
    EDITABLE = frozenset(
        {
            "chat_name",
            "nickname",
            "self_id",
            "peer_id",
            "enable_accessibility",
            "foreground",
            "reverse_ws_urls",
            "http_post_urls",
            "media_base_url",
            "chat_type",
            "group_id",
            "group_mention_name",
        }
    )

    def __init__(
        self, config, app, transport_factory, *, config_path=None, mock=False, client_factory=None
    ):
        self.config = config
        self.app = app
        self.transport_factory = transport_factory
        self.config_path = config_path
        self.mock = mock
        self.client_factory = client_factory
        self.gateway = None
        self.transports = None
        self.lease = None
        self.lock = asyncio.Lock()
        self.error = ""
        self.connecting = False
        self.monitor = MessageMonitor()
        self.monitor.set_scope(config)
        self._bind()

    def _bind(self):
        self.app.state.gateway = self.gateway
        self.app.state.transports = self.transports

    def status(self):
        if self.gateway is not None:
            return self.gateway.status()
        return {
            "online": None,
            "good": False,
            "wxbridge": {
                "ui_ready": False,
                "mock": self.mock,
                "last_error": self.error,
                "event_overflow": False,
            },
        }

    def messages(self, *, after=0, session=None, limit=100):
        status = self.status()
        return {
            **self.monitor.page(after=after, session=session, limit=limit),
            "connected": self.gateway is not None,
            "ready": status["good"],
            "chat_name": self.config.chat_name if configured_chat(self.config.chat_name) else "",
            "chat_type": self.config.chat_type,
            "error": self.error or status["wxbridge"]["last_error"],
        }

    def view(self):
        values = self.config.model_dump(mode="json")
        values["self_id"] = str(self.config.self_id)
        values["peer_id"] = str(self.config.peer_id)
        values["group_id"] = str(self.config.group_id)
        if not configured_chat(self.config.chat_name):
            values["chat_name"] = ""
        phase = "unconfigured" if not configured_chat(self.config.chat_name) else "disconnected"
        if self.gateway is not None:
            phase = "connected" if self.gateway.status()["good"] else "attention"
        if self.connecting:
            phase = "connecting"
        # Signing secrets are not needed by this form.
        values.pop("http_post_secret", None)
        return {
            "config": values,
            "phase": phase,
            "mock": self.mock,
            "connected": self.gateway is not None,
            "status": self.status(),
            "error": self.error or self.status()["wxbridge"]["last_error"],
            "config_path": str(self.config_path) if self.config_path else None,
        }

    def _make_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        if self.mock:
            return WeChat(
                backend_factory=lambda: MockBackend(self.config.chat_name, self.config.chat_type)
            )
        return WeChat(
            enable_accessibility=self.config.enable_accessibility,
            foreground=self.config.foreground,
            chat_type=self.config.chat_type,
        )

    def _runtime_config(self):
        config = self.config.model_copy()
        if self.mock and not config.database.stem.endswith("-mock"):
            config.database = config.database.with_name(config.database.stem + "-mock.sqlite3")
        return config

    async def _disconnect(self):
        gateway, transports, lease = self.gateway, self.transports, self.lease
        self.gateway = self.transports = self.lease = None
        self._bind()
        try:
            if transports is not None:
                await transports.close()
        finally:
            try:
                if gateway is not None:
                    await gateway.close()
            finally:
                if lease is not None:
                    lease.close()

    async def disconnect(self):
        async with self.lock:
            await self._disconnect()
            self.error = ""

    async def _connect(self):
        await self._disconnect()
        self.error = ""
        if not configured_chat(self.config.chat_name):
            self.error = "请在页面填写微信联系人或群聊的准确名称，再保存并连接。"
            return False
        self.connecting = True
        client = gateway = transports = None
        lease = ExitStack()
        try:
            runtime_config = self._runtime_config()
            lease.enter_context(exclusive_ledger(runtime_config.database))
            client = await asyncio.to_thread(self._make_client)
            gateway = Gateway(
                runtime_config, client, mock=self.mock, on_message=self.monitor.record
            )
            await gateway.start()
            transports = self.transport_factory(gateway)
            await transports.start()
            self.gateway, self.transports, self.lease = gateway, transports, lease
            self._bind()
            return True
        except BaseException as exc:
            try:
                if transports is not None:
                    await transports.close()
            finally:
                try:
                    if gateway is not None:
                        await gateway.close()
                    elif client is not None:
                        await asyncio.to_thread(client.close)
                finally:
                    lease.close()
            if not isinstance(exc, Exception):
                raise
            self.error = str(exc)
            log.warning("微信未连接 (%s)，Web 配置页仍可使用", type(exc).__name__)
            return False
        finally:
            self.connecting = False

    async def connect(self):
        async with self.lock:
            return await self._connect()

    async def configure(self, updates, *, connect=True):
        if not isinstance(updates, dict) or set(updates) - self.EDITABLE:
            raise ValueError("配置包含不支持的字段")
        updates = dict(updates)
        for field in ("self_id", "peer_id", "group_id"):
            if field in updates:
                updates[field] = number(updates[field], field)
        if "chat_name" in updates and isinstance(updates["chat_name"], str):
            updates["chat_name"] = updates["chat_name"].strip()
        async with self.lock:
            candidate = OneBotConfig.model_validate({**self.config.model_dump(), **updates})
            if not configured_chat(candidate.chat_name):
                raise ValueError("请填写微信联系人或群聊的准确名称")

            if (
                identity_scope(candidate) != identity_scope(self.config)
                and self._runtime_config().database.exists()
            ):
                digest = hashlib.sha256(json.dumps(identity_scope(candidate)).encode()).hexdigest()[
                    :12
                ]
                candidate.database = self.config.database.parent / f"onebot-{digest}.sqlite3"
            # Validate and persist before disconnecting a working session.
            if self.config_path is not None:
                save_config(self.config_path, candidate)
            await self._disconnect()
            self.config = candidate
            self.monitor.set_scope(candidate)
            self.error = ""
            if connect:
                await self._connect()
            return self.view()

    async def dispatch(self, request):
        async with self.lock:
            if self.gateway is not None:
                return await self.gateway.dispatch(request)
            try:
                if not isinstance(request, dict) or not isinstance(request.get("action"), str):
                    raise ActionError("API 请求需要 action 字符串")
                action = request["action"]
                if action == "get_status":
                    data = self.status()
                elif action == "get_login_info":
                    data = {"user_id": self.config.self_id, "nickname": self.config.nickname}
                elif action == "get_version_info":
                    data = {
                        "app_name": "wxbridge",
                        "app_version": version("wxbridge"),
                        "protocol_version": "v11",
                        "platform": "wechat",
                    }
                elif action not in Gateway.ACTIONS:
                    raise ActionError("当前 wxbridge 适配层未实现此 API", 1404)
                else:
                    raise ActionError("微信尚未连接，请在 Web 管理页保存配置并连接微信", 100)
                result = response(data)
            except ActionError as exc:
                result = response(error=exc)
            if isinstance(request, dict) and "echo" in request:
                result["echo"] = request["echo"]
            return result
