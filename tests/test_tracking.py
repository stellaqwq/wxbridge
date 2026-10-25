from wxbridge import Message
from wxbridge.models import Snapshot
from wxbridge.tracking import MessageTracker


def snapshot(*ids, context="same"):
    return Snapshot("目标", "friend", tuple(Message(i, "相同正文") for i in ids), context)


def test_identical_texts_are_distinct_messages():
    tracker = MessageTracker()
    tracker.update(snapshot("a"))
    assert [m.id for m in tracker.update(snapshot("a", "b", "c"))] == ["b", "c"]


def test_first_message_in_previously_empty_chat_is_delivered():
    tracker = MessageTracker()
    assert tracker.update(snapshot()) == []
    assert [m.id for m in tracker.update(snapshot("first"))] == ["first"]


def test_history_prepended_at_top_is_not_new():
    tracker = MessageTracker()
    tracker.update(snapshot("b", "c"))
    assert tracker.update(snapshot("a", "b", "c")) == []
    assert [m.id for m in tracker.update(snapshot("a", "b", "c", "d"))] == ["d"]


def test_complete_id_rebuild_is_rebaselined():
    tracker = MessageTracker()
    tracker.update(snapshot("a", "b"))
    assert tracker.update(snapshot("x", "y")) == []
    assert [m.id for m in tracker.update(snapshot("x", "y", "z"))] == ["z"]


def test_context_rebuild_is_rebaselined_even_with_overlap():
    tracker = MessageTracker()
    tracker.update(snapshot("a", "b"))
    assert tracker.update(snapshot("a", "b", "c", context="recreated")) == []


def test_empty_gap_does_not_replay_history():
    tracker = MessageTracker()
    tracker.update(snapshot("a"))
    assert tracker.update(snapshot()) == []
    assert tracker.update(snapshot("a", "b")) == []


def test_returning_seen_rows_are_not_duplicated():
    tracker = MessageTracker()
    tracker.update(snapshot("a", "b"))
    assert [m.id for m in tracker.update(snapshot("b", "c"))] == ["c"]
    assert tracker.update(snapshot("a", "b", "c")) == []
