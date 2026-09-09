"""Perch: one installed workspace, shared by desktop and command-line workflows."""

from __future__ import annotations
import argparse
import json
import logging
import sys
import threading
import webbrowser

from desktop.lifecycle import Instance, extend_path
from scanner import Scanner
from server import serve
from storage import DATA_DIR
from term import TermRegistry
import settings


def run_app(url, httpd):
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    cfg = settings.load()
    # pywebview builds its API with Function() and uses eval for bridge replies.
    # Enable this only for the native window, before its first navigation.
    httpd.native_bridge = True

    class Bridge:
        def pick_folder(self):
            result = window.create_file_dialog(webview.FileDialog.FOLDER)
            return result[0] if result else None

    window = webview.create_window(
        "Perch",
        url,
        js_api=Bridge(),
        width=cfg["window"]["width"],
        height=cfg["window"]["height"],
        min_size=(860, 580),
        text_select=True,
        background_color="#242a2e",
    )

    def action(name):
        window.evaluate_js(f"window.perchActions.{name}()")

    def activate():
        window.restore()
        window.show()

    httpd.activate = activate

    def on_closing():
        if any(t["alive"] for t in httpd.terms.list()) or httpd.delivering:
            if not window.create_confirmation_dialog(
                "Quit Perch?",
                "Quitting stops terminals and replies started by Perch. Other agents keep running.",
            ):
                return False
        if not httpd.demo:
            settings.patch(cfg, {"window": {"width": int(window.width), "height": int(window.height)}})
        return True

    window.events.closing += on_closing
    menus = [
        Menu(
            "Workspace",
            [
                MenuAction("New session", lambda: action("newSession")),
                MenuAction("Shared tools", lambda: action("sharedTools")),
                MenuSeparator(),
                MenuAction("Settings", lambda: action("settings")),
                MenuSeparator(),
                MenuAction("Quit Perch", window.destroy),
            ],
        )
    ]
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"), menu=menus)


def auto_share(stop):
    from sync import fingerprint, sync_all

    previous = None
    while not stop.wait(30):
        cfg = settings.load()["sharing"]
        if not cfg["autoSync"]:
            previous = None
            continue
        try:
            current = (fingerprint(), json.dumps(cfg, sort_keys=True))
            if current != previous:
                report = sync_all(skills=cfg["skills"], mcp=cfg["mcp"], targets=tuple(cfg["targets"]))
                previous = None if any(report[k]["errors"] for k in ("skills", "mcp")) else (fingerprint(), json.dumps(cfg, sort_keys=True))
        except (OSError, ValueError):
            logging.exception("Automatic sharing failed")


def parser():
    p = argparse.ArgumentParser(description="Perch — a desktop workspace for coding agents")
    p.add_argument("--browser", action="store_true", help="Explicit browser mode for development")
    p.add_argument("--no-browser", action="store_true", help="Serve locally without opening a window")
    p.add_argument("--port", type=int, default=7766)
    p.add_argument("--quiet-days", type=float)
    p.add_argument("--sync", action="store_true", help="Apply compatible skill and MCP additions and exit")
    p.add_argument("--dry-run", action="store_true", help="Preview sharing without changing harness files")
    p.add_argument("--tools", action="store_true", help="Show shared tool inventory as JSON and exit")
    p.add_argument("--targets", nargs="+", choices=settings.TARGETS, help="Harnesses to receive shared tools")
    p.add_argument("--demo", action="store_true", help="Read-only synthetic sessions for UI evaluation")
    p.add_argument("--version", action="version", version="Perch 0.2.0")
    return p


def main():
    if sys.argv[1:2] == ["--perch-pty-child"]:
        from term import exec_pty_child

        exec_pty_child(sys.argv[2:])
    args = parser().parse_args()
    extend_path()
    cfg = settings.load()
    if args.sync or args.dry_run or args.tools:
        from sync import overview, sync_all

        sharing = cfg["sharing"]
        result = (
            overview()
            if args.tools
            else sync_all(
                skills=sharing["skills"],
                mcp=sharing["mcp"],
                targets=tuple(args.targets or sharing["targets"]),
                dry_run=args.dry_run,
            )
        )
        print(json.dumps(result, indent=2))
        return int(any(result.get(section, {}).get(kind) for section in ("skills", "mcp") for kind in ("errors", "conflicts", "blocked"))) if not args.tools else 0
    if args.quiet_days is not None:
        if not 0.1 <= args.quiet_days <= 90:
            raise SystemExit("--quiet-days must be between 0.1 and 90")
        cfg["watching"]["quietDays"] = args.quiet_days
    if not (args.browser or args.no_browser):
        try:
            import webview  # noqa: F401
        except ImportError:
            raise SystemExit(
                "Desktop runtime missing. Install with: pip install -e .\nUse --browser only for development."
            )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(DATA_DIR / "perch.log"), level=logging.WARNING)
    instance = Instance()
    if not instance.acquire():
        try:
            instance.activate()
        except (OSError, ValueError):
            raise SystemExit("Perch is starting or unavailable. Wait a moment and open it again.")
        finally:
            instance.close()
        return 0
    server = None
    try:
        if args.demo:
            from demo import DemoScanner

            scanner = DemoScanner()
        else:
            scanner = Scanner(config_dir=str(DATA_DIR))
            scanner.apply_settings(cfg)
            scanner.start_watching()
        terms = TermRegistry()
        try:
            server = serve(scanner, terms, args.port, demo=args.demo)
        except OSError:
            server = serve(scanner, terms, 0, demo=args.demo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instance.publish(server)
        if not args.demo:
            threading.Thread(target=auto_share, args=(server.stop_event,), daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/?token={server.token}"
        if args.browser:
            webbrowser.open(url)
        if args.browser or args.no_browser:
            # Development mode needs an authenticated entry URL; never log it in desktop mode.
            print(url, flush=True)
            thread.join()
        else:
            run_app(url, server)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.close()
        instance.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
