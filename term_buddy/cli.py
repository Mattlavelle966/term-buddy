from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__
from .completion import complete_buffer
from .config import Config
from .events import append_event, read_events
from .input_adapter import run_input_adapter
from .model import ModelClient, ModelError
from .session import Session, SessionError, capture_pane
from .tui import BuddyUI
from .web import WebError, ensure_searxng


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="term-buddy", description="Local-AI tmux debugging companion")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--config", type=Path, help="configuration JSON path")
    root.add_argument("--endpoint", help="OpenAI-compatible API base URL")
    root.add_argument("--model", help="model name")
    root.add_argument("--session", default=None, help="tmux session name")
    root.add_argument("--yolo", action="store_true", help="allow Buddy /run commands to write")
    root.add_argument("--no-watch", action="store_true", help="disable proactive observations")
    sub = root.add_subparsers(dest="action")
    sub.add_parser("attach", help="attach to the running session")
    sub.add_parser("stop", help="stop the running session")
    sub.add_parser("config", help="create or print configuration")

    emit = sub.add_parser("emit", help=argparse.SUPPRESS)
    emit.add_argument("kind")
    emit.add_argument("--events", type=Path, required=True)
    emit.add_argument("--session", required=True)
    emit.add_argument("--pane", default="")
    emit.add_argument("--status", type=int, default=0)
    emit.add_argument("--cwd", default="")
    emit.add_argument("--command", default="")
    emit.add_argument("--message", default="")

    suggest = sub.add_parser("suggest", help=argparse.SUPPRESS)
    suggest.add_argument("--events", type=Path, required=True)
    suggest.add_argument("--buffer", required=True)

    complete = sub.add_parser("_complete", help=argparse.SUPPRESS)
    complete.add_argument("--buffer", required=True)
    complete.add_argument("--point", type=int)
    complete.add_argument("--cwd", required=True)

    shell_adapter = sub.add_parser("_shell", help=argparse.SUPPRESS)
    shell_adapter.add_argument("--events", type=Path, required=True)
    shell_adapter.add_argument("command", nargs=argparse.REMAINDER)

    buddy = sub.add_parser("_buddy", help=argparse.SUPPRESS)
    buddy.add_argument("--events", type=Path, required=True)
    buddy.add_argument("--session", required=True)
    buddy.add_argument("--yolo", action="store_true")
    buddy.add_argument("--no-watch", action="store_true")
    capture = sub.add_parser("_capture", help=argparse.SUPPRESS)
    capture.add_argument("--output", type=Path, required=True)
    return root


def load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    if getattr(args, "endpoint", None):
        config.endpoint = args.endpoint
    if getattr(args, "model", None):
        config.model = args.model
    return config


def event_context(path: Path, limit: int) -> str:
    events, _ = read_events(path)
    chunks = []
    for event in events[-12:]:
        if event.get("kind") == "command_finished":
            chunks.append(f"$ {event.get('command')}\nexit={event.get('status')}\n{event.get('output', '')}")
    return "\n\n".join(chunks)[-limit:]


def capture_stream(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("ab", buffering=0) as handle:
        os.chmod(path, 0o600)
        while chunk := sys.stdin.buffer.read(65536):
            handle.write(chunk)
    return 0


def transcript_tail(events_path: Path, limit: int) -> str:
    path = events_path.parent / "transcript.log"
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - limit * 2 - 4096))
        content = handle.read().decode("utf-8", errors="replace")
    ghost = re.compile(
        r"\x1b\]777;term-buddy-ghost-start\x07.*?"
        r"\x1b\]777;term-buddy-ghost-end\x07",
        flags=re.DOTALL,
    )
    content = ghost.sub("", content)
    ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
    return ansi.sub("", content)[-limit:]


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action == "_complete":
        result = complete_buffer(args.buffer, args.cwd, args.point)
        print(result.suffix)
        for candidate in result.candidates:
            print(candidate)
        return 0
    if args.action == "_shell":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return run_input_adapter(command, args.events)
    config = load_config(args)

    if args.action == "emit":
        payload = {"status": args.status, "cwd": args.cwd, "command": args.command, "message": args.message}
        if args.kind == "command_finished":
            payload["output"] = transcript_tail(args.events, config.max_output_chars)
            if not payload["output"]:
                payload["output"] = capture_pane(args.session, args.pane)[-config.max_output_chars:]
        append_event(args.events, args.kind, payload)
        return 0
    if args.action == "suggest":
        if not args.buffer.strip():
            return 0
        try:
            suffix = ModelClient(config).suggest(args.buffer, event_context(args.events, config.max_output_chars))
        except ModelError:
            return 1
        sys.stdout.write(suffix)
        return 0
    if args.action == "_buddy":
        return BuddyUI(
            config, args.events, args.session, yolo=args.yolo,
            proactive=config.proactive and not args.no_watch,
        ).run()
    if args.action == "_capture":
        return capture_stream(args.output)
    if args.action == "config":
        path = args.config
        if path and path.exists():
            print(path.read_text(encoding="utf-8"), end="")
        else:
            saved = config.save(path)
            print(f"Configuration written to {saved}")
        return 0

    name = args.session or config.session_name
    session = Session(
        config, name, yolo=args.yolo,
        proactive=config.proactive and not args.no_watch,
    )
    try:
        if args.action == "stop":
            session.stop()
            return 0
        if config.web and config.searxng_managed:
            print("term-buddy: ensuring the dedicated SearXNG container is running...")
            ensure_searxng(config)
        if not session.exists():
            if args.action == "attach":
                print(f"term-buddy: session {name!r} does not exist", file=sys.stderr)
                return 1
            session.create()
        return session.attach()
    except (SessionError, WebError) as exc:
        print(f"term-buddy: {exc}", file=sys.stderr)
        return 1
