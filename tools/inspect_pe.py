"""Read-only PE inspection. Does not load or execute the target DLL."""

import argparse
import json
import mmap
import struct


class PE:
    def __init__(self, data):
        self.data = data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0":
            raise ValueError("Not a PE file")
        count, optsize = struct.unpack_from("<H12xH", data, pe + 6)
        opt = pe + 24
        self.is64 = struct.unpack_from("<H", data, opt)[0] == 0x20B
        self.image_size = struct.unpack_from("<I", data, opt + 56)[0]
        self.image_base = struct.unpack_from(
            "<Q" if self.is64 else "<I", data, opt + (24 if self.is64 else 28)
        )[0]
        self.export_rva, self.export_size = struct.unpack_from(
            "<II", data, opt + (112 if self.is64 else 96)
        )
        self.sections = []
        for i in range(count):
            entry = opt + optsize + i * 40
            name = data[entry : entry + 8].rstrip(b"\0").decode("ascii")
            vsize, rva, rawsize, raw = struct.unpack_from("<IIII", data, entry + 8)
            self.sections.append(dict(name=name, rva=rva, size=vsize, raw=raw, rawsize=rawsize))

    def offset(self, rva):
        for sec in self.sections:
            if sec["rva"] <= rva < sec["rva"] + sec["rawsize"]:
                return sec["raw"] + rva - sec["rva"]
        raise ValueError(f"RVA not backed by a file section: {rva:#x}")

    def string(self, rva):
        at = self.offset(rva)
        return self.data[at : self.data.find(b"\0", at)].decode("ascii", "replace")

    def exports(self):
        if not self.export_rva:
            return []
        at = self.offset(self.export_rva)
        count = struct.unpack_from("<I", self.data, at + 24)[0]
        functions, names, ordinals = struct.unpack_from("<III", self.data, at + 28)
        result = []
        for i in range(count):
            name_rva = struct.unpack_from("<I", self.data, self.offset(names) + i * 4)[0]
            ordinal = struct.unpack_from("<H", self.data, self.offset(ordinals) + i * 2)[0]
            function_rva = struct.unpack_from(
                "<I", self.data, self.offset(functions) + ordinal * 4
            )[0]
            result.append((self.string(name_rva), hex(function_rva)))
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    args = parser.parse_args()
    with (
        open(args.file, "rb") as source,
        mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        pe = PE(data)
        print(
            json.dumps(
                {
                    "image_size": hex(pe.image_size),
                    "sections": pe.sections,
                    "exports": pe.exports(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
