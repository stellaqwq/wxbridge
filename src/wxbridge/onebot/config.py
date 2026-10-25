import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLACEHOLDER_CHAT = "请填写微信联系人唯一备注"


def configured_chat(name):
    return bool(name.strip()) and name != PLACEHOLDER_CHAT


class OneBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    self_id: int = Field(default=10000, gt=0, le=2**63 - 1, strict=True)
    nickname: str = Field(default="微信机器人", min_length=1, max_length=100)
    peer_id: int = Field(default=10001, gt=0, le=2**63 - 1, strict=True)
    chat_name: str = Field(min_length=1, max_length=100)
    chat_type: Literal["friend", "group"] = "friend"
    group_id: int = Field(default=20001, gt=0, le=2**63 - 1, strict=True)
    group_mention_name: str = Field(default="", max_length=100)
    access_token: str = Field(min_length=16, max_length=256, repr=False)
    host: str = "127.0.0.1"
    port: int = Field(default=6700, ge=1, le=65535)
    database: Path = Path("data/onebot.sqlite3")
    enable_accessibility: bool = False
    foreground: bool = False
    poll_interval: float = Field(default=1, ge=0.1, le=60, allow_inf_nan=False)
    heartbeat_interval: float = Field(default=15, ge=1, le=3600, allow_inf_nan=False)
    reverse_ws_urls: list[str] = Field(default_factory=list, max_length=8)
    reconnect_interval: float = Field(default=3, ge=0.1, le=300, allow_inf_nan=False)
    http_post_urls: list[str] = Field(default_factory=list, max_length=8)
    http_post_secret: str = Field(default="", repr=False)
    http_timeout: float = Field(default=10, ge=0.1, le=120, allow_inf_nan=False)
    rate_limit_interval: float = Field(default=0.5, ge=0.1, le=60, allow_inf_nan=False)
    media_base_url: str = ""

    @field_validator("group_mention_name")
    @classmethod
    def mention_name(cls, value):
        return value.strip().removeprefix("@")

    @field_validator("media_base_url")
    @classmethod
    def valid_media_base(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("媒体服务地址需要 http(s)://主机:端口，不含路径、令牌或查询参数")
        _ = url.port
        return value.rstrip("/")

    @field_validator("chat_name", "nickname", "host")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("名称和地址不能为空或带首尾空白")
        return value

    @field_validator("access_token")
    @classmethod
    def valid_token(cls, value):
        if not value.isascii() or any(char.isspace() for char in value):
            raise ValueError("access_token 需要不含空白的 ASCII 字符")
        return value

    @field_validator("reverse_ws_urls", "http_post_urls")
    @classmethod
    def valid_urls(cls, values, info):
        schemes = {"ws", "wss"} if info.field_name == "reverse_ws_urls" else {"http", "https"}
        if len(set(values)) != len(values):
            raise ValueError("接入地址不能重复")
        for value in values:
            url = urlsplit(value)
            if url.scheme not in schemes or not url.hostname or url.fragment or url.username:
                raise ValueError("接入地址的协议、主机或格式不正确")
            _ = url.port  # Also reject malformed ports at startup.
        return values

    @model_validator(mode="after")
    def different_ids(self):
        if self.self_id == self.peer_id:
            raise ValueError("self_id 和 peer_id 不能相同")
        return self


def load_config(path):
    path = Path(path).resolve()
    config = OneBotConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    if not config.database.is_absolute():
        config.database = (path.parent / config.database).resolve()
    return config


def init_config(path, *, mock=False):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "chat_name": "模拟好友" if mock else PLACEHOLDER_CHAT,
        "access_token": secrets.token_urlsafe(32),
        "database": "onebot.sqlite3",
    }
    config = OneBotConfig(**values)
    # Never overwrite an existing token or configuration.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(config.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def save_config(path, config):
    """Atomically replace settings while retaining the original relative database path."""
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    values = config.model_dump(mode="json")
    old_database = raw.get("database", "data/onebot.sqlite3")
    resolved_old = Path(old_database)
    if not resolved_old.is_absolute():
        resolved_old = (path.parent / resolved_old).resolve()
    if config.database == resolved_old:
        values["database"] = old_database
    else:
        values["database"] = os.path.relpath(config.database, path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".onebot-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
