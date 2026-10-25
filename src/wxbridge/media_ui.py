"""Verified foreground UI operations for attachments in a single private chat."""

import hashlib
import time
from pathlib import Path
from uuid import uuid4

from .errors import ChatChanged, UnsupportedVersion, WeChatError
from .media import safe_filename
from .pixels import capture
from .winapi import click_point, foreground_root, owned_by, press_enter, press_shortcut
from .windows import rectangle


class MediaUI:
    def __init__(self, backend):
        self.b = backend

    def _windows(self, cls):
        import win32gui
        import win32process

        handles = []

        def visit(hwnd, _):
            if (
                win32gui.IsWindowVisible(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == self.b.pid
                and hwnd != self.b.hwnd
                and owned_by(hwnd, self.b.hwnd)
            ):
                native_cls = win32gui.GetClassName(hwnd)
                if native_cls == cls or (
                    cls.startswith("mmui::") and self.b.uia.root(hwnd).CurrentClassName == cls
                ):
                    handles.append(hwnd)

        win32gui.EnumWindows(visit, None)
        return handles

    def _wait_window(self, cls, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            windows = self._windows(cls)
            if len(windows) == 1:
                return windows[0]
            if len(windows) > 1:
                raise WeChatError("附件窗口不唯一，已停止")
            time.sleep(0.05)
        raise WeChatError("微信未显示预期的附件窗口")

    def _wait_main(self, timeout=1.5):
        deadline = time.monotonic() + timeout
        while foreground_root() != self.b.hwnd and time.monotonic() < deadline:
            time.sleep(0.02)
        self.b._visible()

    def _click(self, node, hwnd=None, **options):
        hwnd = hwnd or self.b.hwnd
        self.b._visible(None if hwnd == self.b.hwnd else hwnd)
        if not node.CurrentIsEnabled:
            raise WeChatError("附件操作控件不可用")
        left, top, right, bottom = rectangle(node)
        click_point(
            hwnd,
            (left + right) / 2,
            (top + bottom) / 2,
            foreground_hwnd=foreground_root(),
            **options,
        )

    def _chat(self, expected, modal=None):
        name, _, composer = self.b._chat(modal)
        if name != expected:
            raise ChatChanged("附件操作期间目标会话改变")
        return composer

    def _picker(self, hwnd, path, expected):
        composer = self._chat(expected, hwnd)
        root = self.b.uia.root(hwnd)
        edit = self._file_edit(root)
        pattern = self.b._pattern(edit, "Value")
        pattern.SetValue(str(path))
        if pattern.CurrentValue != str(path):
            raise WeChatError("附件路径回读不一致，已停止")
        return composer, self.b._find(aid="1", cls="Button", root=root)

    def _file_edit(self, root):
        edits = [
            node
            for node in self.b.uia.children(root, True)
            if node.CurrentClassName == "Edit" and node.CurrentAutomationId in {"1148", "1001"}
        ]
        if len(edits) != 1:
            raise UnsupportedVersion("无法唯一确认文件名输入框")
        return edits[0]

    def _cancel_picker(self, hwnd):
        if hwnd in self._windows("#32770"):
            root = self.b.uia.root(hwnd)
            cancel = self.b._find(aid="2", cls="Button", root=root)
            self._click(cancel, hwnd)

    @staticmethod
    def _file_state(path):
        stat = Path(path).stat()
        return stat.st_size, stat.st_mtime_ns

    def _preview(self, composer):
        left, top, right, bottom = rectangle(composer)
        # Home puts the blinking caret at the left edge; exclude that gutter.
        frame = capture((left + 8, top + 4, right - 8, bottom - 4))
        return hashlib.sha256(frame.data).digest()

    def prepare(self, path, expected, kind, file_state):
        composer = self._chat(expected)
        if self.b._pattern(composer, "Value").CurrentValue:
            raise WeChatError("输入框中存在草稿，请先处理草稿")
        if self._windows("#32770") or self._windows("mmui::XMenu"):
            raise WeChatError("已有附件窗口或菜单，请先关闭")
        button = self.b._find(cls="mmui::XButton", name="发送文件")
        self._click(button)
        dialog = self._wait_window("#32770")
        try:
            _, opened = self._picker(dialog, path, expected)
            if self._file_state(path) != file_state:
                raise WeChatError("选择期间附件文件发生变化")
            self._click(opened, dialog)
            deadline = time.monotonic() + 4
            while self._windows("#32770") and time.monotonic() < deadline:
                time.sleep(0.05)
            self._wait_main()
            composer = self._chat(expected)
            if self.b._pattern(composer, "Value").CurrentValue != "\ufffc":
                raise WeChatError("没有读到唯一附件草稿，未提交；请检查微信")
            composer.SetFocus()
            press_shortcut(0x24)  # Home: leave the attachment unchanged.
            time.sleep(0.1)
            self.b._prepared_attachment = (
                expected,
                tuple(composer.GetRuntimeId()),
                str(path),
                kind,
                file_state,
                self._preview(composer),
            )
        except BaseException:
            # Cancel only the native picker created by this operation, never a draft.
            self._cancel_picker(dialog)
            raise

    def submit(self, expected):
        prepared = getattr(self.b, "_prepared_attachment", None)
        if not prepared or prepared[0] != expected:
            raise WeChatError("不存在已验证的附件草稿")
        composer = self._chat(expected)
        if tuple(composer.GetRuntimeId()) != prepared[1]:
            raise ChatChanged("附件输入框已经重建")
        if self._file_state(prepared[2]) != prepared[4]:
            raise WeChatError("附件文件在提交前发生变化")
        if self.b._pattern(composer, "Value").CurrentValue != "\ufffc":
            raise WeChatError("附件草稿被修改，未提交")
        if self._preview(composer) != prepared[5]:
            raise WeChatError("附件预览被修改，未提交；请检查草稿")
        composer.SetFocus()
        if tuple(self.b.uia.auto.GetFocusedElement().GetRuntimeId()) != prepared[1]:
            raise WeChatError("附件输入框未获得焦点，未提交")
        self._chat(expected)
        self.b._prepared_attachment = None
        press_enter()

    def _message(self, message):
        snapshot = self.b.snapshot()
        if (
            message.info.get("chat_name") != snapshot.chat_name
            or message.info.get("context_id") != snapshot.context_id
        ):
            raise ChatChanged("附件不属于当前聊天控件")
        matches = [m for m in snapshot.messages if m.id == message.id]
        if len(matches) != 1 or matches[0].type != message.type:
            raise WeChatError("附件消息已经离开可见列表或内容改变")
        if matches[0].attr not in {"self", "friend"}:
            raise WeChatError("无法确认附件所在一侧，已停止")
        message_list = self.b._find(aid="chat_message_list")
        for node in self.b.uia.children(message_list):
            if tuple(node.GetRuntimeId()) == tuple(matches[0].info.get("runtime_id", ())):
                return snapshot.chat_name, node, matches[0]
        raise WeChatError("附件控件已经改变")

    def _menu(self, message, labels):
        expected, node, current = self._message(message)
        if self._windows("mmui::XMenu"):
            raise WeChatError("已有菜单打开，请先关闭")
        left, top, right, bottom = rectangle(node)
        scale = self.b._scale()
        x = right - 140 * scale if current.attr == "self" else left + 100 * scale
        click_point(self.b.hwnd, x, top + min(24 * scale, (bottom - top) / 2), right=True)
        menu = self._wait_window("mmui::XMenu")
        try:
            self._chat(expected, menu)
            items = [
                item
                for item in self.b.uia.children(self.b.uia.root(menu), True)
                if item.CurrentClassName == "mmui::XMenuView" and item.CurrentName in labels
            ]
            if len(items) != 1:
                raise UnsupportedVersion("当前消息菜单没有唯一可用的目标操作")
            self._click(items[0], menu)
        except BaseException:
            if menu in self._windows("mmui::XMenu"):
                self.b._visible(menu)
                press_shortcut(0x1B)
            raise
        return expected

    def download(self, message, directory, timeout):
        if message.type not in {"file", "image"}:
            raise ValueError("仅支持保存文件和图片消息")
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        expected = self._menu(message, {"另存为...", "另存为…"})
        dialog = self._wait_window("#32770")
        try:
            return self._save_as(dialog, message, directory, expected, timeout)
        finally:
            self._cancel_picker(dialog)

    def _save_as(self, dialog, message, directory, expected, timeout):
        root = self.b.uia.root(dialog)
        edit = self._file_edit(root)
        original = self.b._pattern(edit, "Value").CurrentValue
        name = message.info.get("filename") or original
        destination = directory / (uuid4().hex[:12] + "-" + safe_filename(name))
        _, save = self._picker(dialog, destination, expected)
        if destination.exists():
            raise FileExistsError(destination)
        self._click(save, dialog)
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            if self._windows("#32770"):
                # Never approve replacement prompts or choose another filename.
                time.sleep(0.1)
                continue
            self._wait_main()
            self._chat(expected)
            if destination.is_file():
                state = self._file_state(destination)
                if state == previous:
                    return destination
                previous = state
            time.sleep(0.2)
        raise WeChatError("附件保存未在期限内完成，请检查微信及目标目录")

    def transcribe(self, message, timeout):
        if message.type != "voice":
            raise ValueError("需要语音消息")
        _, _, current = self._message(message)
        existing = current.info.get("transcript")
        if existing:
            return existing
        expected = self._menu(message, {"转文字", "语音转文字"})
        self._wait_main()
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            name, node, current = self._message(message)
            if name != expected:
                raise ChatChanged("语音转写期间聊天改变")
            text = current.info.get("transcript")
            if not text:
                # Some versions expose a separately named transcription label.
                labels = [
                    item.CurrentName
                    for item in self.b.uia.children(node, True)
                    if any(
                        token in item.CurrentAutomationId.lower()
                        for token in (
                            "transcription",
                            "transcribe",
                            "voice_to_text",
                            "translate_text",
                        )
                    )
                    and item.CurrentName.strip()
                ]
                if len(labels) == 1:
                    text = labels[0].strip()
            if text and text == previous:
                return text
            previous = text
            time.sleep(0.3)
        raise WeChatError("微信没有暴露完整转文字结果，未交给 AI、未发送回复")
