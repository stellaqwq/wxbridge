import asyncio
import logging
import sqlite3
import time
import uuid
from collections import deque
from importlib.metadata import version

from wxbridge.errors import DeliveryUncertain, WeChatError

from .groups import member_info, mention_segments
from .media import MediaCache
from .protocol import ActionError, boolean, escape, number, outgoing_message, response, text_message
from .store import MessageStore

log = logging.getLogger(__name__)


class Gateway:
    ACTIONS = frozenset(
        {
            "send_private_msg",
            "send_group_msg",
            "send_msg",
            "get_msg",
            "get_image",
            "get_private_file_url",
            "get_login_info",
            "get_friend_list",
            "get_stranger_info",
            "get_group_list",
            "get_group_info",
            "get_group_member_info",
            "get_group_member_list",
            "get_group_file_url",
            "get_status",
            "get_version_info",
            "can_send_image",
            "can_send_record",
            "get_supported_actions",
        }
    )

    def __init__(self, config, client, *, mock=False, on_message=None):
        self.config = config
        self.client = client
        self.mock = mock
        self.store = MessageStore(config)
        self.media = MediaCache(config, self.store)
        self.pending_media = {}
        self.seen_media = deque(maxlen=2048)
        self.session = uuid.uuid4().hex
        self.ui_lock = asyncio.Lock()
        self.rate_lock = asyncio.Lock()
        self.last_rate_call = 0.0
        self.outbox = asyncio.Queue(maxsize=256)
        self.running = False
        self.ready = False
        self.event_overflow = False
        self.last_error = ""
        self.tasks = []
        self.jobs = set()
        self.on_message = on_message
        self.group_member_count = 0

    @property
    def is_group(self):
        return self.config.chat_type == "group"

    def _sender(self, message):
        if message.attr == "self":
            return self.config.self_id, self.config.nickname
        if not self.is_group:
            return self.config.peer_id, self.config.chat_name
        key = message.info.get("sender_key") or (
            "name:" + message.sender if message.sender else "unknown"
        )
        return self.store.member(key, message.sender)

    @property
    def actions(self):
        return self.ACTIONS | ({"wxbridge_mock_receive"} if self.mock else set())

    def status(self):
        return {
            # UI availability cannot prove the account's server connection status.
            "online": True if self.mock else None,
            "good": self.running and self.ready and not self.event_overflow,
            "wxbridge": {
                "ui_ready": self.ready,
                "mock": self.mock,
                "last_error": self.last_error,
                "event_overflow": self.event_overflow,
                "media_pending": len(self.pending_media),
            },
        }

    def lifecycle(self, subtype):
        return {
            "time": int(time.time()),
            "self_id": self.config.self_id,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": subtype,
        }

    async def start(self):
        async with self.ui_lock:
            info = await asyncio.to_thread(self.client.ChatInfo)
            self._check_chat(info)
            # Startup history is a baseline, never an inbound event.
            await asyncio.to_thread(self.client.GetNextNewMessage, self.config.chat_name)
        self.running = self.ready = True
        self.tasks = [
            asyncio.create_task(self._poll()),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._media_loop()),
        ]

    def _check_chat(self, info):
        if (
            info.get("chat_name") != self.config.chat_name
            or info.get("chat_type") != self.config.chat_type
        ):
            raise WeChatError("请打开配置中名称与类型均匹配的唯一微信会话")
        self.group_member_count = info.get("member_count", self.group_member_count)

    async def close(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Accepted asynchronous sends finish once; never cancel/replay a submitted send.
        if self.jobs:
            await asyncio.gather(*tuple(self.jobs), return_exceptions=True)
        try:
            await asyncio.to_thread(self.client.close)
        finally:
            self.store.close()

    def emit(self, event):
        try:
            self.outbox.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.event_overflow = True
            log.error("事件队列已满，跳过此次上报；请检查事件接收端")
            return False

    async def poll_once(self):
        async with self.ui_lock:
            messages = await asyncio.to_thread(self.client.GetNextNewMessage, self.config.chat_name)
            self.ready = True
            self.last_error = ""
            for message in messages:
                if message.attr not in {"friend", "self"}:
                    continue
                incoming = message.attr == "friend"
                sender_id, nickname = self._sender(message)
                if message.type != "text":
                    if message.type in {"image", "file", "voice"}:
                        self._queue_media(message, incoming, nickname, sender_id)
                    continue
                if not message.content:
                    continue
                payload = self._payload(message.content, sender_id, nickname)
                if self.is_group and incoming:
                    payload["message"], payload["raw_message"] = mention_segments(
                        message.content, self.config.group_mention_name, self.config.self_id
                    )
                    payload["wxbridge"] = {
                        "sender_identity": message.info.get("sender_source", "local_alias")
                    }
                direction = "in" if incoming else "out"
                mid, fresh = self.store.add(f"{self.session}:{direction}:{message.id}", payload)
                if fresh:
                    self._notify_message(mid, incoming=incoming, text=message.content)
                if fresh and incoming:
                    self._emit_message(mid)

    def _emit_message(self, mid):
        payload = self.store.get(mid)
        return self.emit(
            {
                **payload,
                "self_id": self.config.self_id,
                "post_type": "message",
                "sub_type": "normal" if self.is_group else "friend",
                "user_id": payload["sender"]["user_id"],
                "font": 0,
            }
        )

    def _queue_media(self, message, incoming, nickname, sender_id=None):
        source = f"{self.session}:{'in' if incoming else 'out'}:{message.id}"
        if source in self.seen_media:
            return
        self.seen_media.append(source)
        entry = {
            "entry_id": source,
            "message_id": None,
            "time": int(time.time()),
            "direction": "received" if incoming else "sent",
            "chat_name": self.config.chat_name,
            "sender": nickname,
            "sender_id": sender_id if sender_id is not None else self.config.peer_id,
            "kind": message.type,
            "text": message.content,
            "forward_status": "pending" if incoming else "display_only",
            "detail": "等待保存附件或语音转文字" if incoming else "自己发出的媒体仅显示",
        }
        if incoming:
            if len(self.pending_media) >= 32:
                entry.update(forward_status="failed", detail="媒体处理队列已满，请稍后重新发送")
                log.warning("媒体处理队列已满，未上报新媒体")
            else:
                self.pending_media[source] = {
                    "message": message,
                    "entry": entry,
                    "due": 0,
                    "deadline": time.monotonic() + 300,
                }
        self._notify_entry(entry)

    async def _media_loop(self):
        while True:
            await self.process_media_once()
            await asyncio.sleep(0.25)

    async def process_media_once(self):
        """Process one due attachment; a failed attachment never stops text polling."""
        now = time.monotonic()
        candidate = next(
            ((key, item) for key, item in self.pending_media.items() if item["due"] <= now), None
        )
        if candidate is None:
            return
        source, item = candidate
        message, entry = item["message"], item["entry"]
        entry.update(forward_status="processing", detail="正在转写语音或保存附件")
        self._notify_entry(entry)
        try:
            async with self.ui_lock:
                self._check_chat(await asyncio.to_thread(self.client.ChatInfo))
                payload = self._payload("", entry["sender_id"], entry["sender"])
                payload["time"] = entry["time"]
                payload["wxbridge"] = {"original_type": message.type}
                if message.type == "voice":
                    transcript = message.info.get("transcript") or await asyncio.to_thread(
                        self.client.TranscribeVoice, message
                    )
                    if not isinstance(transcript, str) or not transcript.strip():
                        raise WeChatError("微信没有返回语音转文字结果")
                    payload["message"] = [{"type": "text", "data": {"text": transcript}}]
                    payload["raw_message"] = escape(transcript)
                    entry["text"] = transcript
                else:
                    path = await asyncio.to_thread(
                        self.client.DownloadAttachment, message, self.media.directory
                    )
                    token, asset = self.media.register(path, message.info.get("filename"))
                    data = {"file": token, "url": asset["url"]}
                    if message.type == "file":
                        data.update(
                            file_id=token,
                            name=asset["name"],
                            file_name=asset["name"],
                            file_size=asset["size"],
                        )
                    payload["message"] = [{"type": message.type, "data": data}]
                    payload["raw_message"] = f"[CQ:{message.type},file={token}]"
                mid, fresh = self.store.add(source, payload)
            queued = self._emit_message(mid) if fresh else True
            entry.update(
                message_id=mid,
                forward_status="forwarded" if queued else "failed",
                detail=("语音已转文字并交给 OneBot" if message.type == "voice" else "已交给 OneBot")
                if queued
                else "OneBot 事件队列已满，未上报；请检查接收端",
            )
            log.info("媒体消息处理完成 kind=%s message_id=%s queued=%s", message.type, mid, queued)
        except Exception as exc:
            # No click is submitted while WeChat lacks focus. Retry that condition only;
            # failed saves/transcriptions are not blindly replayed or replaced by summaries.
            reason = str(exc)
            waiting = isinstance(exc, WeChatError) and "前台" in reason and now < item["deadline"]
            if waiting:
                item["due"] = time.monotonic() + 5
                entry.update(
                    forward_status="waiting",
                    detail="请让微信目标会话获得前台焦点，5 秒后重试，最多等待 5 分钟",
                )
                self._notify_entry(entry)
                return
            entry.update(forward_status="failed", detail=f"未上报：{reason}")
            log.warning("媒体消息未上报 kind=%s reason=%s", message.type, reason)
        self.pending_media.pop(source, None)
        self._notify_entry(entry)

    def _notify_message(self, mid, *, incoming, text=None):
        if self.on_message is None:
            return
        try:
            payload = self.store.get(mid)
            self._notify_entry(
                {
                    "message_id": mid,
                    "time": payload["time"],
                    "direction": "received" if incoming else "sent",
                    "chat_name": self.config.chat_name,
                    "sender": payload["sender"]["nickname"],
                    "kind": "text",
                    "text": text if text is not None else payload["message"][0]["data"]["text"],
                }
            )
        except Exception as exc:
            log.warning("网页消息显示记录失败 (%s)，不影响 OneBot 上报", type(exc).__name__)

    def _notify_entry(self, entry):
        if self.on_message is not None:
            try:
                self.on_message(entry)
            except Exception as exc:
                log.warning("网页消息显示记录失败 (%s)，不影响 OneBot 上报", type(exc).__name__)

    async def _poll(self):
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                self.ready = False
                reason = "微信会话不可读取，请检查联系人、无障碍状态，并保持微信消息区域可见"
                if self.last_error != reason:
                    log.warning("%s (%s)", reason, type(exc).__name__)
                self.last_error = reason
            await asyncio.sleep(self.config.poll_interval)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.config.heartbeat_interval)
            self.emit(
                {
                    "time": int(time.time()),
                    "self_id": self.config.self_id,
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "status": self.status(),
                    "interval": round(self.config.heartbeat_interval * 1000),
                }
            )

    def _payload(self, text, sender_id, nickname):
        payload = {
            "time": int(time.time()),
            "message_type": "group" if self.is_group else "private",
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": escape(text),
            "sender": {"user_id": sender_id, "nickname": nickname, "sex": "unknown", "age": 0},
        }
        if self.is_group:
            payload.update(
                group_id=self.config.group_id, group_name=self.config.chat_name, anonymous=None
            )
            payload["sender"].update(card=nickname, role="member", area="", level="", title="")
        return payload

    def _check_group(self, params):
        if not self.is_group or number(params.get("group_id"), "group_id") != self.config.group_id:
            raise ActionError("group_id 不在当前配置的群聊映射中", 100)

    def _check_target(self, action, params):
        kind = params.get("message_type", "group" if "group_id" in params else "private")
        if action == "send_group_msg":
            if "user_id" in params or params.get("message_type", "group") != "group":
                raise ActionError("群消息目标参数不一致", 100)
            kind = "group"
        elif action == "send_private_msg" and (kind != "private" or "group_id" in params):
            raise ActionError("私聊消息不能指定群聊目标", 100)
        if kind == "group":
            if "user_id" in params:
                raise ActionError("不能同时指定群聊与私聊目标", 100)
            self._check_group(params)
        elif kind == "private":
            if self.is_group or "group_id" in params:
                raise ActionError("当前会话没有接入该私聊", 100)
            if number(params.get("user_id"), "user_id") != self.config.peer_id:
                raise ActionError("user_id 不在配置的联系人映射中", 100)
        else:
            raise ActionError("message_type 只能是 private 或 group", 100)

    async def dispatch(self, request):
        try:
            if not isinstance(request, dict):
                raise ActionError("API 请求必须是 JSON 对象")
            action = request.get("action")
            params = request.get("params", {})
            if not isinstance(action, str) or not action or not isinstance(params, dict):
                raise ActionError("需要 action 字符串及 params 对象")
            suffix = next((s for s in ("_rate_limited", "_async") if action.endswith(s)), "")
            base = action.removesuffix(suffix) if suffix else action
            if base not in self.actions:
                raise ActionError("当前 wxbridge 适配层未实现此 API", 1404)
            if suffix:
                if len(self.jobs) >= 64:
                    raise ActionError("异步任务队列已满", 100)
                task = asyncio.create_task(self._job(base, params, suffix == "_rate_limited"))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)
                result = {"status": "async", "retcode": 1, "data": None}
            else:
                result = response(await self._execute(base, params))
        except ActionError as exc:
            result = response(error=exc)
        except DeliveryUncertain as exc:
            log.warning("微信发送未取得确认：%s；不会自动重发", exc)
            result = response(
                error=ActionError(
                    "发送结果不确定，请在微信核对，禁止自动重发",
                    1200,
                    data={"delivery_status": "unknown", "retryable": False},
                )
            )
        except (WeChatError, OSError, ValueError) as exc:
            result = response(error=ActionError(str(exc), 100))
        except Exception:
            log.exception("OneBot API 执行失败")
            result = response(error=ActionError("适配器内部错误，请查看本地日志", 1500))
        if isinstance(request, dict) and "echo" in request:
            result["echo"] = request["echo"]
        return result

    async def _job(self, action, params, rate_limited):
        if rate_limited:
            async with self.rate_lock:
                delay = self.config.rate_limit_interval - (time.monotonic() - self.last_rate_call)
                await asyncio.sleep(max(0, delay))
                result = await self.dispatch({"action": action, "params": params})
                self.last_rate_call = time.monotonic()
        else:
            result = await self.dispatch({"action": action, "params": params})
        if result["status"] == "failed":
            log.error("异步 API %s 失败，retcode=%s；不会重试", action, result["retcode"])

    async def _execute(self, action, params):
        config = self.config
        if action in {"send_private_msg", "send_group_msg", "send_msg"}:
            self._check_target(action, params)
            kind, content = outgoing_message(
                params.get("message"), boolean(params.get("auto_escape", False))
            )
            asset = token = None
            if kind == "image":
                # Resolve and validate before entering the irreversible UI send boundary.
                token, asset = self.media.prepare_image(content)
            async with self.ui_lock:
                self._check_chat(await asyncio.to_thread(self.client.ChatInfo))
                if kind == "image":
                    result = await asyncio.to_thread(
                        self.client.SendImage, asset["path"], who=config.chat_name
                    )
                    payload = self._payload("", config.self_id, config.nickname)
                    payload["message"] = [
                        {"type": "image", "data": {"file": token, "url": asset["url"]}}
                    ]
                    payload["raw_message"] = f"[CQ:image,file={token}]"
                else:
                    result = await asyncio.to_thread(
                        self.client.SendMsg, content, who=config.chat_name
                    )
                    payload = self._payload(content, config.self_id, config.nickname)
                if result.status != "confirmed_in_ui":
                    raise DeliveryUncertain("未获得微信界面确认")
                try:
                    mid, fresh = self.store.add(
                        f"{self.session}:out:{result.message_id}",
                        payload,
                    )
                    if fresh:
                        if kind == "image":
                            source = f"{self.session}:out:{result.message_id}"
                            self.seen_media.append(source)
                            self._notify_entry(
                                {
                                    "entry_id": source,
                                    "message_id": mid,
                                    "time": payload["time"],
                                    "direction": "sent",
                                    "chat_name": config.chat_name,
                                    "sender": config.nickname,
                                    "kind": "image",
                                    "text": "[图片]",
                                    "forward_status": "sent",
                                    "detail": "微信界面已确认图片发出",
                                }
                            )
                        else:
                            self._notify_message(mid, incoming=False)
                except sqlite3.Error as exc:
                    raise DeliveryUncertain("消息已提交，但消息库写入失败") from exc
            return {"message_id": mid}
        if action == "get_msg":
            mid = number(params.get("message_id"), "message_id", maximum=2**31 - 1)
            data = self.store.get(mid)
            if data is None:
                raise ActionError("消息不在本地消息库中", 100)
            return data
        if action in {"get_image", "get_private_file_url"}:
            token = params.get("file" if action == "get_image" else "file_id")
            asset = self.media.describe(token)
            if action == "get_private_file_url":
                return {"url": asset["url"], "file_name": asset["name"]}
            return {
                "file": asset["path"],
                "url": asset["url"],
                "size": asset["size"],
                "filename": asset["name"],
            }
        if action == "get_login_info":
            return {"user_id": config.self_id, "nickname": config.nickname}
        if action == "get_friend_list":
            if self.is_group:
                return []
            return [
                {
                    "user_id": config.peer_id,
                    "nickname": config.chat_name,
                    "remark": config.chat_name,
                }
            ]
        if action == "get_stranger_info":
            uid = number(params.get("user_id"), "user_id")
            if self.is_group and uid != config.self_id:
                member = self.store.get_member(uid)
                if member is None:
                    raise ActionError("此成员尚未在当前群聊中被监听到", 100)
                return {**member, "sex": "unknown", "age": 0}
            if uid not in {config.self_id, config.peer_id}:
                raise ActionError("此 ID 不在本地映射中，无法查询微信资料", 100)
            nickname = config.nickname if uid == config.self_id else config.chat_name
            return {"user_id": uid, "nickname": nickname, "sex": "unknown", "age": 0}
        if action == "get_group_list":
            return (
                [{"group_id": config.group_id, "group_name": config.chat_name}]
                if self.is_group
                else []
            )
        if action == "get_group_info":
            self._check_group(params)
            return {
                "group_id": config.group_id,
                "group_name": config.chat_name,
                "member_count": self.group_member_count,
                "max_member_count": 0,
            }
        if action == "get_group_member_info":
            self._check_group(params)
            uid = number(params.get("user_id"), "user_id")
            if uid == config.self_id:
                return member_info(
                    config.group_id, uid, config.group_mention_name or config.nickname
                )
            member = self.store.get_member(uid)
            if member is None:
                raise ActionError("此成员尚未在当前群聊中被监听到", 100)
            return member_info(config.group_id, uid, member["nickname"], member["last_sent_time"])
        if action == "get_group_member_list":
            self._check_group(params)
            return [
                member_info(
                    config.group_id, config.self_id, config.group_mention_name or config.nickname
                )
            ] + [
                member_info(config.group_id, m["user_id"], m["nickname"], m["last_sent_time"])
                for m in self.store.members()
            ]
        if action == "get_group_file_url":
            self._check_group(params)
            asset = self.media.describe(params.get("file_id"))
            return {"url": asset["url"], "file_name": asset["name"]}
        if action == "get_status":
            return self.status()
        if action == "get_version_info":
            return {
                "app_name": "wxbridge",
                "app_version": version("wxbridge"),
                "protocol_version": "v11",
                "platform": "wechat",
                "supported_actions": sorted(self.actions),
            }
        if action in {"can_send_image", "can_send_record"}:
            return {"yes": action == "can_send_image"}
        if action == "get_supported_actions":
            return sorted(self.actions)
        if action == "wxbridge_mock_receive" and self.mock:
            text = text_message(params.get("message"), True)
            sender = params.get("sender")
            if sender is not None and (not isinstance(sender, str) or not 0 < len(sender) <= 100):
                raise ActionError("模拟 sender 需要 1 到 100 字的名称")
            async with self.ui_lock:
                await asyncio.to_thread(self.client._call, "inject", text, "friend", "text", sender)
            await self.poll_once()
            return None
        raise ActionError("未实现的 API", 1404)
