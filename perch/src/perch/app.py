"""Perch: connect context and capabilities across coding harnesses."""

from __future__ import annotations
import argparse
import json
import logging
import sys
import threading
import webbrowser

from .desktop.lifecycle import Instance, extend_path
from .scanner import Scanner
from .server import serve
from .storage import DATA_DIR
from .term import TermRegistry
from . import settings


def run_app(url, httpd):
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    cfg = settings.load()
    # pywebview builds its API with Function() and uses eval for bridge replies.
    # Enable this only for the native window, before its first navigation.
    httpd.native_bridge = True

    class Bridge:
        def installation(self, repair=False, cli=True):
            from .desktop import installation
            import subprocess

            try:
                if repair:
                    if httpd.demo:
                        raise ValueError("Demo mode is read-only")
                    if type(cli) is not bool:
                        raise ValueError("CLI preference must be a boolean")
                    return installation.repair(cli=cli)
                return installation.status()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                return {"error": str(exc)}

        def open_cli(self, agent_id):
            from .desktop.lifecycle import open_cli_session

            try:
                if httpd.demo:
                    raise ValueError("Demo mode is read-only")
                if agent_id in httpd.delivering or any(t.get("session") == agent_id.partition(":")[2] and t["alive"] for t in httpd.terms.list()):
                    raise ValueError("This session is busy in Perch")
                open_cli_session(httpd.scanner, agent_id)
                return {"ok": True}
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}

        def show_export(self, snapshot_id):
            from .conversations import archive_path
            from .desktop.lifecycle import show_export

            try:
                show_export(archive_path(snapshot_id))
                return {"ok": True}
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}

        def open_session(self, agent_id):
            from .desktop.lifecycle import open_codex_session

            agent = next((a for a in httpd.scanner.scan()["agents"] if a["id"] == agent_id), None)
            try:
                if httpd.demo or not agent or agent["harness"] != "codex":
                    raise ValueError("A recorded Codex session is required")
                open_codex_session(agent_id.partition(":")[2])
                return {"ok": True}
            except (OSError, ValueError) as exc:
                return {"error": str(exc)}

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
                MenuAction("Resources", lambda: action("sharedTools")),
                MenuSeparator(),
                MenuAction("Settings", lambda: action("settings")),
                MenuSeparator(),
                MenuAction("Quit Perch", window.destroy),
            ],
        )
    ]
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"), menu=menus)


def auto_share(stop):
    from .sync import fingerprint, sync_all

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
    p = argparse.ArgumentParser(description="Perch — connect context and capabilities across coding harnesses")
    p.add_argument("--browser", action="store_true", help="Explicit browser mode for development")
    p.add_argument("--no-browser", action="store_true", help="Serve locally without opening a window")
    p.add_argument("--port", type=int, default=7766)
    p.add_argument("--quiet-days", type=float)
    p.add_argument("--sync", action="store_true", help="Apply compatible skill and MCP additions and exit")
    p.add_argument("--dry-run", action="store_true", help="Preview sharing without changing harness files")
    p.add_argument("--tools", action="store_true", help="Show shared tool inventory as JSON and exit")
    p.add_argument("--capabilities", action="store_true", help="List resources, plugin components, and compatibility as JSON")
    p.add_argument("--resolve-skill", metavar="NAME", help="Review skill copies and choose a shared source")
    p.add_argument("--skill-source", metavar="SOURCE_ID", help="Source ID from --resolve-skill")
    p.add_argument("--link", metavar="RESOURCE_ID", help="Review a resource connection; use --apply with --revision to apply")
    p.add_argument("--target", choices=settings.TARGETS, help="Destination for --link")
    p.add_argument("--apply", action="store_true", help="Apply the reviewed connection")
    p.add_argument("--revision", help="Revision returned by --link")
    p.add_argument("--targets", nargs="+", choices=settings.TARGETS, help="Harnesses to receive shared tools")
    p.add_argument("--demo", action="store_true", help="Read-only synthetic sessions for UI evaluation")
    p.add_argument("--sessions", action="store_true", help="List detected session IDs as JSON and exit")
    p.add_argument("--export-session", metavar="HARNESS:ID", help="Save a complete recorded session and exit")
    p.add_argument("--transfer", metavar="SOURCE_ID", help="Prepare context for another recorded conversation")
    p.add_argument("--to", metavar="TARGET_ID", help="Destination conversation for --transfer")
    p.add_argument("--transfer-id", help="Optional UUID for repeatable preparation")
    p.add_argument("--send-transfer", metavar="UUID", help="Send prepared context through the running app; starts a harness turn")
    p.add_argument("--transfer-status", metavar="UUID", help="Read a saved transfer receipt")
    p.add_argument("--version", action="version", version="Perch 0.2.0")
    return p


def main():
    if sys.argv[1:2] == ["--perch-pty-child"]:
        from .term import exec_pty_child

        exec_pty_child(sys.argv[2:])
    args = parser().parse_args()
    extend_path()
    cfg = settings.load()
    if args.transfer or args.send_transfer or args.transfer_status:
        from . import transfers
        from uuid import uuid4

        scanner = Scanner(config_dir=str(DATA_DIR))
        scanner.apply_settings(cfg)
        try:
            if args.transfer:
                result = transfers.prepare(scanner, args.transfer, args.to, args.transfer_id or str(uuid4()))
            elif args.send_transfer:
                result = transfers.send_to_app(scanner, args.send_transfer)
            else:
                result = transfers.read(scanner, args.transfer_status)
        except (OSError, ValueError, TypeError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return int(result.get("status") in ("failed", "uncertain"))
    if args.resolve_skill:
        from .skill_sharing import review

        try:
            result = review(args.resolve_skill, args.skill_source, args.revision, args.apply)
        except (OSError, ValueError, TypeError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return int(bool(result["report"]["skills"]["errors"] or result["report"]["skills"]["conflicts"]))
    if args.capabilities or args.link:
        from . import capabilities

        try:
            result = capabilities.inventory() if args.capabilities else capabilities.link(args.link, args.target, revision=args.revision, apply=args.apply)
        except (OSError, ValueError, TypeError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return int(result.get("status") == "blocked" or bool(result.get("errors")))
    if args.sync or args.dry_run or args.tools:
        from .sync import overview, sync_all

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
    if args.sessions or args.export_session:
        from .conversations import export_known_session

        scanner = Scanner(config_dir=str(DATA_DIR))
        scanner.apply_settings(cfg)
        try:
            result = (
                [{key: agent.get(key) for key in ("id", "harness", "title", "cwd")} for agent in scanner.scan()["agents"]]
                if args.sessions else export_known_session(scanner, args.export_session)
            )
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
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
            from .demo import DemoScanner

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
