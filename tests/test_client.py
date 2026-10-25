import threading
from dataclasses import replace

import pytest

from wxbridge import ChatChanged, DeliveryUncertain, Message, WeChat, WeChatError
from wxbridge.models import Snapshot


def message(mid, text="你好", attr="friend"):
    return Message(str(mid), text, "测试好友", attr)


class Backend:
    def __init__(self):
        self.state = Snapshot("测试好友", "friend", (message("old"),), "context")
        self.owner = threading.get_ident()
        self.submits = 0
        self.closed = False
        self.echo = True
        self.draft_error = False
        self.submit_error = False

    def check(self):
        assert threading.get_ident() == self.owner

    def snapshot(self):
        self.check()
        return self.state

    def open_chat(self, who):
        self.check()
        self.state = replace(self.state, chat_name=who)

    def compose(self, text, who):
        self.check()
        if self.draft_error:
            raise WeChatError("存在草稿")

    def submit(self, who, text):
        self.check()
        self.submits += 1
        if self.submit_error:
            raise OSError("UI failed")
        if self.echo:
            self.state = replace(
                self.state, messages=(*self.state.messages, message("new-self", text, "self"))
            )

    def close(self):
        self.check()
        self.closed = True


@pytest.fixture
def client():
    with WeChat(backend_factory=Backend, send_timeout=0.01) as wx:
        yield wx, wx._backend


def test_com_is_owned_by_dedicated_thread(client):
    wx, backend = client
    assert backend.owner != threading.get_ident()
    assert wx.ChatInfo()["chat_name"] == "测试好友"
    errors = []

    def read():
        try:
            wx.GetAllMessage()
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    reader.join()
    assert errors == []


def test_old_messages_do_not_become_new(client):
    wx, backend = client
    assert wx.GetNextNewMessage("测试好友") == []
    backend.state = replace(backend.state, messages=(*backend.state.messages, message("fresh")))
    assert [m.id for m in wx.GetNextNewMessage("测试好友")] == ["fresh"]
    assert wx.GetNextNewMessage("测试好友") == []


def test_switch_away_resets_baseline(client):
    wx, backend = client
    wx.GetNextNewMessage("测试好友")
    backend.state = replace(backend.state, chat_name="另一个人")
    with pytest.raises(ChatChanged):
        wx.GetNextNewMessage("测试好友")
    backend.state = replace(
        backend.state, chat_name="测试好友", messages=(message("old"), message("during-absence"))
    )
    assert wx.GetNextNewMessage("测试好友") == []


def test_wrong_recipient_never_submits(client):
    wx, backend = client
    with pytest.raises(ChatChanged):
        wx.SendMsg("回复", who="另一个人")
    assert backend.submits == 0


def test_model_result_cannot_cross_rebuilt_context(client):
    wx, backend = client
    with pytest.raises(ChatChanged):
        wx.SendMsg("回复", who="测试好友", expected_context="old-context")
    assert backend.submits == 0


def test_existing_draft_cancels_send(client):
    wx, backend = client
    backend.draft_error = True
    with pytest.raises(WeChatError, match="草稿"):
        wx.SendMsg("回复")
    assert backend.submits == 0


def test_only_new_exact_self_echo_confirms_send(client):
    wx, backend = client
    result = wx.SendMsg("回复")
    assert result.status == "confirmed_in_ui" and result.message_id == "new-self"
    assert backend.submits == 1


@pytest.mark.parametrize(
    "old_attr,old_text", [("self", "回复"), ("friend", "回复"), ("self", "回复的其他内容")]
)
def test_stale_or_wrong_echo_never_confirms(client, old_attr, old_text):
    wx, backend = client
    backend.echo = False
    backend.state = replace(backend.state, messages=(message("stale", old_text, old_attr),))
    with pytest.raises(DeliveryUncertain):
        wx.SendMsg("回复")
    assert backend.submits == 1


def test_failed_submission_is_not_retried(client):
    wx, backend = client
    backend.submit_error = True
    with pytest.raises(DeliveryUncertain):
        wx.SendMsg("回复")
    assert backend.submits == 1


@pytest.mark.parametrize("text", ["", " ", "x" * 4001, None])
def test_invalid_text_never_submits(client, text):
    wx, backend = client
    with pytest.raises(ValueError):
        wx.SendMsg(text)
    assert backend.submits == 0


def test_close_is_idempotent(client):
    wx, backend = client
    wx.close()
    wx.close()
    assert backend.closed
    with pytest.raises(WeChatError):
        wx.ChatInfo()
