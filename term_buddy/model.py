from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config


class ModelError(RuntimeError):
    pass


@dataclass(slots=True)
class ModelClient:
    config: Config

    def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        url = self.config.endpoint.rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                data: dict[str, Any] = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ModelError(f"model request failed: {exc}") from exc
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model returned an unexpected response") from exc

    def observe(self, transcript: str) -> str:
        prompt = (
            "Review this latest terminal activity. Reply with one brief, useful observation "
            "or next action. If it succeeded and there is nothing meaningful to add, reply "
            "with exactly SILENT. If you need to inspect the machine, reply with only one "
            "tag in the form <tool>read-only command</tool>.\n\n" + transcript
        )
        return self.complete([
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": prompt},
        ])

    def ask(self, question: str, context: str) -> str:
        return self.complete([
            {"role": "system", "content": self.config.system_prompt + (
                " When local inspection is necessary, request one command by replying only "
                "with <tool>command</tool>. Tool availability is enforced by the host."
            )},
            {"role": "user", "content": f"Terminal context:\n{context}\n\nQuestion:\n{question}"},
        ])

    def suggest(self, command_buffer: str, context: str) -> str:
        answer = self.complete([
            {"role": "system", "content": (
                "Complete a shell command. Return only the exact suffix to append to the "
                "current buffer, with no markdown, explanation, newline, or command execution."
            )},
            {"role": "user", "content": f"Recent context:\n{context}\n\nCurrent buffer:\n{command_buffer}"},
        ], temperature=0.0)
        return answer.splitlines()[0] if answer else ""
