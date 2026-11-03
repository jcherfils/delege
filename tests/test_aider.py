"""Aider's one-shot command adapter and local Ollama model mapping."""
from __future__ import annotations

from delege.aider import aider_argv, aider_model


def test_aider_uses_ollama_chat_driver():
    assert aider_model("ollama/qwen3-coder:30b") == "ollama_chat/qwen3-coder:30b"
    assert aider_model("ollama_chat/qwen3-coder:30b") == "ollama_chat/qwen3-coder:30b"
    assert aider_model(None) is None


def test_aider_command_is_one_shot_and_leaves_git_to_delege():
    argv = aider_argv("/venv/bin/aider", "Fix the parser", "ollama/qwen3-coder:30b")
    assert argv == [
        "/venv/bin/aider", "--yes", "--no-auto-commits", "--no-dirty-commits",
        "--no-suggest-shell-commands", "--no-auto-lint", "--no-auto-test", "--no-check-update",
        "--no-detect-urls", "--no-analytics", "--model", "ollama_chat/qwen3-coder:30b", "--message",
        "Fix the parser",
    ]
