"""Direct Microsoft UI Automation COM access. Own this object on one thread."""

import ctypes
import gc


class UIA:
    def __init__(self):
        import comtypes
        import comtypes.client

        self.comtypes = comtypes
        comtypes.CoInitialize()
        # Generate typelib bindings in memory; do not modify the user's Python install.
        comtypes.client.gen_dir = None
        self.api = comtypes.client.GetModule("UIAutomationCore.dll")
        self.auto = comtypes.client.CreateObject(
            self.api.CUIAutomation, interface=self.api.IUIAutomation
        )
        try:
            extended = self.auto.QueryInterface(self.api.IUIAutomation2)
            extended.ConnectionTimeout = 2000
            extended.TransactionTimeout = 2000
        except (AttributeError, comtypes.COMError):
            pass
        self.condition = self.auto.CreateTrueCondition()

    def root(self, hwnd):
        return self.auto.ElementFromHandle(ctypes.c_void_p(hwnd))

    def children(self, element, descendants=False):
        items = element.FindAll(4 if descendants else 2, self.condition)
        return [items.GetElement(i) for i in range(items.Length)]

    def nodes(self, hwnd):
        root = self.root(hwnd)
        return [root, *self.children(root, descendants=True)]

    def ready(self, hwnd):
        return any(n.CurrentClassName == "mmui::MainView" for n in self.nodes(hwnd))

    def close(self):
        self.auto = self.condition = None
        gc.collect()
        self.comtypes.CoUninitialize()
