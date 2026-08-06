from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config


class SessionError(RuntimeError):
    pass


def runtime_root() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/term-buddy-{os.getuid()}"))
    path = base / "term-buddy"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@dataclass(slots=True)
class Session:
    config: Config
    name: str
    yolo: bool = False
    proactive: bool = True

    @property
    def directory(self) -> Path:
        return runtime_root() / self.name

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["tmux", *args], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown tmux error"
            raise SessionError(f"tmux {' '.join(args[:2])} failed: {detail}")
        return completed

    def exists(self) -> bool:
        if not shutil.which("tmux"):
            return False
        return self._tmux("has-session", "-t", self.name, check=False).returncode == 0

    def create(self) -> None:
        if not shutil.which("tmux"):
            raise SessionError("tmux is required but was not found (Debian/Ubuntu: sudo apt install tmux)")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.events_path.write_text("", encoding="utf-8")
        transcript = self.directory / "transcript.log"
        transcript.write_bytes(b"")
        os.chmod(self.events_path, 0o600)
        os.chmod(transcript, 0o600)

        package_root = Path(__file__).resolve().parent.parent
        launcher = package_root / "bin" / "term-buddy"
        if not launcher.exists():
            launcher = Path(shutil.which("term-buddy") or "term-buddy")
        shell_integration = Path(__file__).resolve().parent / "shell" / "term-buddy.bash"
        bootstrap = self.directory / "bashrc"
        bootstrap.write_text(
            "[[ -f ~/.bashrc ]] && source ~/.bashrc\n"
            f"source {shlex.quote(str(shell_integration))}\n",
            encoding="utf-8",
        )
        env = {
            "TERM_BUDDY_EVENTS": str(self.events_path),
            "TERM_BUDDY_SESSION": self.name,
            "TERM_BUDDY_LAUNCHER": str(launcher),
            "TERM_BUDDY_YOLO": "1" if self.yolo else "0",
            "TERM_BUDDY_PROACTIVE": "1" if self.proactive else "0",
            "TERM_BUDDY_AUTOCOMPLETE": "1" if self.config.autocomplete else "0",
            "TERM_BUDDY_ENDPOINT": self.config.endpoint,
            "TERM_BUDDY_MODEL": self.config.model,
        }
        for key, value in env.items():
            self._tmux("set-environment", "-g", key, value, check=False)

        environment = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        shell_command = (
            f"exec env {environment} {shlex.quote(self.config.shell)} "
            f"--rcfile {shlex.quote(str(bootstrap))} -i"
        )
        buddy_command = (
            f"exec env {environment} {shlex.quote(str(launcher))} _buddy "
            f"--events {shlex.quote(str(self.events_path))} "
            f"--session {shlex.quote(self.name)}" + (" --yolo" if self.yolo else "")
            + ("" if self.proactive else " --no-watch")
        )
        created = self._tmux(
            "new-session", "-d", "-P", "-F", "#{window_id} #{pane_id}",
            "-s", self.name, "-n", "buddy", shell_command, check=False,
        )
        if created.returncode:
            raise SessionError(created.stderr.strip() or "failed to create tmux session")
        try:
            _, left_pane = created.stdout.strip().split(maxsplit=1)
            terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
            buddy_width = min(self.config.buddy_width, max(20, terminal_width // 2))
            self._tmux("set-option", "-t", self.name, "remain-on-exit", "on")
            self._tmux("split-window", "-h", "-l", str(buddy_width), "-t", left_pane, buddy_command)
            capture_command = f"exec {shlex.quote(str(launcher))} _capture --output {shlex.quote(str(transcript))}"
            self._tmux("pipe-pane", "-o", "-t", left_pane, capture_command)
            self._tmux("select-pane", "-t", left_pane)
            self._tmux("set-option", "-t", self.name, "status-left", " #[bold]term-buddy #[default] ")
            self._tmux("set-option", "-t", self.name, "status-right", "#{pane_current_path} ")
        except (SessionError, ValueError) as exc:
            self._tmux("kill-session", "-t", self.name, check=False)
            raise SessionError(f"could not lay out Buddy panes: {exc}") from exc

    def attach(self) -> int:
        command = ["tmux", "attach-session", "-t", self.name]
        if os.environ.get("TMUX"):
            command = ["tmux", "switch-client", "-t", self.name]
        return subprocess.call(command)

    def stop(self) -> None:
        if self.exists():
            self._tmux("kill-session", "-t", self.name)


def capture_pane(session_name: str, pane: str = "", lines: int = 160) -> str:
    target = pane or session_name
    completed = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return completed.stdout
