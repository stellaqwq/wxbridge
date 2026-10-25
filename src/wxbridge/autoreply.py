import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path

from .errors import DeliveryUncertain, WeChatError
from .storage import exclusive_ledger

log = logging.getLogger(__name__)


class AutoReply:
    """Single private-chat AI worker with persistent claims and no automatic resend.

    `reply` is user-supplied. Network/model work never runs on the COM thread.
    """

    def __init__(
        self,
        client,
        chat,
        reply,
        *,
        database="data/replies.sqlite3",
        interval=1.0,
        max_age=120.0,
        min_send_interval=2.0,
        predicate=None,
        message_types=("text",),
    ):
        if interval < 0.1 or max_age <= 0 or min_send_interval < 0:
            raise ValueError("轮询、消息年龄或发送间隔无效")
        self.client, self.chat, self.reply = client, chat, reply
        self.database = Path(database)
        self.interval, self.max_age = interval, max_age
        self.min_send_interval = min_send_interval
        self.last_error = None
        self.predicate = predicate or (lambda _: True)
        self.message_types = frozenset(message_types)
        if not self.message_types or not self.message_types <= {"text", "voice"}:
            raise ValueError("自动回复仅支持显式选定的文字或语音类型")

    def run(self, stop=None):
        if stop is not None and stop.is_set():
            return
        with exclusive_ledger(self.database):
            self._run(stop)

    def _run(self, stop=None):
        stopper = stop or threading.Event()
        inbox = queue.Queue(maxsize=1000)
        poll_stop = threading.Event()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY, "
                "chat TEXT NOT NULL, state TEXT NOT NULL, error TEXT)"
            )
            db.execute(
                "UPDATE replies SET state='unknown', error='上次运行处理未结束' "
                "WHERE state='processing'"
            )
            db.commit()
            self.client.ChatWith(self.chat)

            # ChatWith establishes the baseline; no pre-existing messages are replayed.
            def poll():
                while not poll_stop.is_set() and not stopper.is_set():
                    try:
                        for message in self.client.GetNextNewMessage(self.chat):
                            if (
                                message.attr == "friend"
                                and message.type in self.message_types
                                and self.predicate(message)
                            ):
                                inbox.put_nowait((time.monotonic(), message))
                        self.last_error = None
                    except queue.Full:
                        self.last_error = "消息队列已满，监听已停止"
                        poll_stop.set()
                    except WeChatError as exc:
                        self.last_error = str(exc)
                    except Exception:
                        self.last_error = "读取消息异常，监听已停止"
                        poll_stop.set()
                    poll_stop.wait(self.interval)

            listener = threading.Thread(target=poll, name="wxbridge-poll", daemon=True)
            listener.start()
            last_send = 0.0
            try:
                while not stopper.is_set() and not poll_stop.is_set():
                    try:
                        observed, message = inbox.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    with db:
                        inserted = db.execute(
                            "INSERT OR IGNORE INTO replies VALUES (?,?,'processing',NULL)",
                            (message.id, self.chat),
                        ).rowcount
                    if not inserted:
                        continue
                    state, error = "failed", None
                    try:
                        if time.monotonic() - observed > self.max_age:
                            raise WeChatError("消息等待过久，已跳过回复")
                        answer = self.reply(message)
                        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
                            raise WeChatError("模型没有返回可发送的完整文本")
                        delay = max(0, self.min_send_interval - (time.monotonic() - last_send))
                        if stopper.wait(delay) or poll_stop.is_set():
                            raise WeChatError("运行已停止，未提交回复")
                        if time.monotonic() - observed > self.max_age:
                            raise WeChatError("模型处理超时，已跳过回复")
                        self.client.SendMsg(
                            answer, who=self.chat, expected_context=message.info.get("context_id")
                        )
                        last_send = time.monotonic()
                        state = "confirmed_in_ui"
                    except DeliveryUncertain:
                        last_send = time.monotonic()
                        state, error = "unknown", "发送结果不确定，未重发，请检查微信"
                    except WeChatError as exc:
                        error = str(exc)
                    except Exception:
                        # Do not persist model exceptions, which can contain credentials.
                        error = "模型或回复处理失败"
                    with db:
                        db.execute(
                            "UPDATE replies SET state=?,error=? WHERE id=?",
                            (state, error, message.id),
                        )
                    if error:
                        log.warning("%s", error)
            finally:
                poll_stop.set()
                listener.join()
