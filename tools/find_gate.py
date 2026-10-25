"""Read-only, local analysis of references to the named accessibility flag."""

import argparse
import bisect
import json
import mmap
import re
import struct

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from inspect_pe import PE


def analyze(path, marker_name="qt.accessibility.core"):
    with open(path, "rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pe = PE(data)
        needle = data.find(marker_name.encode() + b"\0")
        if needle < 0:
            raise ValueError("Named accessibility marker not found")
        marker = next(
            s["rva"] + needle - s["raw"]
            for s in pe.sections
            if s["raw"] <= needle < s["raw"] + s["rawsize"]
        )
        text = next(s for s in pe.sections if s["name"] == ".text")
        pdata = next(s for s in pe.sections if s["name"] == ".pdata")
        functions = [
            struct.unpack_from("<III", data, at)
            for at in range(pdata["raw"], pdata["raw"] + pdata["size"] - 11, 12)
        ]
        starts = [f[0] for f in functions]
        dis = Cs(CS_ARCH_X86, CS_MODE_64)
        for match in re.finditer(
            rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....",
            data[text["raw"] : text["raw"] + text["rawsize"]],
            re.DOTALL,
        ):
            rva = text["rva"] + match.start()
            displacement = struct.unpack_from("<i", match.group(), 3)[0]
            if rva + 7 + displacement != marker:
                continue
            function = functions[bisect.bisect_right(starts, rva) - 1]
            begin, end, _ = function
            print(
                json.dumps(
                    {"marker": hex(marker), "xref": hex(rva), "function": [hex(begin), hex(end)]}
                )
            )
            for ins in dis.disasm(
                data[pe.offset(begin) : pe.offset(begin) + min(end - begin, 4096)], begin
            ):
                print(f"{ins.address:08x}  {ins.bytes.hex():24s} {ins.mnemonic:8s} {ins.op_str}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    analyze(parser.parse_args().file)
