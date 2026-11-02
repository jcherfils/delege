"""OpenCode 2: version detection, the headless ``opencode run`` command line, its JSON events, and the per-task
``opencode.jsonc`` that allows exactly the providers a task may use."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .util import read_jsonc

OPENCODE_CONFIG = "opencode.jsonc"                  # OpenCode 2's project configuration
OPENCODE_MAJOR = 2                                  # the configuration and the agents are written for OpenCode 2
OPENCODE_INSTALL = "curl -fsSL https://opencode.ai/v2/install | bash   (from OpenCode 1: opencode upgrade --major)"
MIN_CONTEXT = 65536                                 # what OpenCode and Ollama ask for agents


def parse_version(text: str) -> tuple[int, ...] | None:
    """``opencode --version`` → (major, minor, patch): OpenCode 2 prints ``opencode v2.1.4``, OpenCode 1 ``1.19.4``."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def version_problem(text: str, where: str = "this machine") -> str | None:
    """None when the output of ``opencode --version`` is OpenCode 2, else what is wrong and the fix."""
    v = parse_version(text)
    if v is None:
        return f"`opencode --version` on {where} gave no version ({(text or '').strip()[:80]!r}) — install: {OPENCODE_INSTALL}"
    if v[0] != OPENCODE_MAJOR:
        return (f"OpenCode {'.'.join(map(str, v))} on {where}: delege's {OPENCODE_CONFIG} and .opencode/agents "
                f"are written for OpenCode {OPENCODE_MAJOR} — install it: {OPENCODE_INSTALL}")
    return None


def opencode_argv(opencode: str, agent: str, prompt: str, model: str | None = None,
                  json_output: bool = False) -> list[str]:
    """The headless run: ``--standalone`` gives each task its own private OpenCode server, started in the task's
    worktree, so it reads that worktree's opencode.jsonc and agents (the shared background service keeps the
    config it read when it started). No ``--auto``: a permission left at ``ask`` is rejected in a headless run.
    ``json_output`` (cloud models): ``--format json``, one event per line, the tokens in ``step_finish``."""
    return [opencode, "run", "--standalone", "--agent", agent, *(["--model", model] if model else []),
            *(["--format", "json"] if json_output else []), prompt]


def parse_json_output(text: str) -> dict:
    """``opencode run --format json`` → ``{"steps", "input", "output", "text", "error", "status"}``: the tokens of
    every ``step_finish`` (``part.tokens``: input, output, reasoning, cache), the ``text`` parts joined (the agent's
    report), the first ``error`` event's message and HTTP status. Lines that are not JSON are kept as text."""
    out = {"steps": 0, "input": 0, "output": 0, "text": "", "error": "", "status": None}
    texts = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            if line:
                texts.append(line)
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            texts.append(line)
            continue
        part = ev.get("part") or {}
        kind = ev.get("type")
        if kind == "step_finish":
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            out["steps"] += 1
            out["input"] += int(tok.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
            out["output"] += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
        elif kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "error" and not out["error"]:
            err = ev.get("error") or {}
            data = err.get("data") if isinstance(err, dict) else {}
            data = data if isinstance(data, dict) else {}
            out["error"] = str(ev.get("message") or data.get("message") or err.get("name") or "error")[:600]
            st = data.get("statusCode") or data.get("status")
            out["status"] = int(st) if isinstance(st, int) or (isinstance(st, str) and st.isdigit()) else None
    out["text"] = "\n".join(texts)
    return out


def cloud_policies(policies: list, providers) -> list:
    """The repository's ``policies`` with the ``provider.use`` rules replaced: deny every provider, then allow
    exactly ``providers`` (``ollama`` and the free providers in use) — the task's worktree config."""
    keep = [p for p in policies if p.get("action") != "provider.use"]
    allow = [{"action": "provider.use", "resource": p, "effect": "allow"} for p in sorted(set(providers))]
    return [{"action": "provider.use", "resource": "*", "effect": "deny"}, *allow, *keep]


def write_task_config(worktree: Path, providers, src: Path | None = None) -> Path:
    """Writes the worktree's ``opencode.jsonc`` from the repository's (``src``; default the worktree's own copy),
    with the providers allowed for this task. The repository's copy keeps ``ollama`` only; delege restores the file
    before it looks at the diff."""
    out = Path(worktree) / OPENCODE_CONFIG
    cfg = read_jsonc(src or out)
    cfg["policies"] = cloud_policies(cfg.get("policies") or [], providers)
    cfg.pop("$schema", None)
    out.write_text("// written by delege for this task: the providers it may use (never committed)\n"
                   + json.dumps(cfg, indent=2) + "\n")
    return out


def find_opencode(name: str = "opencode") -> str:
    """``opencode`` on PATH, else where OpenCode 2's installer puts it (``~/.opencode/bin``; it adds that folder to
    PATH in ``~/.bashrc`` only, which a script or an ssh command does not read)."""
    if name != "opencode" or shutil.which(name):
        return name
    alt = Path.home() / ".opencode" / "bin" / "opencode"
    return str(alt) if alt.exists() else name


def opencode_models(opencode: str, cwd: Path | str = ".") -> set[str] | None:
    """The model ids ``opencode models`` lists (``provider/model`` lines), or None when the command fails."""
    try:
        r = subprocess.run([opencode, "models"], cwd=str(cwd), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    return {line.strip() for line in r.stdout.splitlines() if "/" in line and " " not in line.strip()}
