from dataclasses import dataclass, field


@dataclass(frozen=True)
class Message:
    id: str
    content: str
    sender: str = ""
    attr: str = "other"
    type: str = "text"
    info: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Snapshot:
    chat_name: str
    chat_type: str
    messages: tuple[Message, ...]
    context_id: str = ""
    info: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class SendResult:
    chat: str
    text: str
    message_id: str
    status: str = "confirmed_in_ui"

    def __bool__(self):
        return self.status == "confirmed_in_ui"


@dataclass(frozen=True)
class MediaSendResult:
    chat: str
    path: str
    kind: str
    message_id: str
    verification: str
    status: str = "confirmed_in_ui"

    def __bool__(self):
        return self.status == "confirmed_in_ui"
