from __future__ import annotations

import fcntl
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import Config


class WebError(RuntimeError):
    pass


def _config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "term-buddy"


def _docker(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *command], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WebError(f"could not manage the Term Buddy SearXNG container: {exc}") from exc


def _write_searxng_settings(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "settings.yml"
    if not path.exists():
        path.write_text(
            "use_default_settings: true\n"
            "server:\n"
            f"  secret_key: \"{secrets.token_hex(32)}\"\n"
            "  bind_address: \"0.0.0.0\"\n"
            "  port: 8080\n"
            "  limiter: false\n"
            "search:\n"
            "  formats:\n"
            "    - html\n"
            "    - json\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    return path


def _local_port(url: str) -> int:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise WebError("managed SearXNG must use a loopback http:// URL")
    return parsed.port or 80


def ensure_searxng(config: Config) -> bool:
    """Ensure the one dedicated container exists. Return True when it was started."""
    if not config.web or not config.searxng_managed:
        return False
    if not shutil.which("docker"):
        raise WebError("web search is enabled but Docker is not installed")
    port = _local_port(config.searxng_url)
    root = _config_root() / "searxng"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / "container.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inspected = _docker([
            "container", "inspect", "--format", "{{.State.Running}}", config.searxng_container,
        ], timeout=10)
        if inspected.returncode == 0 and inspected.stdout.strip() == "true":
            _wait_ready(config.searxng_url)
            return False
        if inspected.returncode == 0:
            started = _docker(["start", config.searxng_container], timeout=60)
            if started.returncode:
                raise WebError(started.stderr.strip() or "failed to start SearXNG")
        else:
            config_directory = root / "config"
            data_directory = root / "data"
            _write_searxng_settings(config_directory)
            data_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            started = _docker([
                "run", "-d", "--name", config.searxng_container,
                "--restart", "unless-stopped",
                "-p", f"127.0.0.1:{port}:8080",
                "-v", f"{config_directory}:/etc/searxng",
                "-v", f"{data_directory}:/var/cache/searxng",
                config.searxng_image,
            ])
            if started.returncode:
                # A concurrent process may have won the fixed container name.
                retry = _docker([
                    "container", "inspect", "--format", "{{.State.Running}}",
                    config.searxng_container,
                ], timeout=10)
                if retry.returncode or retry.stdout.strip() != "true":
                    raise WebError(started.stderr.strip() or "failed to create SearXNG")
        _wait_ready(config.searxng_url)
        return True


def _wait_ready(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(base_url.rstrip("/") + "/", headers={"User-Agent": "term-buddy/1"})
            with urllib.request.urlopen(request, timeout=2) as response:
                if 200 <= getattr(response, "status", 200) < 500:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise WebError(f"SearXNG container started but did not become ready: {last_error}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n\n", value)
        return value.strip()


def _validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WebError("web fetch only accepts public http(s) URLs without credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise WebError(f"could not resolve web URL: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise WebError("web fetch blocked a private, local, or reserved network address")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WebClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, limit: int = 5) -> str:
        query = " ".join(query.split())[:500]
        if not query:
            raise WebError("search query is empty")
        url = self.base_url + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "term-buddy/1"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(1_000_001)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebError(f"SearXNG search failed: {exc}") from exc
        if len(raw) > 1_000_000:
            raise WebError("SearXNG response exceeded 1 MB")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WebError("SearXNG returned invalid JSON; ensure the json format is enabled") from exc
        lines = [f"Search results for: {query}"]
        for index, result in enumerate(payload.get("results", [])[:limit], start=1):
            title = " ".join(str(result.get("title", "Untitled")).split())
            target = str(result.get("url", ""))
            snippet = " ".join(str(result.get("content", "")).split())[:600]
            lines.append(f"{index}. {title}\nURL: {target}\n{snippet}")
        if len(lines) == 1:
            lines.append("No results.")
        return "\n\n".join(lines)

    def fetch(self, url: str) -> str:
        _validate_public_url(url)
        opener = urllib.request.build_opener(_SafeRedirect())
        request = urllib.request.Request(url, headers={
            "Accept": "text/html,text/plain,application/json", "User-Agent": "term-buddy/1",
        })
        try:
            with opener.open(request, timeout=15) as response:
                content_type = response.headers.get_content_type()
                raw = response.read(1_000_001)
                final_url = response.geturl()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebError(f"web fetch failed: {exc}") from exc
        if len(raw) > 1_000_000:
            raise WebError("web page exceeded the 1 MB fetch limit")
        if content_type not in {"text/html", "text/plain", "application/json"}:
            raise WebError(f"unsupported web content type: {content_type}")
        decoded = raw.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser = _TextExtractor()
            parser.feed(decoded)
            decoded = parser.text()
        return f"Fetched: {final_url}\n\n{decoded[:16000]}"
