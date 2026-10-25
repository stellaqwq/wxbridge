"""Explicit foreground media operations. No sends unless a path is specified."""

import argparse

from wxbridge import WeChat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True)
    parser.add_argument("--send-file")
    parser.add_argument("--send-image")
    parser.add_argument("--save-to", help="保存当前可见文件/图片，不执行文件")
    parser.add_argument("--transcribe", action="store_true", help="只读取可见语音的转文字结果")
    args = parser.parse_args()
    with WeChat(enable_accessibility=True, foreground=True) as wx:
        wx.ChatWith(args.chat)
        if args.send_file:
            print(wx.SendFile(args.send_file, who=args.chat))
        if args.send_image:
            print(wx.SendImage(args.send_image, who=args.chat))
        for message in wx.GetAllMessage():
            if args.save_to and message.type in {"file", "image"} and message.attr != "other":
                print(wx.DownloadAttachment(message, args.save_to))
            if args.transcribe and message.type == "voice" and message.attr != "other":
                print(wx.TranscribeVoice(message))


if __name__ == "__main__":
    main()
