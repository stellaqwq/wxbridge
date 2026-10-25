import uuid
from collections import deque

from .identity import identity_scope


class MessageMonitor:
    """Bounded Web-only display buffer; never consumes or clears OneBot events."""

    def __init__(self, capacity=200):
        self.capacity = capacity
        self.messages = deque(maxlen=capacity)
        self.scope = None
        self.sequence = 0
        self.session = uuid.uuid4().hex

    def set_scope(self, config):
        scope = identity_scope(config)
        if scope != self.scope:
            self.scope = scope
            self.messages.clear()
            self.sequence = 0
            self.session = uuid.uuid4().hex

    def record(self, message):
        # Status changes replace a media row, while advancing the polling cursor.
        key = message.get("entry_id")
        if key:
            for old in tuple(self.messages):
                if old.get("entry_id") == key:
                    self.messages.remove(old)
                    break
        self.sequence += 1
        self.messages.append({**message, "sequence": self.sequence})

    def page(self, after=0, session=None, limit=100):
        reset = session != self.session or after > self.sequence
        cursor = 0 if reset else after
        oldest = self.messages[0]["sequence"] if self.messages else self.sequence + 1
        dropped = max(0, oldest - cursor - 1) if not reset else 0
        messages = [message for message in self.messages if message["sequence"] > cursor][:limit]
        return {
            "session": self.session,
            "messages": messages,
            "cursor": messages[-1]["sequence"] if messages else max(cursor, self.sequence),
            "reset": reset,
            "dropped": dropped,
            "capacity": self.capacity,
        }
