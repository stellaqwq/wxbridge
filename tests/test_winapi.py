import sys
from types import SimpleNamespace

from wxbridge.winapi import foreground_root


def test_transient_null_foreground_is_a_paused_state(monkeypatch):
    def forbidden(*_):
        raise AssertionError("A null foreground handle must not be queried")

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(GetForegroundWindow=lambda: 0, IsWindow=forbidden, GetAncestor=forbidden),
    )
    assert foreground_root() is None


def test_explicit_activation_uses_only_alt_when_direct_call_is_refused(monkeypatch):
    import wxbridge.winapi as module

    current, calls, keys = [22], [], []

    def activate(handle):
        calls.append(handle)
        if len(calls) == 1:
            raise OSError("foreground lock")
        current[0] = handle

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            ShowWindow=lambda *_: None,
            GetForegroundWindow=lambda: current[0],
            IsWindow=lambda _: True,
            GetAncestor=lambda handle, _: handle,
            SetForegroundWindow=activate,
        ),
    )
    monkeypatch.setattr(module, "_press_key", lambda key: keys.append(key))
    module.show_window(11)
    assert calls == [11, 11] and keys == [0x12]


def test_already_active_window_does_not_receive_activation_keys(monkeypatch):
    import wxbridge.winapi as module

    def forbidden(*_):
        raise AssertionError("An active window must not receive activation keys")

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            ShowWindow=lambda *_: None,
            GetForegroundWindow=lambda: 11,
            IsWindow=lambda _: True,
            GetAncestor=lambda handle, _: handle,
            SetForegroundWindow=forbidden,
        ),
    )
    monkeypatch.setattr(module, "_press_key", forbidden)
    module.show_window(11)


def test_qt_menu_ownership_is_resolved_without_rootowner_assumptions(monkeypatch):
    from wxbridge.winapi import owned_by

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(GetWindow=lambda hwnd, _: {33: 22, 22: 11, 44: 44}.get(hwnd, 0)),
    )
    assert owned_by(33, 11)
    assert not owned_by(44, 11)
