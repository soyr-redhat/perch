"""Perch — every coding agent you have up, one calm window.

Run:   python perch.py [--port 7766] [--browser] [--quiet-days 7]
Native window needs: pip install pywebview pywinpty  (otherwise falls back to browser)
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scanner import Scanner  # noqa: E402
from server import serve  # noqa: E402
from term import TermRegistry  # noqa: E402


def free_port(preferred: int) -> int:
    for candidate in range(preferred, preferred + 10):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"no free port near {preferred}")


def run_app(url: str, httpd) -> bool:
    """Native pywebview window; returns False if unavailable."""
    try:
        import webview
    except ImportError:
        return False

    class Bridge:
        def pick_folder(self):
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            return result[0] if result else None

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    webview.create_window(
        "Perch", url, js_api=Bridge(),
        width=1220, height=780, min_size=(940, 560),
        background_color="#0b0e1a",
    )
    webview.start()
    httpd.shutdown()
    return True


def main():
    # Windows pipes/consoles default to cp1252; the banner and sync reports are UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Perch — watch every agent, whatever its harness.")
    parser.add_argument("--port", type=int, default=7766)
    parser.add_argument("--browser", action="store_true", help="open in a browser tab instead of a native window")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--quiet-days", type=float, default=None,
                        help="how long a silent session stays listed, in days (default: settings)")
    parser.add_argument("--sync", action="store_true",
                        help="unify skills and MCP servers across harnesses, print a report, exit")
    args = parser.parse_args()

    import settings as settings_mod
    cfg = settings_mod.load()
    sharing = cfg["sharing"]

    if args.sync:
        import json
        from sync import sync_all
        print(json.dumps(sync_all(skills=sharing["skills"], mcp=sharing["mcp"],
                                  targets=tuple(sharing["targets"])), indent=1))
        return

    quiet = args.quiet_days if args.quiet_days is not None else cfg["watching"]["quietDays"]
    scanner = Scanner(quiet_days=quiet, config_dir=str(Path(__file__).parent))
    scanner.apply_settings(cfg)
    if sharing["autoSync"]:
        from sync import sync_all
        sync_all(skills=sharing["skills"], mcp=sharing["mcp"], targets=tuple(sharing["targets"]))
    terms = TermRegistry()
    port = free_port(args.port)
    httpd = serve(scanner, terms, port)

    url = f"http://127.0.0.1:{port}"
    print(f"Perch is watching → {url}", flush=True)
    print("  adapters: " + ", ".join(a.name for a in scanner.adapters), flush=True)
    print("  spawnable: " + ", ".join(a.name for a in scanner.adapters if a.cmd), flush=True)

    if not args.browser and not args.no_browser and run_app(url, httpd):
        return

    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
