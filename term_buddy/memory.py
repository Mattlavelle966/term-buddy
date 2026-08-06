from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .project import PRIORITY_NAMES, SENSITIVE_NAMES, _count_excluded_directories, _files


def memory_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state / "term-buddy" / "memory.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@dataclass(slots=True)
class IndexReport:
    root: str
    discovered: int
    indexed: int
    unchanged: int
    removed: int
    skipped_binary: int
    skipped_sensitive: int
    excluded_directories: int


class ProjectMemory:
    """A small, local, incremental source index. Model inference is never used here."""

    def __init__(self, path: Path | None = None):
        self.path = path or memory_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    root TEXT PRIMARY KEY, indexed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    root TEXT NOT NULL, path TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY (root, path)
                );
                CREATE INDEX IF NOT EXISTS files_root ON files(root);
                """
            )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def index(self, root_value: str) -> IndexReport:
        root = Path(root_value).resolve()
        paths = sorted(path for path in _files(root) if path.is_file() and not path.is_symlink())
        indexed = unchanged = skipped_binary = skipped_sensitive = 0
        seen: set[str] = set()
        with self._connect() as db:
            existing = {
                row[0]: (row[1], row[2])
                for row in db.execute("SELECT path, mtime_ns, size FROM files WHERE root = ?", (str(root),))
            }
            for path in paths:
                relative = str(path.relative_to(root))
                seen.add(relative)
                name = path.name.lower()
                if name in SENSITIVE_NAMES or name.startswith(".env.") or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
                    skipped_sensitive += 1
                    continue
                try:
                    stat = path.stat()
                    if existing.get(relative) == (stat.st_mtime_ns, stat.st_size):
                        unchanged += 1
                        continue
                    raw = path.read_bytes()
                except OSError:
                    continue
                if b"\0" in raw[:8192]:
                    skipped_binary += 1
                    continue
                text = raw.decode("utf-8", errors="replace")
                digest = hashlib.sha256(raw).hexdigest()
                db.execute(
                    "INSERT OR REPLACE INTO files(root,path,mtime_ns,size,digest,content) VALUES(?,?,?,?,?,?)",
                    (str(root), relative, stat.st_mtime_ns, stat.st_size, digest, text),
                )
                indexed += 1
            removed_paths = set(existing) - seen
            db.executemany("DELETE FROM files WHERE root = ? AND path = ?", ((str(root), p) for p in removed_paths))
            db.execute("INSERT OR REPLACE INTO projects(root,indexed_at) VALUES(?,?)", (str(root), time.time()))
        return IndexReport(
            root=str(root), discovered=len(paths), indexed=indexed, unchanged=unchanged,
            removed=len(removed_paths), skipped_binary=skipped_binary,
            skipped_sensitive=skipped_sensitive,
            excluded_directories=_count_excluded_directories(root),
        )

    def root_for(self, cwd_value: str) -> str:
        cwd = Path(cwd_value).resolve()
        with self._connect() as db:
            roots = [row[0] for row in db.execute("SELECT root FROM projects ORDER BY length(root) DESC")]
        for root in roots:
            try:
                cwd.relative_to(root)
                return root
            except ValueError:
                continue
        return ""

    def retrieve(self, root: str, question: str, max_chars: int = 48000) -> tuple[str, list[str]]:
        words = {
            word for word in re.findall(r"[a-zA-Z0-9_.-]{3,}", question.lower())
            if word not in {"what", "this", "that", "does", "should", "could", "would", "project", "about", "tell", "please"}
        }
        with self._connect() as db:
            rows = list(db.execute("SELECT path, content FROM files WHERE root = ?", (root,)))
        scored: list[tuple[int, str, str]] = []
        for path, content in rows:
            lower_path, lower_content = path.lower(), content.lower()
            name = Path(path).name.lower()
            score = 40 if name in PRIORITY_NAMES or name.startswith("readme") else 0
            for word in words:
                score += 60 if word in lower_path else min(12, lower_content.count(word))
            scored.append((score, path, content))
        scored.sort(key=lambda item: (item[0], -len(item[2])), reverse=True)
        parts = [f"PROJECT ROOT: {root}\nRETRIEVED FILES:"]
        sources: list[str] = []
        used = len(parts[0])
        for score, path, content in scored:
            if sources and score <= 0:
                break
            lower_content = content.lower()
            positions = [position for word in words if (position := lower_content.find(word)) >= 0]
            center = min(positions) if positions else 0
            start = max(0, center - 4000)
            excerpt = content[start:start + 12000]
            if start:
                excerpt = "[...earlier content omitted...]\n" + excerpt
            if start + 12000 < len(content):
                excerpt += "\n[...later content omitted...]"
            block = f"\n\n--- FILE: {path} ---\n{excerpt}"
            remaining = max_chars - used
            if remaining < 512:
                break
            parts.append(block[:remaining])
            sources.append(path)
            used += min(len(block), remaining)
        return "".join(parts), sources
