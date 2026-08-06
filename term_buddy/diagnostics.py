from __future__ import annotations

import re


def plan_diagnostics(question: str) -> list[str]:
    """Map common intents to safe evidence commands without relying on model tool syntax."""
    text = question.lower()
    if re.search(r"\b(last|latest|recent)\b.*\bcommits?\b|\bcommits?\b.*\b(last|latest|recent)\b", text):
        count = re.search(r"\b([1-5])\b", text)
        return [f"git log --stat --oneline -{count.group(1) if count else '1'}"]
    if re.search(r"\b(uncommitted|git diff|working tree|changed files)\b", text):
        return ["git status --short", "git diff --stat", "git diff"]
    if re.search(r"\b(gpu|gpus|graphics card)\b", text):
        return ["nvidia-smi -L", "lspci"]
    if re.search(r"\b(listening|open) ports?\b|\bwhat.*ports?\b", text):
        return ["ss -lntp"]
    if re.search(r"\b(disk space|filesystem usage|storage usage)\b", text):
        return ["df -h"]
    if re.search(r"\b(memory usage|how much (ram|memory))\b", text):
        return ["free -h"]
    return []
