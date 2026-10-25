"""Explicitly configured, single private-chat AI example. No default recipient."""

import argparse
import json
import logging
import os
import urllib.request
from dataclasses import replace

from wxbridge import WeChat, WeChatError
from wxbridge.autoreply import AutoReply
from wxbridge.winapi import find_window, show_window


def model_reply(message):
    base = os.environ["AI_BASE_URL"].rstrip("/")
    model = os.environ["AI_MODEL"]
    key = os.getenv("AI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {
        "model": model,
        "stream": False,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": "你是 AI 助手，请简短、自然地回复。"},
            {"role": "user", "content": message.content},
        ],
    }
    request = urllib.request.Request(
        base + "/chat/completions", headers=headers, data=json.dumps(payload).encode()
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    choice = result["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise WeChatError("模型输出不完整，不会自动发送")
    return choice["message"]["content"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True, help="仅一个已打开的私聊好友唯一备注")
    parser.add_argument("--enable-accessibility", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--prefix", default="/ai ", help="仅处理以此开头的测试消息")
    parser.add_argument("--database", default="data/ai-replies.sqlite3")
    parser.add_argument("--voice", action="store_true", help="实验：该私聊的新语音转文字后回复")
    args = parser.parse_args()
    if not args.prefix.strip():
        parser.error("测试前缀不能为空")
    for key in ("AI_BASE_URL", "AI_MODEL"):
        if not os.getenv(key):
            parser.error("请先设置环境变量 " + key)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    hwnd, _ = find_window()
    if args.foreground:
        show_window(hwnd)
    with WeChat(hwnd=hwnd, enable_accessibility=args.enable_accessibility) as wx:
        if wx.ChatInfo()["chat_name"] != args.chat:
            raise WeChatError("请先手动打开指定的测试私聊")

        def reply(message):
            text = (
                wx.TranscribeVoice(message)
                if message.type == "voice"
                else message.content[len(args.prefix) :]
            )
            return model_reply(replace(message, content=text))

        # Voice is opt-in, restricted to this one named chat. No audio upload here.
        worker = AutoReply(
            wx,
            args.chat,
            reply,
            database=args.database,
            predicate=lambda message: (
                message.type == "voice" or message.content.startswith(args.prefix)
            ),
            message_types=("text", "voice") if args.voice else ("text",),
        )
        try:
            worker.run()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
