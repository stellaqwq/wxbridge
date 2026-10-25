import time

from .compatibility import VERIFIED_DLLS
from .errors import UnsupportedVersion, WeChatError
from .pe import locate_gate
from .winapi import ProcessMemory, weixin_module


class AccessibilityLease:
    """Restore the original byte on failure or close; never try arbitrary addresses."""

    def __init__(self, pid, hwnd, uia):
        self.pid, self.hwnd, self.uia = pid, hwnd, uia
        self.memory = None
        self.original = None
        self.address = None
        self.proof = None

    def enable(self):
        if self.uia.ready(self.hwnd):
            return {"already_enabled": True}
        base, module_size, path = weixin_module(self.pid)
        proof = locate_gate(path)
        if proof.sha256 not in VERIFIED_DLLS:
            raise UnsupportedVersion("该微信 DLL 尚未通过实机验证，禁止自动修改进程状态")
        if module_size != proof.image_size:
            raise UnsupportedVersion("磁盘 DLL 与进程模块大小不一致")
        self.proof = proof
        self.memory = ProcessMemory(self.pid, writable=True)
        try:
            for rva, expected in [
                (proof.getter_rva, proof.getter_bytes),
                (proof.setter_rva, proof.setter_bytes),
            ]:
                if self.memory.read(base + rva, len(expected)) != expected:
                    raise UnsupportedVersion("进程与文件中的无障碍指令不一致")
            self.address = base + proof.gate_rva
            original = self.memory.read(self.address, 1)[0]
            if original not in (0, 1):
                raise UnsupportedVersion("候选地址当前不是布尔值，已停止")
            self.original = original
            self.memory.write_byte(self.address, 1)
            import win32gui

            handles = [self.hwnd]
            win32gui.EnumChildWindows(self.hwnd, lambda h, _: handles.append(h), None)
            for handle in handles:
                for object_id in (-4, -25):
                    try:
                        win32gui.SendMessageTimeout(handle, 0x003D, 0, object_id, 0x2, 500)
                    except Exception:
                        pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.uia.ready(self.hwnd):
                    return {
                        "already_enabled": False,
                        "pid": self.pid,
                        "dll": str(path),
                        "sha256": proof.sha256,
                        "gate_rva": hex(proof.gate_rva),
                    }
                time.sleep(0.15)
            raise WeChatError("开启后控件树仍未出现，已恢复原值")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.memory:
            try:
                if self.original is not None:
                    if self.proof is not None:
                        base = self.address - self.proof.gate_rva
                        for rva, expected in (
                            (self.proof.getter_rva, self.proof.getter_bytes),
                            (self.proof.setter_rva, self.proof.setter_bytes),
                        ):
                            if self.memory.read(base + rva, len(expected)) != expected:
                                raise UnsupportedVersion("模块指令已变化，已跳过状态恢复写入")
                    self.memory.write_byte(self.address, self.original)
            finally:
                self.memory.close()
                self.memory = None
