import ctypes
import hashlib
import re
import sys
import time
from uuid import uuid4

from .errors import ChatChanged, UnsupportedVersion, WeChatError
from .models import Message, Snapshot


def rectangle(element):
    r = element.CurrentBoundingRectangle
    return r.left, r.top, r.right, r.bottom


def text_value(value):
    return value.replace("\r\n", "\n").replace("\u2029", "\n")


class WindowsBackend:
    def __init__(
        self, *, hwnd=None, enable_accessibility=False, foreground=False, expected_chat_type=None
    ):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise UnsupportedVersion("需要 Windows 和 64 位 Python")
        import win32event

        from .accessibility import AccessibilityLease
        from .uia import UIA
        from .winapi import find_window

        self.uia = None
        self.lease = None
        self.mutex = None
        self._mutex_owned = False
        self.run_id = uuid4().hex
        self.hwnd, self.pid = find_window(hwnd)
        self.previous_dpi = None
        self.expected_chat_type = expected_chat_type
        try:
            setter = ctypes.windll.user32.SetThreadDpiAwarenessContext
            setter.argtypes, setter.restype = [ctypes.c_void_p], ctypes.c_void_p
            self.previous_dpi = setter(ctypes.c_void_p(-4))
            self.mutex = win32event.CreateMutex(None, False, f"Local\\wxbridge-{self.pid}")
            if win32event.WaitForSingleObject(self.mutex, 0) not in (0, 0x80):
                raise WeChatError("已有 wxbridge 实例控制这个微信进程")
            self._mutex_owned = True
            self.uia = UIA()
            self.lease = AccessibilityLease(self.pid, self.hwnd, self.uia)
            if enable_accessibility:
                self.lease.enable()
            if not self.uia.ready(self.hwnd):
                raise UnsupportedVersion("微信未暴露聊天控件；可显式启用 enable_accessibility")
            if foreground:
                from .winapi import show_window

                show_window(self.hwnd)
            self.snapshot()
        except BaseException:
            self.close()
            raise

    def _visible(self, modal=None, *, require_foreground=True):
        import win32gui

        from .winapi import find_window, foreground_root, owned_by

        if find_window(self.hwnd)[1] != self.pid:
            raise WeChatError("微信窗口句柄已被其他进程复用")
        if not win32gui.IsWindowVisible(self.hwnd) or win32gui.IsIconic(self.hwnd):
            raise WeChatError("请展开已登录的微信窗口")
        allowed = {self.hwnd}
        if modal is not None:
            import win32process

            if (
                not win32gui.IsWindow(modal)
                or win32process.GetWindowThreadProcessId(modal)[1] != self.pid
                or not owned_by(modal, self.hwnd)
            ):
                raise WeChatError("附件窗口不属于当前微信，已停止操作")
            allowed.add(modal)
        foreground = foreground_root()
        if foreground is None:
            raise WeChatError("无法确认当前桌面状态，请解锁桌面后重试")
        if require_foreground and foreground not in allowed:
            raise WeChatError("请让微信保持前台，暂停自动化期间的其他桌面操作")

    def _find(self, *, aid=None, cls=None, name=None, root=None):
        nodes = self.uia.children(root, True) if root is not None else self.uia.nodes(self.hwnd)
        matches = [
            n
            for n in nodes
            if (aid is None or n.CurrentAutomationId == aid)
            and (cls is None or n.CurrentClassName == cls)
            and (name is None or n.CurrentName == name)
        ]
        if len(matches) != 1:
            raise WeChatError("目标控件不存在或不唯一，已停止操作")
        return matches[0]

    def _pattern(self, node, name):
        raw = node.GetCurrentPattern(getattr(self.uia.api, f"UIA_{name}PatternId"))
        if not raw:
            raise UnsupportedVersion(f"控件不支持 {name} 接口")
        return raw.QueryInterface(getattr(self.uia.api, f"IUIAutomation{name}Pattern"))

    def _chat(self, modal=None, *, require_foreground=True):
        self._visible(modal, require_foreground=require_foreground)
        composer = self._find(aid="chat_input_field", cls="mmui::ChatInputField")
        # Confirm against an independent title label, not only an input placeholder.
        headers = [
            n
            for n in self.uia.nodes(self.hwnd)
            if n.CurrentClassName
            in {"mmui::ChatTitleBarChatSingleView", "mmui::ChatTitleBarChatRoomView"}
        ]
        if len(headers) != 1:
            raise WeChatError("无法唯一定位私聊或群聊标题")
        header = headers[0]
        chat_type = (
            "group" if header.CurrentClassName == "mmui::ChatTitleBarChatRoomView" else "friend"
        )
        if getattr(self, "expected_chat_type", None) not in {None, chat_type}:
            raise ChatChanged("当前会话类型与配置不一致，已暂停读取和发送")
        labels = [
            n
            for n in self.uia.children(header, True)
            if n.CurrentClassName == "mmui::XTextView"
            and n.CurrentAutomationId.endswith(".current_chat_name_label")
        ]
        if len(labels) != 1 or not labels[0].CurrentName:
            raise WeChatError("无法唯一定位会话标题")
        name = labels[0].CurrentName
        suffixes = ("", "长按鼠标 语音输入", "按住鼠标 语音输入文字")
        if composer.CurrentName not in {name + suffix for suffix in suffixes}:
            raise WeChatError("会话标题与输入框名称不一致，已停止操作")
        self._chat_metadata = {}
        if chat_type == "group":
            counts = [
                n.CurrentName
                for n in self.uia.children(header, True)
                if n.CurrentAutomationId.endswith(".current_chat_count_label")
            ]
            if len(counts) == 1:
                match = re.fullmatch(r"[（(](\d+)[）)]", counts[0])
                if match:
                    self._chat_metadata["member_count"] = int(match[1])
        return name, chat_type, composer

    def snapshot(self):
        from .media import identity_content, message_details
        from .pixels import avatar_fingerprint, capture, classify, classify_media

        name, chat_type, composer = self._chat(require_foreground=False)
        context = ":".join(map(str, composer.GetRuntimeId()))
        message_list = self._find(aid="chat_message_list", cls="mmui::RecyclerListView")
        rect = rectangle(message_list)
        if not self._unobscured(rect):
            raise WeChatError("微信消息区域被遮挡，请将微信与浏览器并排并保持消息区域可见")
        frame = capture(rect)
        scale = self._scale()
        messages = []
        for item in self.uia.children(message_list):
            cls, content = item.CurrentClassName, item.CurrentName
            kind, details = message_details(cls, content)
            runtime_id = ":".join(map(str, item.GetRuntimeId()))
            identity = identity_content(kind, content, details)
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
            mid = f"{self.run_id}:{name}:{runtime_id}:{digest}"
            attr = "other"
            if kind in {"text", "voice", "file", "image"} and self._unobscured(rectangle(item)):
                classifier = classify if kind in {"text", "voice"} else classify_media
                attr = classifier(frame, rectangle(item), scale)
            sender = name if attr == "friend" and chat_type == "friend" else ""
            if chat_type == "group" and attr == "friend":
                # This Weixin version exposes the body but not group nicknames in UIA.
                # Keep a clearly local alias derived only from the visible avatar gutter.
                fingerprint = avatar_fingerprint(frame, rectangle(item), scale)
                details.update(
                    sender_key="avatar:" + fingerprint if fingerprint else "unknown",
                    sender_source="avatar_pixels" if fingerprint else "unavailable",
                )
            messages.append(
                Message(
                    mid,
                    content,
                    sender,
                    attr,
                    kind,
                    {
                        "class": cls,
                        "source": "uia",
                        "context_id": context,
                        "role_source": "light_theme_pixels",
                        "chat_name": name,
                        "runtime_id": tuple(item.GetRuntimeId()),
                        **details,
                    },
                )
            )
        after_name, after_type, after_composer = self._chat(require_foreground=False)
        if not self._unobscured(rect):
            raise WeChatError("读取时微信消息区域被遮挡，本次快照已丢弃")
        if (
            after_name != name
            or after_type != chat_type
            or ":".join(map(str, after_composer.GetRuntimeId())) != context
        ):
            raise ChatChanged("读取过程中会话发生变化，本次快照已丢弃")
        return Snapshot(
            name, chat_type, tuple(messages), context, getattr(self, "_chat_metadata", {}).copy()
        )

    def _scale(self):
        return ctypes.windll.user32.GetDpiForWindow(self.hwnd) / 96

    def prepare_attachment(self, path, expected, kind, file_state):
        from .media_ui import MediaUI

        return MediaUI(self).prepare(path, expected, kind, file_state)

    def submit_attachment(self, expected):
        from .media_ui import MediaUI

        return MediaUI(self).submit(expected)

    def download_attachment(self, message, directory, timeout):
        from .media_ui import MediaUI

        return MediaUI(self).download(message, directory, timeout)

    def transcribe_voice(self, message, timeout):
        from .media_ui import MediaUI

        return MediaUI(self).transcribe(message, timeout)

    def _unobscured(self, rect):
        import win32gui

        left, top, right, bottom = rect
        if bottom <= top or right <= left:
            return False
        inset_x, inset_y = min(16, (right - left) / 4), min(8, (bottom - top) / 4)
        for y in (top + inset_y, (top + bottom) / 2, bottom - inset_y):
            for x in (left + inset_x, (left + right) / 2, right - inset_x):
                try:
                    target = win32gui.WindowFromPoint((int(x), int(y)))
                    if not target or win32gui.GetAncestor(target, 2) != self.hwnd:
                        return False
                except Exception:
                    # Offscreen virtual rows and disappearing windows are unreadable,
                    # not a reason to abort every other visible message in the snapshot.
                    return False
        return True

    def open_chat(self, who):
        name, _, _ = self._chat()
        if name == who:
            return
        session = self._find(aid="session_item_" + who, cls="mmui::ChatSessionCell")
        self._pattern(session, "SelectionItem").Select()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if self._chat()[0] == who:
                    return
            except WeChatError:
                pass
            time.sleep(0.1)
        raise WeChatError("会话未打开；请手动打开目标会话后重试")

    def compose(self, msg, expected):
        name, _, composer = self._chat()
        if name != expected:
            raise ChatChanged("填写正文前目标会话改变")
        value = self._pattern(composer, "Value")
        if value.CurrentValue:
            raise WeChatError("输入框中存在草稿，请先处理草稿")
        if value.CurrentIsReadOnly:
            raise WeChatError("输入框不可编辑")
        value.SetValue(msg)
        if text_value(value.CurrentValue) != text_value(msg):
            raise WeChatError("输入内容回读不一致，未提交发送；请检查草稿")
        self._prepared = (name, tuple(composer.GetRuntimeId()), msg)

    def submit(self, expected, msg):
        from .winapi import press_enter

        name, _, composer = self._chat()
        if name != expected:
            raise ChatChanged("提交前目标会话改变")
        if getattr(self, "_prepared", None) != (name, tuple(composer.GetRuntimeId()), msg):
            raise ChatChanged("提交前聊天控件发生变化")
        if text_value(self._pattern(composer, "Value").CurrentValue) != text_value(msg):
            raise WeChatError("提交前正文回读不一致")
        button = self._find(cls="mmui::XOutlineButton", name="发送")
        if not button.CurrentIsEnabled:
            raise WeChatError("发送按钮不可用")
        composer.SetFocus()
        focused = self.uia.auto.GetFocusedElement()
        if tuple(focused.GetRuntimeId()) != tuple(composer.GetRuntimeId()):
            raise WeChatError("无法确认输入框获得焦点，已取消提交")
        current_name, _, current = self._chat()
        if current_name != expected or tuple(current.GetRuntimeId()) != tuple(
            composer.GetRuntimeId()
        ):
            raise ChatChanged("按键提交前会话发生变化")
        if text_value(self._pattern(current, "Value").CurrentValue) != text_value(msg):
            raise WeChatError("按键提交前正文发生变化")
        press_enter()

    def close(self):
        import win32event

        try:
            if self.lease:
                self.lease.close()
        finally:
            try:
                if self.uia:
                    self.uia.close()
                    self.uia = None
            finally:
                try:
                    if self.mutex:
                        if self._mutex_owned:
                            win32event.ReleaseMutex(self.mutex)
                        self.mutex.Close()
                        self.mutex = None
                finally:
                    if self.previous_dpi:
                        ctypes.windll.user32.SetThreadDpiAwarenessContext(
                            ctypes.c_void_p(self.previous_dpi)
                        )
                        self.previous_dpi = None
