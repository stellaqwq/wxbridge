import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .errors import BatchSendError, ChatChanged, DeliveryUncertain, WeChatError
from .models import MediaSendResult, SendResult
from .tracking import MessageTracker


class WeChat:
    """Single visible chat client. All UI/COM work belongs to one dedicated thread."""

    def __init__(
        self,
        *,
        enable_accessibility=False,
        foreground=False,
        hwnd=None,
        backend_factory=None,
        send_timeout=5.0,
        chat_type=None,
    ):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wxbridge-ui")
        self._lock = threading.RLock()
        self._tracker = MessageTracker()
        self._closed = False
        self._timeout = send_timeout
        self._backend = None
        try:
            if backend_factory is None:
                from .windows import WindowsBackend

                def backend_factory():
                    return WindowsBackend(
                        hwnd=hwnd,
                        enable_accessibility=enable_accessibility,
                        foreground=foreground,
                        expected_chat_type=chat_type,
                    )

            self._backend = self._pool.submit(backend_factory).result()
        except BaseException:
            self._pool.shutdown(wait=True)
            raise

    def _call(self, method, *args):
        if self._closed:
            raise WeChatError("客户端已经关闭")
        return self._pool.submit(getattr(self._backend, method), *args).result()

    def ChatInfo(self):
        with self._lock:
            snapshot = self._call("snapshot")
            return {
                "chat_name": snapshot.chat_name,
                "chat_type": snapshot.chat_type,
                **snapshot.info,
            }

    def ChatWith(self, who, exact=True):
        if not exact:
            raise ValueError("仅支持精确匹配聊天名称")
        if not isinstance(who, str) or not who.strip():
            raise ValueError("聊天名称不能为空")
        with self._lock:
            self._tracker.reset()
            self._call("open_chat", who)
            snapshot = self._call("snapshot")
            if snapshot.chat_name != who:
                raise ChatChanged("打开的聊天与指定名称不一致")
            self._tracker.update(snapshot)
            return True

    def GetAllMessage(self):
        with self._lock:
            return list(self._call("snapshot").messages)

    def GetNextNewMessage(self, who=None):
        with self._lock:
            try:
                snapshot = self._call("snapshot")
                if who is not None and snapshot.chat_name != who:
                    raise ChatChanged("当前会话已经改变，暂停监听")
                return self._tracker.update(snapshot)
            except Exception:
                self._tracker.reset()
                raise

    def SendMsg(self, msg, who=None, clear=True, exact=True, *, expected_context=None):
        if not isinstance(msg, str) or not msg.strip() or len(msg) > 4000:
            raise ValueError("文字必须非空且不超过 4000 字符")
        if not exact or not clear:
            raise ValueError("仅支持精确匹配目标且不拼接已有草稿")
        with self._lock:
            snapshot = self._call("snapshot")
            expected = who or snapshot.chat_name
            if snapshot.chat_name != expected:
                raise ChatChanged("目标聊天与当前窗口不一致，已停止发送")
            if expected_context is not None and snapshot.context_id != expected_context:
                raise ChatChanged("消息所属的聊天控件已经重建，已取消这次回复")
            before = {m.id for m in snapshot.messages}
            self._call("compose", msg, expected)
            # Submission is the only irreversible boundary; never retry it.
            try:
                self._call("submit", expected, msg)
                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    after = self._call("snapshot")
                    if after.chat_name != expected:
                        raise DeliveryUncertain("提交后会话改变，请人工核对投递结果")
                    for message in after.messages:
                        if (
                            message.id not in before
                            and message.attr == "self"
                            and message.type == "text"
                            and message.content == msg
                        ):
                            return SendResult(expected, msg, message.id)
                    time.sleep(0.1)
            except DeliveryUncertain:
                raise
            except Exception as exc:
                raise DeliveryUncertain("提交阶段异常，不能确定是否发出；请勿自动重发") from exc
            raise DeliveryUncertain("没有读到新发出的相同正文，请在微信中核对")

    def SendFile(self, path, who=None, *, expected_context=None):
        """Send one attachment; image extensions use Weixin's native image preview."""
        from .media import local_attachment

        attachment = local_attachment(path)
        return self._send_attachment(attachment, who, expected_context)

    def SendImage(self, path, who=None, *, expected_context=None):
        from .media import local_attachment

        attachment = local_attachment(path, image=True)
        return self._send_attachment(attachment, who, expected_context)

    def SendFiles(self, paths, who=None, *, expected_context=None):
        """Prevalidate all paths, then submit each once. Stop on the first failure."""
        from .media import local_attachment

        if isinstance(paths, (str, bytes)) or hasattr(paths, "__fspath__"):
            raise ValueError("SendFiles 需要路径列表；单个文件请使用 SendFile")
        attachments = [local_attachment(path) for path in paths]
        if not attachments:
            raise ValueError("附件列表不能为空")
        with self._lock:
            baseline = self._call("snapshot")
            expected = who or baseline.chat_name
            context = expected_context if expected_context is not None else baseline.context_id
            completed = []
            for attachment in attachments:
                try:
                    completed.append(self._send_attachment(attachment, expected, context))
                except Exception as exc:
                    raise BatchSendError(
                        completed, attachment[0], isinstance(exc, DeliveryUncertain)
                    ) from exc
            return completed

    def _send_attachment(self, attachment, who, expected_context):
        path, kind, file_state = attachment
        with self._lock:
            snapshot = self._call("snapshot")
            expected = who or snapshot.chat_name
            if snapshot.chat_name != expected:
                raise ChatChanged("附件目标与当前聊天不一致")
            if expected_context is not None and snapshot.context_id != expected_context:
                raise ChatChanged("附件回复所属的聊天控件已经重建")
            before = {message.id for message in snapshot.messages}
            self._call("prepare_attachment", str(path), expected, kind, file_state)
            try:
                self._call("submit_attachment", expected)
                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    after = self._call("snapshot")
                    if after.chat_name != expected or after.context_id != snapshot.context_id:
                        raise DeliveryUncertain("附件提交后聊天改变，请核对发送结果")
                    for message in after.messages:
                        if (
                            message.id not in before
                            and message.attr == "self"
                            and message.type == kind
                            and (kind == "image" or message.info.get("filename") == path.name)
                        ):
                            return MediaSendResult(
                                expected,
                                str(path),
                                kind,
                                message.id,
                                "new_image_and_side" if kind == "image" else "filename_and_side",
                            )
                    time.sleep(0.1)
            except DeliveryUncertain:
                raise
            except Exception as exc:
                raise DeliveryUncertain("附件提交阶段异常，请核对结果，不要自动重发") from exc
            raise DeliveryUncertain("没有读到新发出的附件消息，请核对结果，不要自动重发")

    def DownloadAttachment(self, message, directory, *, timeout=30.0):
        """Save a visible file/image using Weixin's Save As menu; never execute it."""
        if timeout <= 0:
            raise ValueError("保存超时必须大于零")
        with self._lock:
            return self._call("download_attachment", message, str(directory), timeout)

    def TranscribeVoice(self, message, *, timeout=30.0):
        """Request and read Weixin's own voice transcription, without playing audio."""
        if message.type != "voice" or timeout <= 0:
            raise ValueError("需要语音消息及有效的等待期限")
        with self._lock:
            return self._call("transcribe_voice", message, timeout)

    def watch(self, who, *, interval=1.0, stop=None, message_types=("text",)):
        if interval < 0.1:
            raise ValueError("轮询间隔至少 0.1 秒")
        stopper = stop or threading.Event()
        while not stopper.is_set():
            for message in self.GetNextNewMessage(who):
                if message.attr == "friend" and message.type in message_types:
                    yield message
            stopper.wait(interval)

    def close(self):
        with self._lock:
            if not self._closed:
                try:
                    self._call("close")
                finally:
                    self._closed = True
                    self._pool.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
