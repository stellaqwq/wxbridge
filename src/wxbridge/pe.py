"""Minimal read-only PE parser and constrained Qt accessibility getter/setter scan."""

import hashlib
import mmap
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import UnsupportedVersion


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    size: int
    raw: int
    raw_size: int
    flags: int


class PE:
    def __init__(self, data):
        self.data = data
        if data[:2] != b"MZ":
            raise ValueError("不是 PE 文件")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0":
            raise ValueError("PE 签名无效")
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        if machine != 0x8664:
            raise UnsupportedVersion("仅支持 Windows x64 微信")
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        if struct.unpack_from("<H", data, opt)[0] != 0x20B:
            raise UnsupportedVersion("需要 PE32+ 客户端")
        self.image_size = struct.unpack_from("<I", data, opt + 56)[0]
        self.sections = []
        for index in range(count):
            at = opt + opt_size + index * 40
            name = data[at : at + 8].rstrip(b"\0").decode("ascii")
            size, rva, raw_size, raw = struct.unpack_from("<IIII", data, at + 8)
            flags = struct.unpack_from("<I", data, at + 36)[0]
            self.sections.append(Section(name, rva, size, raw, raw_size, flags))

    def section(self, name):
        return next(s for s in self.sections if s.name == name)

    def offset(self, rva):
        for sec in self.sections:
            if sec.rva <= rva < sec.rva + sec.raw_size:
                return sec.raw + rva - sec.rva
        raise ValueError("RVA 没有对应文件数据")


@dataclass(frozen=True)
class GateProof:
    sha256: str
    image_size: int
    gate_rva: int
    getter_rva: int
    getter_bytes: bytes
    setter_rva: int
    setter_bytes: bytes


def locate_gate(path: str | Path) -> GateProof:
    """Require a leaf bool getter paired with a same-address bool setter near Qt code.

    No runtime candidate probing and no version-dependent hard-coded addresses.
    Unknown or ambiguous binaries are rejected before any process write.
    """
    with open(path, "rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pe = PE(data)
        text = pe.section(".text")
        writable = pe.section(".data")
        marker_offset = data.find(b"qt.accessibility.core\0")
        if marker_offset < 0:
            raise UnsupportedVersion("未找到 Qt 无障碍标记")
        marker_sec = next(s for s in pe.sections if s.raw <= marker_offset < s.raw + s.raw_size)
        marker_rva = marker_sec.rva + marker_offset - marker_sec.raw
        code = data[text.raw : text.raw + text.raw_size]
        anchors = []
        for match in re.finditer(
            rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....", code, re.S
        ):
            at = match.start()
            dest = text.rva + at + 7 + struct.unpack_from("<i", code, at + 3)[0]
            if dest == marker_rva:
                anchors.append(text.rva + at)
        candidates = []
        for getter in re.finditer(rb"\x0f\xb6\x05....\xc3", code, re.S):
            at = getter.start()
            getter_rva = text.rva + at
            if not any(abs(getter_rva - anchor) < 0x10000 for anchor in anchors):
                continue
            gate = getter_rva + 7 + struct.unpack_from("<i", code, at + 3)[0]
            if not (
                writable.flags & 0x80000000
                and writable.rva <= gate < writable.rva + writable.raw_size
            ):
                continue
            if data[pe.offset(gate)] not in (0, 1):
                continue
            for setter in re.finditer(rb"\x88\x0d....", code[at + 8 : at + 64], re.S):
                setter_at = at + 8 + setter.start()
                setter_rva = text.rva + setter_at
                dest = setter_rva + 6 + struct.unpack_from("<i", code, setter_at + 2)[0]
                if dest == gate:
                    candidates.append(
                        (gate, getter_rva, getter.group(), setter_rva, setter.group())
                    )
        if len(candidates) != 1:
            raise UnsupportedVersion(f"无障碍状态地址无法唯一确认，候选数：{len(candidates)}")
        gate, getter_rva, getter_bytes, setter_rva, setter_bytes = candidates[0]
        return GateProof(
            hashlib.sha256(data).hexdigest(),
            pe.image_size,
            gate,
            getter_rva,
            getter_bytes,
            setter_rva,
            setter_bytes,
        )
