"""Diagnostic only: no chat navigation, typing, or message sending."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wxbridge.accessibility import AccessibilityLease
from wxbridge.pe import locate_gate
from wxbridge.uia import UIA
from wxbridge.winapi import find_window, weixin_module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--tree", type=Path)
    args = parser.parse_args()
    hwnd, pid = find_window()
    base, size, path = weixin_module(pid)
    proof = locate_gate(path)
    print(
        json.dumps(
            {
                "pid": pid,
                "hwnd": hwnd,
                "module_size": size,
                "gate_rva": hex(proof.gate_rva),
                "sha256": proof.sha256,
            }
        )
    )
    uia = UIA()
    lease = AccessibilityLease(pid, hwnd, uia)
    try:
        print(json.dumps({"nodes_before": len(uia.nodes(hwnd))}))
        if args.activate:
            print(json.dumps(lease.enable()))
        nodes = uia.nodes(hwnd)
        print(json.dumps({"nodes_after": len(nodes), "ready": uia.ready(hwnd)}))
        if args.tree:

            def dump(element, depth=0):
                if depth > 20:
                    return {}
                rect = element.CurrentBoundingRectangle
                return {
                    "name": element.CurrentName,
                    "class": element.CurrentClassName,
                    "id": element.CurrentAutomationId,
                    "type": element.CurrentControlType,
                    "runtime_id": list(element.GetRuntimeId() or []),
                    "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "children": [dump(n, depth + 1) for n in uia.children(element)],
                }

            args.tree.parent.mkdir(parents=True, exist_ok=True)
            args.tree.write_text(
                json.dumps(dump(uia.root(hwnd)), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("Tree saved locally")
    finally:
        lease.close()
        uia.close()


if __name__ == "__main__":
    main()
