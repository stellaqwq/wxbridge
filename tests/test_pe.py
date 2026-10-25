import struct

import pytest

from wxbridge.errors import UnsupportedVersion
from wxbridge.pe import locate_gate


def binary(duplicate=False, marker=True, gate_byte=0):
    data = bytearray(0x1000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x8664, 3)
    struct.pack_into("<H", data, 0x94, 0xF0)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<I", data, 0x98 + 56, 0x4000)
    for index, (name, rva, raw, flags) in enumerate(
        [
            (b".text", 0x1000, 0x400, 0x60000020),
            (b".rdata", 0x2000, 0x800, 0x40000040),
            (b".data", 0x3000, 0xC00, 0xC0000040),
        ]
    ):
        at = 0x188 + index * 40
        data[at : at + len(name)] = name
        struct.pack_into("<IIII", data, at + 8, 0x400, rva, 0x400, raw)
        struct.pack_into("<I", data, at + 36, flags)
    if marker:
        data[0x800:0x816] = b"qt.accessibility.core\0"
    data[0x400:0x407] = b"\x48\x8d\x15" + struct.pack("<i", 0x2000 - 0x1007)

    def pair(at, gate):
        data[at : at + 8] = b"\x0f\xb6\x05" + struct.pack("<i", gate - (at + 0xC00 + 7)) + b"\xc3"
        data[at + 16 : at + 23] = (
            b"\x88\x0d" + struct.pack("<i", gate - (at + 0xC00 + 22)) + b"\xc3"
        )

    pair(0x420, 0x3010)
    data[0xC10] = gate_byte
    if duplicate:
        pair(0x460, 0x3020)
    return data


@pytest.mark.parametrize("gate_byte", [0, 1])
def test_locates_shared_boolean_getter_and_setter(tmp_path, gate_byte):
    path = tmp_path / "client.dll"
    path.write_bytes(binary(gate_byte=gate_byte))
    proof = locate_gate(path)
    assert proof.gate_rva == 0x3010
    assert proof.getter_rva == 0x1020
    assert proof.setter_rva == 0x1030
    assert len(proof.sha256) == 64


@pytest.mark.parametrize("kwargs", [{"duplicate": True}, {"marker": False}, {"gate_byte": 9}])
def test_unverified_candidates_are_rejected(tmp_path, kwargs):
    path = tmp_path / "client.dll"
    path.write_bytes(binary(**kwargs))
    with pytest.raises(UnsupportedVersion):
        locate_gate(path)


def test_mismatched_getter_setter_targets_are_rejected(tmp_path):
    data = binary()
    struct.pack_into("<i", data, 0x432, 0x3020 - 0x1036)
    path = tmp_path / "client.dll"
    path.write_bytes(data)
    with pytest.raises(UnsupportedVersion):
        locate_gate(path)
