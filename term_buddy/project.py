from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json",
}
PRIORITY_NAMES = {
    "readme", "readme.md", "pyproject.toml", "package.json", "cargo.toml",
    "go.mod", "makefile", "dockerfile", "compose.yaml", "docker-compose.yml",
}


@dataclass(slots=True)
class ProjectSnapshot:
    root: str
    content: str
    discovered: int
    included: int
    skipped_binary: int
    skipped_sensitive: int
    truncated: bool


def _files(root: Path) -> list[Path]:
    if shutil.which("rg"):
        result = subprocess.run(
            ["rg", "--files", "-0"], cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30,
        )
        if result.returncode in {0, 1}:
            return [root / os.fsdecode(item) for item in result.stdout.split(b"\0") if item]
    found: list[Path] = []
    ignored = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored and not name.startswith(".")]
        found.extend(Path(directory) / name for name in filenames if not name.startswith("."))
    return found


def _priority(path: Path, root: Path) -> tuple[int, int, str]:
    relative = path.relative_to(root)
    name = path.name.lower()
    important = 0 if name in PRIORITY_NAMES or name.startswith("readme") else 1
    return important, len(relative.parts), str(relative)


def build_project_snapshot(root_value: str, max_chars: int) -> ProjectSnapshot:
    root = Path(root_value).resolve()
    paths = sorted((path for path in _files(root) if path.is_file() and not path.is_symlink()), key=lambda p: _priority(p, root))
    relative_paths = [str(path.relative_to(root)) for path in paths]
    tree = "PROJECT ROOT: " + str(root) + "\n\nFILES:\n" + "\n".join(relative_paths)
    if len(tree) > max_chars // 4:
        tree = tree[: max_chars // 4] + "\n[...file tree truncated...]"
    parts = [tree, "\n\nCONTENTS:"]
    used = sum(len(part) for part in parts)
    included = skipped_binary = skipped_sensitive = 0
    truncated = False

    for path in paths:
        relative = str(path.relative_to(root))
        lower_name = path.name.lower()
        if (
            lower_name in SENSITIVE_NAMES or lower_name.startswith(".env.")
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        ):
            skipped_sensitive += 1
            continue
        remaining = max_chars - used
        if remaining < 256:
            truncated = True
            break
        try:
            with path.open("rb") as handle:
                raw = handle.read(min(path.stat().st_size, remaining - 128) + 1)
        except (OSError, ValueError):
            continue
        if b"\0" in raw[:8192]:
            skipped_binary += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        was_cut = len(raw) > remaining - 128 or path.stat().st_size > len(raw)
        block = f"\n\n--- FILE: {relative} ---\n{text[: max(0, remaining - 128)]}"
        if was_cut:
            block += "\n[...file truncated to fit context...]"
        parts.append(block)
        used += len(block)
        included += 1
        if was_cut:
            truncated = True

    return ProjectSnapshot(
        root=str(root), content="".join(parts)[:max_chars], discovered=len(paths),
        included=included, skipped_binary=skipped_binary,
        skipped_sensitive=skipped_sensitive, truncated=truncated,
    )
