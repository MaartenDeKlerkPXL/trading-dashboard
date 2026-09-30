"""Entry point: `python -m backend` starts the server and opens the dashboard."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .config import DATA_DIR, ConfigError, load_settings
from .main import create_app


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        DATA_DIR / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def _open_browser_when_ready(url: str) -> None:
    def wait_and_open() -> None:
        for _ in range(60):
            try:
                urllib.request.urlopen(url + "api/health", timeout=1)
            except OSError:
                time.sleep(0.5)
                continue
            webbrowser.open(url)
            return

    threading.Thread(target=wait_and_open, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading Dashboard")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n  Fout in config.toml: {exc}\n")
        return 1

    host, port = settings.app.host, settings.app.port
    url = f"http://localhost:{port}/"
    if _port_in_use(host, port):
        print(
            f"\n  Poort {port} is al in gebruik. Draait het dashboard al in een ander Terminal-venster?\n"
            f"  Open dan gewoon {url} in je browser, of stop het andere venster met Ctrl+C.\n"
        )
        return 1

    _setup_logging()
    print(f"\n  Dashboard start op {url}\n  Stoppen: druk op Ctrl+C in dit venster.\n")
    if not args.no_browser:
        _open_browser_when_ready(url)
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
