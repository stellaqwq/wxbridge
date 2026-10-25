"""In-memory UI backend for protocol testing. Never imports Windows automation."""

import uuid
from dataclasses import replace
from pathlib import Path

from wxbridge.errors import ChatChanged
from wxbridge.models import Message, Snapshot


class MockBackend:
    def __init__(self, chat_name, chat_type="friend"):
        self.state = Snapshot(
            chat_name, chat_type, (Message("history", "启动前的历史", attr="friend"),), "mock"
        )
        self.sent = []
        self.sent_images = []
        self.prepared_attachment = None

    def snapshot(self):
        return self.state

    def compose(self, text, who):
        if who != self.state.chat_name:
            raise ChatChanged("模拟会话已经切换")

    def submit(self, who, text):
        self.compose(text, who)
        self.sent.append(text)
        self.inject(text, "self")

    def inject(self, text, attr="friend", kind="text", sender=None):
        name = (
            sender
            if sender is not None
            else ("模拟群成员" if self.state.chat_type == "group" else self.state.chat_name)
        )
        message = Message(uuid.uuid4().hex, text, name, attr, kind)
        self.state = replace(self.state, messages=(*self.state.messages[-100:], message))

    def prepare_attachment(self, path, who, kind, file_state):
        self.compose("", who)
        if kind != "image":
            raise ValueError("模拟 OneBot 当前只开放图片附件发送")
        self.prepared_attachment = path, who

    def submit_attachment(self, who):
        path, expected = self.prepared_attachment
        self.compose("", expected)
        if who != expected:
            raise ChatChanged("模拟附件目标改变")
        self.sent_images.append(Path(path).read_bytes())
        self.inject("[图片]", "self", "image")
        self.prepared_attachment = None

    def close(self):
        pass
