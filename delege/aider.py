"""Aider command construction for local, one-shot task execution."""
from __future__ import annotations

import shutil


def find_aider(name: str = "aider") -> str:
    """Resolve Aider from PATH when possible, preserving a configured path otherwise."""
    return shutil.which(name) or name


def aider_model(model: str | None) -> str | None:
    """Translate Delege's OpenCode-style Ollama model id to Aider's recommended Ollama driver."""
    if model and model.startswith("ollama/"):
        return "ollama_chat/" + model.split("/", 1)[1]
    return model


def aider_argv(aider: str, prompt: str, model: str | None = None) -> list[str]:
    """Run one non-interactive Aider task and leave commits to Delege."""
    args = [aider, "--yes", "--no-auto-commits", "--no-dirty-commits", "--no-suggest-shell-commands",
            "--no-auto-lint", "--no-auto-test", "--no-check-update", "--no-detect-urls", "--no-analytics"]
    if model:
        args += ["--model", aider_model(model)]
    return [*args, "--message", prompt]
