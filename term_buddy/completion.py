from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DIRECTORY_COMMANDS = {"cd", "pushd", "rmdir"}
SHELL_BUILTINS = {
    "alias", "bg", "bind", "break", "builtin", "cd", "command", "continue",
    "declare", "dirs", "disown", "echo", "enable", "eval", "exec", "exit",
    "export", "fc", "fg", "getopts", "hash", "help", "history", "jobs", "kill",
    "let", "local", "logout", "mapfile", "popd", "printf", "pushd", "pwd", "read",
    "readonly", "return", "set", "shift", "shopt", "source", "suspend", "test",
    "times", "trap", "type", "typeset", "ulimit", "umask", "unalias", "unset", "wait",
}


@dataclass(slots=True)
class Completion:
    suffix: str
    candidates: list[str]


def _humanize_path(value: str) -> str:
    stem = Path(value).stem
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem).replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", words).strip().lower() or "project"


def _git_changes(cwd: Path) -> list[tuple[str, str]]:
    """Read staged changes quickly; fall back to working-tree changes for a preview."""
    for arguments in (
        ["git", "diff", "--cached", "--name-status", "--no-renames"],
        ["git", "diff", "--name-status", "--no-renames"],
    ):
        try:
            completed = subprocess.run(
                arguments, cwd=cwd, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=0.2,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        changes = []
        for line in completed.stdout.splitlines():
            status, separator, path = line.partition("\t")
            if separator and path:
                changes.append((status[:1], path))
        if changes:
            return changes
    return []


def _commit_message(cwd: Path) -> str:
    changes = _git_changes(cwd)
    if not changes:
        return ""
    statuses = {status for status, _path in changes}
    verb = "Add" if statuses == {"A"} else "Remove" if statuses == {"D"} else "Update"
    if len(changes) == 1:
        return f"{verb} {_humanize_path(changes[0][1])}"
    parents = {str(Path(path).parent) for _status, path in changes}
    if len(parents) == 1 and "." not in parents:
        subject = _humanize_path(next(iter(parents)))
    else:
        subject = "project files"
    return f"{verb} {subject}"


def _git_commit_completion(before: str, cwd: Path) -> Completion | None:
    match = re.fullmatch(r"\s*git\s+commit\s+-m\s+([\"']?)([^\"']*)", before)
    if not match:
        return None
    quote, typed = match.groups()
    message = _commit_message(cwd)
    if not message:
        return Completion("", [])
    if quote:
        if not message.lower().startswith(typed.lower()):
            return Completion("", [])
        suffix = message[len(typed):] + quote
    elif typed:
        return Completion("", [])
    else:
        suffix = f'"{message}"'
    return Completion(suffix, [f'git commit -m "{message}"'])


def _commands(prefix: str) -> list[str]:
    matches = {command for command in SHELL_BUILTINS if command.startswith(prefix)}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in Path(directory).iterdir():
                if entry.name.startswith(prefix) and entry.is_file() and os.access(entry, os.X_OK):
                    matches.add(entry.name)
        except OSError:
            continue
    return sorted(matches)


def _paths(token: str, cwd: Path, *, directories_only: bool) -> list[str]:
    expanded = os.path.expanduser(token)
    typed_parent, stem = os.path.split(expanded)
    scan_root = Path(typed_parent) if typed_parent else cwd
    if not scan_root.is_absolute():
        scan_root = cwd / scan_root
    display_parent = token[: len(token) - len(os.path.basename(token))]
    matches: list[str] = []
    try:
        entries = sorted(scan_root.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith(stem) or (entry.name.startswith(".") and not stem.startswith(".")):
            continue
        if directories_only and not entry.is_dir():
            continue
        candidate = display_parent + entry.name
        if entry.is_dir():
            candidate += "/"
        matches.append(candidate.replace(" ", "\\ "))
    return matches


def complete_buffer(buffer: str, cwd_value: str, point: int | None = None) -> Completion:
    """Complete the token at the cursor using only live PATH/filesystem state."""
    point = len(buffer) if point is None else max(0, min(point, len(buffer)))
    before = buffer[:point]
    cwd = Path(cwd_value).resolve()
    git_commit = _git_commit_completion(before, cwd)
    if git_commit is not None:
        return git_commit
    match = re.search(r"(?:^|\s)((?:\\.|[^\s])*)$", before)
    token = match.group(1) if match else ""
    token_start = match.start(1) if match else point
    prefix_text = before[:token_start].strip()
    first_word = not prefix_text
    command = before.strip().split(maxsplit=1)[0] if before.strip() else ""
    if first_word and "/" not in token:
        candidates = _commands(token)
    else:
        candidates = _paths(
            token, cwd,
            directories_only=command in DIRECTORY_COMMANDS,
        )
    if not candidates:
        return Completion("", [])
    common = os.path.commonprefix(candidates)
    suffix = common[len(token):] if common.startswith(token) else ""
    return Completion(suffix, candidates[:40])
