from types import SimpleNamespace

import pytest

from wxbridge import UnsupportedVersion
from wxbridge.accessibility import AccessibilityLease


def test_mismatched_live_code_does_not_write(monkeypatch):
    import wxbridge.accessibility as module

    writes = []
    proof = SimpleNamespace(
        sha256="test-binary",
        image_size=100,
        gate_rva=50,
        getter_rva=10,
        getter_bytes=b"GETTER",
        setter_rva=20,
        setter_bytes=b"SETTER",
    )

    class Memory:
        def __init__(self, *_args, **_kwargs):
            pass

        def read(self, *_args):
            return b"WRONG!"

        def write_byte(self, *args):
            writes.append(args)

        def close(self):
            pass

    monkeypatch.setattr(module, "weixin_module", lambda _: (1000, 100, "fake.dll"))
    monkeypatch.setattr(module, "locate_gate", lambda _: proof)
    monkeypatch.setattr(module, "VERIFIED_DLLS", {"test-binary": "test"})
    monkeypatch.setattr(module, "ProcessMemory", Memory)
    lease = AccessibilityLease(1, 2, SimpleNamespace(ready=lambda _: False))
    with pytest.raises(UnsupportedVersion):
        lease.enable()
    assert writes == []


def test_close_restores_original_byte():
    writes = []
    closed = []
    lease = AccessibilityLease(1, 2, None)
    lease.memory = SimpleNamespace(
        write_byte=lambda *args: writes.append(args), close=lambda: closed.append(True)
    )
    lease.address, lease.original = 123, 0
    lease.close()
    lease.close()
    assert writes == [(123, 0)] and closed == [True]


def test_unknown_binary_never_opens_writable_process(monkeypatch):
    import wxbridge.accessibility as module

    monkeypatch.setattr(module, "weixin_module", lambda _: (1000, 100, "fake.dll"))
    monkeypatch.setattr(module, "locate_gate", lambda _: SimpleNamespace(sha256="unknown"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("An unverified binary requested writable process access")

    monkeypatch.setattr(module, "ProcessMemory", forbidden)
    lease = AccessibilityLease(1, 2, SimpleNamespace(ready=lambda _: False))
    with pytest.raises(UnsupportedVersion):
        lease.enable()


def test_changed_module_is_not_written_during_cleanup():
    writes, closed = [], []
    lease = AccessibilityLease(1, 2, None)
    lease.proof = SimpleNamespace(
        gate_rva=50, getter_rva=10, getter_bytes=b"GETTER", setter_rva=20, setter_bytes=b"SETTER"
    )
    lease.address, lease.original = 1050, 0
    lease.memory = SimpleNamespace(
        read=lambda *_: b"CHANGED",
        write_byte=lambda *x: writes.append(x),
        close=lambda: closed.append(True),
    )
    with pytest.raises(UnsupportedVersion):
        lease.close()
    assert writes == [] and closed == [True]
