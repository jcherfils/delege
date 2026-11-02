"""The per-project configuration: ``delege.toml`` at the repository root (or ``[tool.delege]`` in
``pyproject.toml``) — the GitHub repository, the base branch, the rules given to every agent, the files no agent may
add, and the agents (name → editable areas, proof commands, cost, media flag…). The agents' OpenCode side lives in
``.opencode/agents/<name>.md`` (``delege init`` writes them, with permissions generated from the areas).

Example (``delege init`` writes a commented one)::

    [project]
    name = "myproject"
    repo = "owner/myproject"           # GitHub owner/name: the issues labelled `delegate`
    base = "main"
    rules = '''
    - Everything you write is in English.
    '''

    [forbidden]
    suffixes = [".pt", ".onnx", ".mp4"]  # never committed nor pushed
    media_suffixes = [".png", ".jpg"]    # only agents with media = true, in their area; never pushed by delege
    dir_names = ["videos"]               # a folder of that name anywhere: never read by agents, never committed
    file_names = ["result.json"]
    paths = ["data/"]                    # repository-relative folders or globs

    [agents.tests]
    description = "Adds pytest tests for one module"
    areas = ["tests/*"]
    proof = ["{python} -m pytest -q -p no:cacheprovider {changed:tests/*test_*.py}"]
    shell = ["python -m pytest *"]       # commands the agent may run (OpenCode patterns), besides read-only ones

Proof placeholders: ``{python}`` (the host's Python), ``{delege}`` (``{python} -m delege``), ``{issue}`` (the
issue number: the command is skipped without one), ``{changed}`` (the changed files) and ``{changed:GLOB|GLOB}``
(those matching: the command is skipped when none does; ``{changed:GLOB!}`` makes it a failure instead — a fix
without a test).
"""
from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CONFIG_NAME = "delege.toml"
BRANCH_PREFIX = "delegate/"
LABEL = "delegate"
MAX_FILE_BYTES = 2_000_000          # a pushed file larger than this is refused

MODEL_SUFFIXES = (".pt", ".pth", ".ckpt", ".onnx", ".safetensors", ".npz", ".npy", ".bin", ".gguf", ".engine",
                  ".tflite", ".pkl", ".pickle", ".h5", ".mlmodel", ".pb")
VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".wav", ".mp3")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".exr")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Agent:
    name: str
    areas: tuple[str, ...]            # files it may change (globs, ``*`` matches ``/`` too)
    proof: tuple[str, ...] = ()       # commands run by delege in the worktree after the agent (placeholders above)
    cost: float = 1.0                 # relative time, for the plan
    media: bool = False               # may change images in its area (never pushed by delege)
    strong: bool = False              # goes to the strongest model running (cloud lanes)
    description: str = ""
    shell: tuple[str, ...] = ()       # shell commands it may run, besides the read-only ones (OpenCode patterns)
    webfetch: tuple[str, ...] = ()    # URLs it may fetch (OpenCode patterns); none by default
    prompt: str = ""                  # extra lines for the task's prompt ({issue}, {branch}, {area})
    steps: int = 60


@dataclass(frozen=True)
class Forbidden:
    suffixes: tuple[str, ...] = MODEL_SUFFIXES + VIDEO_SUFFIXES
    media_suffixes: tuple[str, ...] = IMAGE_SUFFIXES
    dir_names: tuple[str, ...] = ()
    file_names: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass
class Project:
    root: Path
    name: str
    repo: str = ""                    # GitHub owner/name
    base: str = "main"
    remote: str = "origin"
    label: str = LABEL
    rules: str = ""
    max_file_bytes: int = MAX_FILE_BYTES
    agents: dict[str, Agent] = field(default_factory=dict)
    forbidden: Forbidden = field(default_factory=Forbidden)
    config_path: Path | None = None

    @property
    def opencode_config(self) -> Path:
        return self.root / "opencode.jsonc"

    @property
    def agents_dir(self) -> Path:
        return self.root / ".opencode" / "agents"


