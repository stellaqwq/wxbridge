import sys
from types import SimpleNamespace

import pytest

from wxbridge.errors import WeChatError
from wxbridge.windows import WindowsBackend


def test_group_header_count_and_type_pin_do_not_change_the_actual_title():
    from wxbridge.errors import ChatChanged

    def node(cls, aid="", name=""):
        return SimpleNamespace(CurrentClassName=cls, CurrentAutomationId=aid, CurrentName=name)

    header = node("mmui::ChatTitleBarChatRoomView")
    composer = node("mmui::ChatInputField", name="群名(1)")
    labels = [
        node("mmui::XTextView", "title.current_chat_name_label", "群名(1)"),
        node("mmui::XTextView", "title.current_chat_count_label", "(7)"),
    ]
    backend = object.__new__(WindowsBackend)
    backend.hwnd = 11
    backend._visible = lambda *_, **__: None
    backend._find = lambda **_: composer
    backend.uia = SimpleNamespace(nodes=lambda _: [header], children=lambda *args: labels)
    backend.expected_chat_type = "group"
    assert backend._chat()[:2] == ("群名(1)", "group")
    assert backend._chat_metadata == {"member_count": 7}
    backend.expected_chat_type = "friend"
    with pytest.raises(ChatChanged):
        backend._chat()
    backend.expected_chat_type = "group"
    backend.uia.nodes = lambda _: [header, node("mmui::ChatTitleBarChatSingleView")]
    with pytest.raises(WeChatError, match="唯一"):
        backend._chat()


@pytest.fixture
def passive_backend(monkeypatch):
    import wxbridge.winapi as api

    backend = object.__new__(WindowsBackend)
    backend.hwnd, backend.pid = 11, 22
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            IsWindowVisible=lambda _: True,
            IsIconic=lambda _: False,
        ),
    )
    monkeypatch.setattr(api, "find_window", lambda _: (11, 22))
    monkeypatch.setattr(api, "foreground_root", lambda: 33)
    return backend


def test_passive_visibility_allows_browser_focus_but_write_still_requires_wechat(passive_backend):
    passive_backend._visible(require_foreground=False)
    with pytest.raises(WeChatError, match="前台"):
        passive_backend._visible()
    with pytest.raises(WeChatError, match="前台"):
        passive_backend.compose("不得写入草稿", "测试好友")


@pytest.mark.parametrize("hidden,minimized", [(True, False), (False, True)])
def test_passive_visibility_rejects_hidden_or_minimized_window(
    passive_backend, monkeypatch, hidden, minimized
):
    gui = sys.modules["win32gui"]
    monkeypatch.setattr(gui, "IsWindowVisible", lambda _: not hidden)
    monkeypatch.setattr(gui, "IsIconic", lambda _: minimized)
    with pytest.raises(WeChatError, match="展开"):
        passive_backend._visible(require_foreground=False)


def test_passive_read_rejects_unknown_or_locked_desktop(passive_backend, monkeypatch):
    import wxbridge.winapi as api

    monkeypatch.setattr(api, "foreground_root", lambda: None)
    with pytest.raises(WeChatError, match="桌面"):
        passive_backend._visible(require_foreground=False)


def test_occlusion_checks_top_and_bottom_as_well_as_middle(monkeypatch):
    backend = object.__new__(WindowsBackend)
    backend.hwnd = 11
    points = []

    def window_at(point):
        points.append(point)
        return 33 if point == (16, 8) else 11

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            WindowFromPoint=window_at,
            GetAncestor=lambda hwnd, _: hwnd,
        ),
    )
    assert not backend._unobscured((0, 0, 100, 60))
    monkeypatch.setattr(sys.modules["win32gui"], "WindowFromPoint", lambda _: 11)
    assert backend._unobscured((0, 0, 100, 60))


def test_offscreen_row_without_a_window_is_unreadable_instead_of_crashing(monkeypatch):
    backend = object.__new__(WindowsBackend)
    backend.hwnd = 11

    def invalid_ancestor(*_):
        raise AssertionError("A zero handle must not be passed to GetAncestor")

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            WindowFromPoint=lambda _: 0,
            GetAncestor=invalid_ancestor,
        ),
    )
    assert not backend._unobscured((0, -200, 100, -100))


@pytest.mark.parametrize("occluded", [False, True])
def test_snapshot_is_passive_and_never_captures_an_obscured_window(monkeypatch, occluded):
    from wxbridge import pixels

    backend = object.__new__(WindowsBackend)
    calls = []

    def chat(*, require_foreground):
        calls.append(require_foreground)
        return "测试好友", "friend", SimpleNamespace(GetRuntimeId=lambda: (1, 2))

    backend._chat = chat
    backend._find = lambda **_: SimpleNamespace(
        CurrentBoundingRectangle=SimpleNamespace(
            left=0,
            top=0,
            right=100,
            bottom=100,
        )
    )
    backend._unobscured = lambda _: not occluded
    backend._scale = lambda: 1
    backend.uia = SimpleNamespace(children=lambda _: [])

    def capture(_):
        assert not occluded, "An obscured window must never be classified using browser pixels"
        return object()

    monkeypatch.setattr(pixels, "capture", capture)
    if occluded:
        with pytest.raises(WeChatError, match="遮挡"):
            backend.snapshot()
    else:
        assert backend.snapshot().chat_name == "测试好友"
    assert calls and all(value is False for value in calls)
