"""Read-only UIA property/pattern research, temporarily enabling accessibility."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wxbridge.accessibility import AccessibilityLease
from wxbridge.uia import UIA
from wxbridge.winapi import find_window


def main():
    hwnd, pid = find_window()
    if "--show" in sys.argv:
        import time

        import win32gui

        win32gui.ShowWindow(hwnd, 9)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.4)
    uia = UIA()
    lease = AccessibilityLease(pid, hwnd, uia)
    try:
        lease.enable()
        props = {
            getattr(uia.api, name): name
            for name in dir(uia.api)
            if name.startswith("UIA_") and name.endswith("PropertyId")
        }
        results = []
        for node in uia.nodes(hwnd):
            if node.CurrentClassName not in (
                "mmui::ChatTextItemView",
                "mmui::ChatInputField",
                "mmui::ChatTitleBarChatSingleView",
            ):
                continue
            result = {"class": node.CurrentClassName, "runtime_id": list(node.GetRuntimeId())}
            if node.CurrentClassName == "mmui::ChatTitleBarChatSingleView":
                result["descendants"] = [
                    {
                        "class": n.CurrentClassName,
                        "id": n.CurrentAutomationId,
                        "name": n.CurrentName,
                    }
                    for n in uia.children(node, True)
                ]
            for key, name in props.items():
                try:
                    value = node.GetCurrentPropertyValue(key)
                    if value is not None and value != "" and value != 0:
                        result[name] = (
                            value
                            if isinstance(value, (int, float, str, tuple, list))
                            else str(value)
                        )
                except Exception:
                    pass
            patterns = {}
            for name in ("Value", "Text", "LegacyIAccessible", "Invoke"):
                try:
                    obj = node.GetCurrentPattern(getattr(uia.api, "UIA_" + name + "PatternId"))
                    if not obj:
                        continue
                    pattern = obj.QueryInterface(
                        getattr(uia.api, "IUIAutomation" + name + "Pattern")
                    )
                    info = {}
                    if name == "Value":
                        info["value"] = pattern.CurrentValue
                        info["read_only"] = pattern.CurrentIsReadOnly
                    if name == "Text":
                        info["text"] = pattern.DocumentRange.GetText(-1)
                        info["rects"] = list(pattern.DocumentRange.GetBoundingRectangles())
                    if name == "LegacyIAccessible":
                        for key in (
                            "CurrentName",
                            "CurrentValue",
                            "CurrentDescription",
                            "CurrentRole",
                            "CurrentState",
                            "CurrentDefaultAction",
                            "CurrentHelp",
                            "CurrentChildId",
                        ):
                            try:
                                info[key] = getattr(pattern, key)
                            except Exception:
                                pass
                    patterns[name] = info
                except Exception:
                    pass
            result["patterns"] = patterns
            results.append(result)
        Path("data/patterns.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print("Saved read-only properties locally; nodes:", len(results))
    finally:
        lease.close()
        uia.close()


if __name__ == "__main__":
    main()
