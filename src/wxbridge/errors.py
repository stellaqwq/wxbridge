class WeChatError(RuntimeError):
    """The operation could not be completed."""


class UnsupportedVersion(WeChatError):
    pass


class ChatChanged(WeChatError):
    pass


class DeliveryUncertain(WeChatError):
    """Submission started. Check the client; never automatically resend."""


class BatchSendError(WeChatError):
    """A batch stopped. Completed items are exposed so callers do not replay them."""

    def __init__(self, completed, failed_path, uncertain=False):
        self.completed = tuple(completed)
        self.failed_path = str(failed_path)
        self.uncertain = uncertain
        super().__init__(f"附件批次已停止，已有 {len(completed)} 项界面确认；请核对，不要重发整批")
