"""Bounded, one-reply acceptance test against an explicitly named private chat.

Run using the existing wechat-ai-bot environment. Reads its model settings without
copying keys; only a new incoming '/ai ' message can produce a reply.
"""

import argparse
import asyncio
import json
import logging
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-project", type=Path, required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(args.bot_project / "src"))
    from wechat_bot.ai import AI
    from wechat_bot.config import Settings

    from wxbridge import WeChat, WeChatError
    from wxbridge.autoreply import AutoReply
    from wxbridge.winapi import find_window

    logging.basicConfig(level=logging.WARNING)
    settings = Settings(_env_file=args.bot_project / ".env")
    if settings.ai_backend != "compatible":
        raise RuntimeError("The acceptance test requires a configured real model")
    settings = settings.model_copy(
        update={
            "system_prompt": settings.system_prompt
            + "\n本轮是连通测试，请只用一句简短中文回复，不超过40字。"
        }
    )
    token = uuid4().hex[:8]
    report_path = root / "data" / ("live-ai-" + token + ".json")
    database = root / "data" / ("live-ai-" + token + ".sqlite3")
    report = {
        "started_at": datetime.now().astimezone().isoformat(),
        "chat": args.chat,
        "model": settings.ai_model,
        "prefix": "/ai ",
        "sent_count": 0,
        "database": str(database),
        "state": "starting",
    }
    stop = threading.Event()

    def save():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def reply(message):
        report.update(
            state="model_running",
            incoming_id=message.id,
            incoming_role=message.attr,
            incoming_chars=len(message.content),
        )
        save()
        print("INCOMING_PREFIX_MESSAGE: calling the configured model", flush=True)

        async def call():
            ai = AI(settings)
            try:
                return await ai.reply([], message.content[len("/ai ") :])
            finally:
                await ai.close()

        try:
            answer = asyncio.run(call())
        except Exception as exc:
            report.update(state="model_failed", error_type=type(exc).__name__)
            save()
            stop.set()
            raise WeChatError("测试模型调用失败，未发送回复") from None
        report.update(state="model_complete", answer_chars=len(answer))
        save()
        print("MODEL_COMPLETE: exact send verification next", flush=True)
        return answer

    try:
        hwnd, _ = find_window()
        with WeChat(hwnd=hwnd, enable_accessibility=True, foreground=True) as wx:
            info = wx.ChatInfo()
            if info["chat_name"] != args.chat or info["chat_type"] != "friend":
                raise WeChatError("当前会话不是已授权的测试私聊，未开启监听")

            def has_draft():
                backend = wx._backend
                composer = backend._chat()[2]
                return bool(backend._pattern(composer, "Value").CurrentValue)

            if wx._pool.submit(has_draft).result():
                raise WeChatError("当前输入框存在草稿，请先处理后再开始验收")
            report["initial_roles"] = dict(Counter(m.attr for m in wx.GetAllMessage()))

            class OneReply:
                def ChatWith(self, chat):
                    wx.ChatWith(chat)
                    report["state"] = "listening"
                    save()
                    print("READY: only new incoming '/ai ' text from " + args.chat, flush=True)

                def GetNextNewMessage(self, chat):
                    messages = wx.GetNextNewMessage(chat)
                    for message in messages:
                        if "/ai" in message.content:
                            observed = {
                                "id": message.id,
                                "role": message.attr,
                                "type": message.type,
                                "prefix_match": message.content.startswith("/ai "),
                            }
                            report.setdefault("observed_tests", []).append(observed)
                            save()
                            print("TEST_MESSAGE_OBSERVED:", message.attr, message.type, flush=True)
                    return messages

                def SendMsg(self, *positional, **keywords):
                    try:
                        result = wx.SendMsg(*positional, **keywords)
                        report.update(state=result.status, sent_count=1, echo_id=result.message_id)
                        save()
                        print("SEND_CONFIRMED_IN_UI", flush=True)
                        return result
                    except Exception as exc:
                        report.update(state="send_failed_or_unknown", error_type=type(exc).__name__)
                        save()
                        raise
                    finally:
                        # End this explicitly bounded test after one submission attempt.
                        stop.set()

            timer = threading.Timer(args.seconds, stop.set)
            timer.daemon = True
            timer.start()
            try:
                worker = AutoReply(
                    OneReply(),
                    args.chat,
                    reply,
                    database=database,
                    interval=0.5,
                    max_age=240,
                    predicate=lambda m: m.content.startswith("/ai "),
                )
                worker.run(stop)
                report["listener_error"] = worker.last_error
            finally:
                timer.cancel()
        if report["state"] == "listening":
            report["state"] = "timed_out_without_test_message"
    except Exception as exc:
        report.update(state="failed", error_type=type(exc).__name__)
        print("TEST_FAILED:", type(exc).__name__, str(exc), flush=True)
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        save()
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
