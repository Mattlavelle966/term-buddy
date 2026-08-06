from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ToolDenied(RuntimeError):
    pass


READ_ONLY_COMMANDS = {
    "cat", "df", "du", "free", "git", "head", "id", "journalctl", "ls", "lsof",
    "man", "ps", "pwd", "rg", "stat", "tail", "uname", "uptime", "wc", "which",
}
DENIED_TOKENS = {">", ">>", "<", "<<", "|", "||", "&&", ";", "&", "`"}
GIT_READ_ONLY = {"status", "diff", "log", "show", "branch", "remote", "rev-parse", "ls-files"}


@dataclass(slots=True)
class ToolResult:
    command: str
    output: str
    returncode: int


def _validate_read_only(argv: list[str]) -> None:
    if not argv or Path(argv[0]).name not in READ_ONLY_COMMANDS:
        raise ToolDenied("command is not on the read-only allowlist")
    if any(token in DENIED_TOKENS or token.startswith(">") for token in argv):
        raise ToolDenied("shell operators and redirects are disabled in read-only mode")
    command = Path(argv[0]).name
    if command == "git":
        subcommand = next((arg for arg in argv[1:] if not arg.startswith("-")), "")
        if subcommand not in GIT_READ_ONLY:
            raise ToolDenied("that git subcommand is not read-only")
    if command == "rg" and any(arg == "--pre" or arg.startswith("--pre=") for arg in argv):
        raise ToolDenied("rg preprocessors are disabled because they execute commands")
    if command == "man" and any(arg in {"-P", "--pager"} or arg.startswith("--pager=") for arg in argv):
        raise ToolDenied("custom man pagers are disabled because they execute commands")
    if command == "git" and any("output" in arg or arg in {"-c", "--config-env"} for arg in argv):
        raise ToolDenied("git output and configuration overrides are disabled")


def run_command(command: str, *, cwd: str, yolo: bool, timeout: int = 20) -> ToolResult:
    argv = shlex.split(command)
    if not yolo:
        _validate_read_only(argv)
    safe_cwd = str(Path(cwd).resolve()) if Path(cwd).is_dir() else os.getcwd()
    try:
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["PAGER"] = "cat"
        environment["GIT_PAGER"] = "cat"
        environment["MANPAGER"] = "cat"
        completed = subprocess.run(
            argv, cwd=safe_cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, env=environment,
        )
        output = completed.stdout[-16000:]
        return ToolResult(command=command, output=output, returncode=completed.returncode)
    except FileNotFoundError as exc:
        return ToolResult(command=command, output=str(exc), returncode=127)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n[term-buddy: command timed out]"
        return ToolResult(command=command, output=output[-16000:], returncode=124)
