import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict

from . import WeChat
from .errors import WeChatError


def doctor(*, activate=False):
    from .accessibility import AccessibilityLease
    from .compatibility import VERIFIED_DLLS
    from .pe import locate_gate
    from .uia import UIA
    from .winapi import find_window, weixin_module

    hwnd, pid = find_window()
    _, size, path = weixin_module(pid)
    proof = locate_gate(path)
    uia = UIA()
    lease = AccessibilityLease(pid, hwnd, uia)
    try:
        before = len(uia.nodes(hwnd))
        activation = lease.enable() if activate else None
        return {
            "pid": pid,
            "hwnd": hwnd,
            "dll": str(path),
            "module_size": size,
            "sha256": proof.sha256,
            "verified_version": VERIFIED_DLLS.get(proof.sha256),
            "gate_rva": hex(proof.gate_rva),
            "nodes_before": before,
            "nodes_after": len(uia.nodes(hwnd)),
            "chat_controls_ready": uia.ready(hwnd),
            "activation": activation,
        }
    finally:
        lease.close()
        uia.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["onebot"]:
        from .onebot.cli import main as onebot_main

        sys.argv.pop(1)
        return onebot_main()
    parser = argparse.ArgumentParser(description="独立微信 Windows 自动化开发库 wxbridge")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("onebot", help="启动 OneBot v11；使用 onebot --help 查看配置")
    inspect = commands.add_parser("doctor", help="只读诊断；开关须显式启用")
    inspect.add_argument("--enable-accessibility", action="store_true")
    scan = commands.add_parser("scan", help="只读检查本地 DLL 候选地址，不修改进程")
    scan.add_argument("dll")
    for command in ("messages", "send", "send-file", "send-image"):
        sub = commands.add_parser(command)
        sub.add_argument("--enable-accessibility", action="store_true")
        sub.add_argument("--foreground", action="store_true", help="显示并激活微信主窗口")
        if command == "send":
            sub.add_argument("text")
            sub.add_argument("--to", required=True, help="当前会话的精确名称")
        elif command in ("send-file", "send-image"):
            sub.add_argument("path")
            sub.add_argument("--to", required=True, help="当前会话的精确名称")
    args = parser.parse_args()
    try:
        if args.command == "scan":
            from .compatibility import VERIFIED_DLLS
            from .pe import locate_gate

            proof = locate_gate(args.dll)
            result = {
                "sha256": proof.sha256,
                "gate_rva": hex(proof.gate_rva),
                "verified_version": VERIFIED_DLLS.get(proof.sha256),
            }
        elif args.command == "doctor":
            result = doctor(activate=args.enable_accessibility)
        else:
            from .winapi import find_window, show_window

            hwnd, _ = find_window()
            if args.foreground:
                show_window(hwnd)
            with WeChat(hwnd=hwnd, enable_accessibility=args.enable_accessibility) as wx:
                if args.command == "send":
                    result = asdict(wx.SendMsg(args.text, who=args.to))
                elif args.command in ("send-file", "send-image"):
                    sender = wx.SendImage if args.command == "send-image" else wx.SendFile
                    result = asdict(sender(args.path, who=args.to))
                else:
                    messages = wx.GetAllMessage()
                    result = {
                        "chat": wx.ChatInfo(),
                        "roles": dict(Counter(m.attr for m in messages)),
                        "messages": [asdict(m) for m in messages],
                    }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (WeChatError, OSError, ValueError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
