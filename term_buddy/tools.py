from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ToolDenied(RuntimeError):
    pass


READ_ONLY_COMMANDS = {
    "cat", "df", "dmesg", "du", "free", "git", "head", "id", "journalctl", "ls",
    "lsmod", "lsof", "lspci", "man", "nvidia-smi", "ps", "pwd", "rg", "ss", "stat",
    "systemctl", "tail", "uname", "uptime", "wc", "which",
}
DENIED_TOKENS = {">", ">>", "<", "<<", "|", "||", "&&", ";", "&", "`"}
GIT_READ_ONLY = {
    "status", "diff", "diff-tree", "diff-index", "diff-files", "log", "show",
    "shortlog", "blame", "branch", "remote", "rev-parse", "ls-files", "ls-tree",
    "name-rev", "describe",
}


@dataclass(slots=True)
class ToolResult:
    command: str
    output: str
    returncode: int


def _validate_read_only(argv: list[str]) -> None:
    if not argv or Path(argv[0]).name not in READ_ONLY_COMMANDS:
        raise ToolDenied("command is not on the read-only allowlist")
    if any(
        token in DENIED_TOKENS or re.match(r"^\d*[<>]", token)
        or any(operator in token for operator in ("&&", "||", ";", "`"))
        for token in argv
    ):
        raise ToolDenied("shell operators and redirects are disabled in read-only mode")
    command = Path(argv[0]).name
    if command in {"cat", "head", "tail", "wc", "stat", "rg"} and len(argv) < 2:
        raise ToolDenied(f"{command} requires an explicit file or search argument")
    if command == "git":
        subcommand = ""
        index = 1
        options_with_values = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
        while index < len(argv):
            argument = argv[index]
            if argument in options_with_values:
                index += 2
                continue
            if argument.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--config-env=")):
                index += 1
                continue
            if argument.startswith("-"):
                index += 1
                continue
            subcommand = argument
            break
        if subcommand not in GIT_READ_ONLY:
            allowed = ", ".join(sorted(GIT_READ_ONLY))
            raise ToolDenied(f"git subcommand {subcommand or '<missing>'!r} is not allowed; use one of: {allowed}")
    if command == "rg" and any(arg == "--pre" or arg.startswith("--pre=") for arg in argv):
        raise ToolDenied("rg preprocessors are disabled because they execute commands")
    if command == "man" and any(arg in {"-P", "--pager"} or arg.startswith("--pager=") for arg in argv):
        raise ToolDenied("custom man pagers are disabled because they execute commands")
    if command == "git" and any("output" in arg or arg in {"-c", "--config-env"} for arg in argv):
        raise ToolDenied("git output and configuration overrides are disabled")
    if command == "ss" and any(arg == "-K" or "K" in arg.lstrip("-") for arg in argv if arg.startswith("-")):
        raise ToolDenied("ss socket-kill mode is disabled")
    if command == "systemctl":
        subcommand = next((arg for arg in argv[1:] if not arg.startswith("-")), "status")
        allowed = {"status", "show", "list-units", "list-unit-files", "is-active", "is-enabled", "is-failed"}
        if subcommand not in allowed:
            raise ToolDenied("that systemctl operation is not read-only")
    if command == "dmesg" and any(arg in {"-c", "-C", "--clear", "--read-clear"} for arg in argv):
        raise ToolDenied("clearing the kernel log is disabled")
    if command == "journalctl" and any(
        arg == "--rotate" or arg.startswith("--vacuum") or arg.startswith("--relinquish")
        for arg in argv
    ):
        raise ToolDenied("journal maintenance operations are disabled")
    if command == "nvidia-smi":
        allowed_exact = {"-L", "--list-gpus", "-q", "--query", "-i", "--id", "-l", "--loop"}
        allowed_prefixes = ("--query-gpu=", "--query-compute-apps=", "--format=", "--id=", "--loop=")
        for arg in argv[1:]:
            if arg.startswith("-") and arg not in allowed_exact and not arg.startswith(allowed_prefixes):
                raise ToolDenied("that nvidia-smi option is not on the read-only allowlist")


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
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            timeout=timeout, env=environment,
        )
        output = completed.stdout[-16000:]
        return ToolResult(command=command, output=output, returncode=completed.returncode)
    except FileNotFoundError as exc:
        return ToolResult(command=command, output=str(exc), returncode=127)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n[term-buddy: command timed out]"
        return ToolResult(command=command, output=output[-16000:], returncode=124)