# ------------------------------------------------------------------ loading
def _strs(v, what: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{what}: a list of strings is expected")
    return tuple(v)


def _suffixes(v, what: str) -> tuple[str, ...]:
    return tuple(s.lower() if s.startswith(".") else "." + s.lower() for s in _strs(v, what))


def parse_agent(name: str, d: dict) -> Agent:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ConfigError(f"agent {name!r}: lower-case letters, digits, - and _ only (the OpenCode file name)")
    if not isinstance(d, dict):
        raise ConfigError(f"agents.{name}: a table is expected")
    areas = _strs(d.get("areas"), f"agents.{name}.areas")
    if not areas:
        raise ConfigError(f"agents.{name}: `areas` (the files it may change) is required")
    unknown = set(d) - {"areas", "proof", "cost", "media", "strong", "description", "shell", "webfetch", "prompt",
                        "steps"}
    if unknown:
        raise ConfigError(f"agents.{name}: unknown key(s) {', '.join(sorted(unknown))}")
    return Agent(name, areas, _strs(d.get("proof"), f"agents.{name}.proof"), float(d.get("cost", 1.0)),
                 bool(d.get("media", False)), bool(d.get("strong", False)), str(d.get("description", "")),
                 _strs(d.get("shell"), f"agents.{name}.shell"), _strs(d.get("webfetch"), f"agents.{name}.webfetch"),
                 str(d.get("prompt", "")), int(d.get("steps", 60)))


def parse(data: dict, root: Path, config_path: Path | None = None) -> Project:
    """A project from the parsed TOML table (``delege.toml``'s root or ``[tool.delege]``)."""
    p = data.get("project") or {}
    f = data.get("forbidden")
    agents = {n: parse_agent(n, a) for n, a in (data.get("agents") or {}).items()}
    if not agents:
        raise ConfigError("no agent: add [agents.<name>] tables (delege init writes tests, fix, lint, docs)")
    forb = Forbidden()
    if f is not None:
        forb = Forbidden(
            _suffixes(f["suffixes"], "forbidden.suffixes") if "suffixes" in f else forb.suffixes,
            _suffixes(f["media_suffixes"], "forbidden.media_suffixes") if "media_suffixes" in f
            else forb.media_suffixes,
            _strs(f.get("dir_names"), "forbidden.dir_names"), _strs(f.get("file_names"), "forbidden.file_names"),
            tuple(s.rstrip("/") for s in _strs(f.get("paths"), "forbidden.paths")))
    repo = str(p.get("repo") or "") or github_repo(root)
    return Project(root=root, name=str(p.get("name") or root.name), repo=repo, base=str(p.get("base", "main")),
                   remote=str(p.get("remote", "origin")), label=str(p.get("label", LABEL)),
                   rules=str(p.get("rules", "")).strip(), max_file_bytes=int(p.get("max_file_bytes", MAX_FILE_BYTES)),
                   agents=agents, forbidden=forb, config_path=config_path)


def find_config(root: Path) -> tuple[Path, dict] | None:
    """``delege.toml``, else ``pyproject.toml`` › ``[tool.delege]``: (path, table) or None."""
    p = root / CONFIG_NAME
    if p.exists():
        return p, tomllib.loads(p.read_text())
    pp = root / "pyproject.toml"
    if pp.exists():
        t = tomllib.loads(pp.read_text()).get("tool", {}).get("delege")
        if t:
            return pp, t
    return None


def load(root: Path | str) -> Project:
    root = Path(root).resolve()
    found = find_config(root)
    if found is None:
        raise ConfigError(f"no {CONFIG_NAME} (nor [tool.delege] in pyproject.toml) in {root}: run `delege init`")
    path, data = found
    try:
        return parse(data, root, path)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def repo_root(start: Path | str = ".") -> Path:
    """The git clone's root around ``start`` (``start`` itself when it is not in one)."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(start), capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path(start).resolve()


def github_repo(root: Path, remote: str = "origin") -> str:
    """``owner/name`` from the remote's URL (``git@github.com:o/n.git``, ``https://github.com/o/n``), or ""."""
    try:
        r = subprocess.run(["git", "remote", "get-url", remote], cwd=str(root), capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", r.stdout.strip())
    return m.group(1) if m else ""


# ------------------------------------------------------------------ areas and proofs
def match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def in_area(path: str, area: str) -> bool:
    """A task's ``Area:`` — a glob, a folder or a file."""
    return fnmatch.fnmatchcase(path, area) or path.startswith(area.rstrip("*/") + "/") or path == area


_CHANGED = re.compile(r"^\{changed(?::([^}]*?))?(!?)\}$")


def proof_commands(agent: Agent, files: list[str], issue: int | None, python: str) -> list[list[str]]:
    """The agent's proof commands for these changed files (see the module's doc for the placeholders)."""
    out = []
    for cmd in agent.proof:
        argv, skip = [], False
        for tok in shlex.split(cmd):
            m = _CHANGED.match(tok)
            if m:
                globs = [g for g in (m.group(1) or "").split("|") if g]
                picked = [f for f in files if not globs or match(f, globs)]
                if not picked:
                    if m.group(2):
                        argv = [python, "-c", f"import sys; sys.exit('no changed file matches {m.group(1)}: "
                                              "the task needs one (a test that proves the change)')"]
                        break
                    skip = True
                argv += picked
            elif tok == "{delege}":
                argv += [python, "-m", "delege"]
            else:
                if "{issue}" in tok and issue is None:
                    skip = True
                argv.append(tok.replace("{python}", python).replace("{issue}", str(issue)))
        else:
            if skip:
                continue
        out.append(argv)
    return out


def suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()
