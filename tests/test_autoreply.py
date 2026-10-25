import sqlite3
import threading
import time

import pytest

from wxbridge import DeliveryUncertain, Message, WeChatError
from wxbridge.autoreply import AutoReply
from wxbridge.storage import exclusive_ledger


class Client:
    def __init__(self, messages, stop, uncertain=False):
        self.messages = messages
        self.stop = stop
        self.uncertain = uncertain
        self.polls = 0
        self.sent = []

    def ChatWith(self, who):
        assert who == "测试好友"

    def GetNextNewMessage(self, who):
        self.polls += 1
        if self.polls <= 2:
            return self.messages  # Intentionally repeat the same UI IDs.
        self.stop.set()
        return []

    def SendMsg(self, text, who, expected_context):
        self.sent.append((who, text, expected_context))
        if self.uncertain:
            raise DeliveryUncertain("unknown")


def test_only_new_incoming_texts_are_processed_once(tmp_path):
    stop = threading.Event()
    messages = [
        Message("self", "自发", attr="self"),
        Message("system", "系统", attr="system"),
        Message("image", "图片", attr="friend", type="image"),
        Message("voice", "语音 3秒", attr="friend", type="voice"),
        Message("incoming", "你好", attr="friend", info={"context_id": "original"}),
    ]
    client = Client(messages, stop)
    worker = AutoReply(
        client,
        "测试好友",
        lambda m: "AI: " + m.content,
        database=tmp_path / "replies.db",
        interval=0.1,
        min_send_interval=0,
    )
    worker.run(stop)
    assert client.sent == [("测试好友", "AI: 你好", "original")]
    with sqlite3.connect(worker.database) as db:
        assert db.execute("SELECT state FROM replies").fetchall() == [("confirmed_in_ui",)]


def test_uncertain_delivery_is_journaled_and_never_retried(tmp_path):
    stop = threading.Event()
    client = Client([Message("incoming", "你好", attr="friend")], stop, uncertain=True)
    worker = AutoReply(
        client,
        "测试好友",
        lambda _: "回答",
        database=tmp_path / "reply.db",
        interval=0.1,
        min_send_interval=0,
    )
    worker.run(stop)
    assert len(client.sent) == 1
    with sqlite3.connect(worker.database) as db:
        assert db.execute("SELECT state FROM replies").fetchone()[0] == "unknown"


def test_model_failure_does_not_persist_credentials(tmp_path):
    stop = threading.Event()
    client = Client([Message("incoming", "你好", attr="friend")], stop)

    def model(_):
        raise RuntimeError("Authorization: Bearer PRIVATE-KEY")

    worker = AutoReply(client, "测试好友", model, database=tmp_path / "reply.db", interval=0.1)
    worker.run(stop)
    assert client.sent == []
    assert b"PRIVATE-KEY" not in worker.database.read_bytes()
    with sqlite3.connect(worker.database) as db:
        assert db.execute("SELECT state FROM replies").fetchone()[0] == "failed"


def test_expired_model_answer_is_not_sent(tmp_path):
    stop = threading.Event()
    client = Client([Message("incoming", "你好", attr="friend")], stop)

    def model(_):
        time.sleep(0.03)
        return "迟到的回答"

    worker = AutoReply(
        client, "测试好友", model, database=tmp_path / "reply.db", interval=0.1, max_age=0.01
    )
    worker.run(stop)
    assert client.sent == []


def test_ledger_excludes_other_workers(tmp_path):
    path = tmp_path / "reply.db"
    with exclusive_ledger(path), pytest.raises(WeChatError):
        with exclusive_ledger(path):
            pytest.fail("A second worker acquired the same ledger")
    with exclusive_ledger(path):
        pass


def test_test_prefix_is_required_before_model_or_send(tmp_path):
    stop = threading.Event()
    client = Client(
        [
            Message("ordinary", "普通聊天", attr="friend"),
            Message("test", "/ai 测试", attr="friend"),
        ],
        stop,
    )
    model_calls = []

    def model(message):
        model_calls.append(message.id)
        return "测试回答"

    worker = AutoReply(
        client,
        "测试好友",
        model,
        database=tmp_path / "reply.db",
        interval=0.1,
        predicate=lambda message: message.content.startswith("/ai "),
    )
    worker.run(stop)
    assert model_calls == ["test"]
    assert len(client.sent) == 1


def test_restart_does_not_retry_unfinished_reply(tmp_path):
    database = tmp_path / "reply.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE replies (id TEXT PRIMARY KEY, chat TEXT, state TEXT, error TEXT)")
        db.execute("INSERT INTO replies VALUES ('unfinished','测试好友','processing',NULL)")
    stop = threading.Event()
    client = Client([], stop)
    worker = AutoReply(client, "测试好友", lambda _: "不应调用", database=database, interval=0.1)
    worker.run(stop)
    assert client.sent == []
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state FROM replies").fetchone()[0] == "unknown"


def test_voice_opt_in_transcribes_and_replies_only_once(tmp_path):
    stop = threading.Event()
    voice = Message("voice", "语音 3秒", attr="friend", type="voice", info={"context_id": "ctx"})
    client = Client([voice], stop)
    calls = []

    def reply(message):
        assert message.type == "voice"
        calls.append(message.id)
        return "AI: 测试转写"

    worker = AutoReply(
        client,
        "测试好友",
        reply,
        database=tmp_path / "voice.db",
        interval=0.1,
        min_send_interval=0,
        message_types=("voice",),
    )
    worker.run(stop)
    assert calls == ["voice"]
    assert client.sent == [("测试好友", "AI: 测试转写", "ctx")]


def test_voice_transcription_failure_never_sends_fallback_text(tmp_path):
    stop = threading.Event()
    client = Client([Message("voice", "语音", attr="friend", type="voice")], stop)

    def reply(_):
        raise WeChatError("微信没有暴露完整转文字结果")

    worker = AutoReply(
        client,
        "测试好友",
        reply,
        database=tmp_path / "voice.db",
        interval=0.1,
        message_types=("voice",),
    )
    worker.run(stop)
    assert client.sent == []
