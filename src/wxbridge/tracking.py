from collections import deque


class MessageTracker:
    """At-most-once within a UI session; gaps are rebaselined instead of replayed."""

    def __init__(self, capacity=20000):
        self.capacity = capacity
        self.reset()

    def reset(self):
        self.context = None
        self.initialized = False
        self.visible = ()
        self.seen = set()
        self.order = deque()

    def update(self, snapshot):
        context = (snapshot.chat_name, snapshot.context_id)
        messages = snapshot.messages
        ids = tuple(m.id for m in messages)
        overlap = set(self.visible) & set(ids)
        baseline = (
            not self.initialized
            or self.context != context
            or (self.visible and ids and not overlap)
            or (not self.visible and bool(self.seen))
        )
        fresh = []
        if not baseline:
            # Only rows after the last old visible row can be new arrivals.
            # Loading older history at the top must not trigger replies.
            boundary = max((i for i, mid in enumerate(ids) if mid in overlap), default=-1)
            fresh = [m for m in messages[boundary + 1 :] if m.id not in self.seen]
        for mid in ids:
            if mid not in self.seen:
                self.seen.add(mid)
                self.order.append(mid)
        while len(self.order) > self.capacity:
            self.seen.discard(self.order.popleft())
        self.context = context
        self.initialized = True
        self.visible = ids
        return fresh
