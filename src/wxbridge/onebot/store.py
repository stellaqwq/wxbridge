import hashlib
import json
import sqlite3
import time

from .identity import identity_scope


class MessageStore:
    """Local, persistent int32 IDs; UI IDs are scoped to one adapter session."""

    def __init__(self, config):
        config.database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(config.database)
        self.reserved_ids = {config.self_id, config.peer_id}
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS identity (value TEXT NOT NULL)")
            identity = json.dumps(identity_scope(config))
            old = self.connection.execute("SELECT value FROM identity").fetchone()
            if old is not None and old[0] != identity:
                raise ValueError("消息库绑定了其他账号或会话，请更换 database 路径")
            if old is None:
                self.connection.execute("INSERT INTO identity VALUES (?)", (identity,))
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id <= 2147483647), "
                "source TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS assets (token TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS members (key TEXT PRIMARY KEY, "
                "id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, last_seen INTEGER NOT NULL)"
            )
            self.connection.commit()
        except BaseException:
            self.connection.close()
            raise

    def add(self, source, payload):
        old = self.connection.execute(
            "SELECT id, payload FROM messages WHERE source=?", (source,)
        ).fetchone()
        if old:
            return old[0], False
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO messages (source, payload) VALUES (?, ?)",
                (source, json.dumps(payload, ensure_ascii=False)),
            )
            mid = cursor.lastrowid
            payload = {**payload, "message_id": mid, "real_id": mid}
            self.connection.execute(
                "UPDATE messages SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), mid),
            )
        return mid, True

    def get(self, mid):
        row = self.connection.execute("SELECT payload FROM messages WHERE id=?", (mid,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.connection.close()

    def add_asset(self, token, payload):
        with self.connection:
            self.connection.execute(
                "INSERT INTO assets VALUES (?, ?)", (token, json.dumps(payload, ensure_ascii=False))
            )

    def get_asset(self, token):
        row = self.connection.execute(
            "SELECT payload FROM assets WHERE token=?", (token,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def member(self, key, name=""):
        row = self.connection.execute("SELECT id, name FROM members WHERE key=?", (key,)).fetchone()
        if row:
            uid, nickname = row
            if name:
                nickname = name
        else:
            salt = 0
            while True:
                digest = hashlib.sha256(f"{key}:{salt}".encode()).digest()
                uid = 1_000_000_000_000 + int.from_bytes(digest[:6], "big")
                if uid not in self.reserved_ids and self.get_member(uid) is None:
                    break
                salt += 1
            nickname = name or f"群成员-{str(uid)[-6:]}"
        seen = int(time.time())
        with self.connection:
            self.connection.execute(
                "INSERT INTO members VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "name=excluded.name, last_seen=excluded.last_seen",
                (key, uid, nickname, seen),
            )
        return uid, nickname

    def get_member(self, uid):
        row = self.connection.execute(
            "SELECT id, name, last_seen FROM members WHERE id=?", (uid,)
        ).fetchone()
        return {"user_id": row[0], "nickname": row[1], "last_sent_time": row[2]} if row else None

    def members(self):
        return [
            {"user_id": row[0], "nickname": row[1], "last_sent_time": row[2]}
            for row in self.connection.execute(
                "SELECT id, name, last_seen FROM members ORDER BY id"
            )
        ]
