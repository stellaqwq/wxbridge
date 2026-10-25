from types import SimpleNamespace

from wxbridge.onebot.monitor import MessageMonitor


def test_monitor_cursor_pages_are_repeatable_without_consuming_messages():
    monitor = MessageMonitor()
    for index in range(4):
        monitor.record({"text": str(index)})
    first = monitor.page(limit=2)
    assert [item["text"] for item in first["messages"]] == ["0", "1"]
    assert monitor.page(limit=2) == first
    second = monitor.page(after=first["cursor"], session=first["session"])
    assert [item["text"] for item in second["messages"]] == ["2", "3"]
    assert not second["reset"]
    assert monitor.page(after=second["cursor"], session=second["session"])["messages"] == []


def test_monitor_has_bounded_history_and_reports_skipped_messages():
    monitor = MessageMonitor(capacity=3)
    monitor.record({"text": "old"})
    cursor = monitor.page()
    for index in range(5):
        monitor.record({"text": str(index)})
    page = monitor.page(after=cursor["cursor"], session=cursor["session"])
    assert len(monitor.messages) == 3
    assert [item["text"] for item in page["messages"]] == ["2", "3", "4"]
    assert page["dropped"] == 2
    reset = monitor.page(after=999, session=cursor["session"])
    assert reset["reset"] is True and len(reset["messages"]) == 3


def test_contact_scope_change_resets_history_but_reconnect_does_not():
    monitor = MessageMonitor()
    config = SimpleNamespace(self_id=1, peer_id=2, chat_name="第一个联系人")
    monitor.set_scope(config)
    monitor.record({"text": "属于第一个联系人"})
    first = monitor.page()
    monitor.set_scope(config)
    assert monitor.page() == first
    config.chat_name = "第二个联系人"
    monitor.set_scope(config)
    page = monitor.page(after=first["cursor"], session=first["session"])
    assert page["reset"] is True and page["messages"] == [] and page["cursor"] == 0
