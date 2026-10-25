import argparse
import json
import logging
import secrets
import sys
import threading
from pathlib import Path


def _open_when_ready(url, host, port):
    import socket
    import time
    import webbrowser

    for _ in range(150):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="wxbridge OneBot v11 微信私聊与群聊适配器")
    parser.add_argument("--config", type=Path, default=Path("data/onebot.json"))
    parser.add_argument("--init", action="store_true", help="生成配置和随机令牌，不覆盖已有文件")
    parser.add_argument("--mock", action="store_true", help="纯内存模拟，不连接或操作微信")
    parser.add_argument("--open-browser", action="store_true", help="就绪后自动打开 Web 配置页")
    args = parser.parse_args()
    try:
        from pydantic import ValidationError

        from .config import init_config, load_config

        if args.init:
            path = init_config(args.config, mock=args.mock)
            print(f"已生成配置：{path}")
            print("access_token 已随机生成；启动后可在 Web 页面填写联系人和接入地址。")
            return
        if not args.config.exists():
            init_config(args.config, mock=args.mock)
        config = load_config(args.config)
        import uvicorn

        from .server import MAX_API_REQUEST, create_app

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        admin_token = secrets.token_urlsafe(32)
        browser_host = "127.0.0.1" if config.host in {"0.0.0.0", "::"} else config.host
        url_host = f"[{browser_host}]" if ":" in browser_host else browser_host
        admin_url = f"http://{url_host}:{config.port}/#admin={admin_token}"
        print(f"Web 管理页面：{admin_url}", flush=True)
        if args.open_browser:
            threading.Thread(
                target=_open_when_ready, args=(admin_url, browser_host, config.port), daemon=True
            ).start()
        # Access logs may contain access_token query parameters; keep them disabled.
        uvicorn.run(
            create_app(
                config, mock=args.mock, config_path=args.config.resolve(), admin_token=admin_token
            ),
            host=config.host,
            port=config.port,
            access_log=False,
            ws_max_size=MAX_API_REQUEST,
            ws_max_queue=4,
        )
    except ImportError:
        parser.exit(1, "缺少 OneBot 依赖，请先执行：uv sync --extra onebot\n")
    except Exception as exc:
        if isinstance(exc, ValidationError):
            detail = json.dumps(
                exc.errors(include_input=False, include_url=False), ensure_ascii=False, default=str
            )
        else:
            detail = str(exc)
        parser.exit(1, f"启动失败：{detail}\n")


if __name__ == "__main__":
    main()
