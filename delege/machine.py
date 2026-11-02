"""The machine-level configuration: ``~/.config/delege/config.json`` — the owner's machines (hosts), the ssh
command, the commit identity, the timeout, the parallelism, the cloud switch, the models on / off and the registry
overrides. Nothing project-specific lives here (that is ``delege.toml`` in each repository).

::

    {
      "hosts": [
        {"name": "rtx", "slots": 1, "gpu_gb": 32},
        {"name": "box2", "ssh": "jm@box2.local", "repos": {"owner/project": "~/project"}, "slots": 1, "gpu_gb": 16,
         "python": ".venv/bin/python", "agents": ["tests", "lint", "docs"]}
      ],
      "identity": {"name": "OpenCode", "email": "opencode@localhost"},
      "model": null,                     // null: the model of the project's opencode.jsonc; "ollama/<name>": --model
      "timeout_min": 60,
      "parallel": 1,                     // tasks at once over all machines (one by one by default)
      "cloud": false,                    // the free hosted models too (delege run --cloud, or the settings page)
      "enabled": {"google/gemini-2.5-flash": true},        // per-model on / off (the settings page)
      "models": [{"id": "groq/openai/gpt-oss-20b", "limits": {"rpd": 10}, "strength": 3}]   // registry overrides
    }

A host without ``ssh`` is this machine. An ssh host's ``repos`` maps a GitHub ``owner/name`` to its clone on that
machine (relative to its home, or absolute; default ``~/<project name>``). Without the file: this machine, one slot.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import cloud, paths
from .opencode import find_opencode
from .util import read_json, write_json

DEFAULT_IDENTITY = {"name": "OpenCode", "email": "opencode@localhost"}
OLLAMA_URL = "http://localhost:11434"               # each machine's own Ollama ("ollama_url")
DEFAULT_SSH = ["ssh", "-o", "BatchMode=yes"]
HOST_KEYS = ("name", "ssh", "repos", "slots", "gpu_gb", "python", "opencode", "agents", "worktrees")


@dataclass
class Host:
    name: str
    ssh: str | None = None            # user@host; None = this machine
    repos: dict = field(default_factory=dict)   # GitHub owner/name → the clone on that machine
    slots: int = 1                    # tasks at once (one Ollama model serves them in turn: OLLAMA_NUM_PARALLEL)
    gpu_gb: float = 0.0               # GPU memory, the weight of the plan (0: CPU only)
    python: str = "python"
    opencode: str = "opencode"
    agents: list[str] | None = None   # only these agents (None: all)
    worktrees: str = ""
    repo: str = ""                    # resolved for the current project (``clone_of``)

    @property
    def local(self) -> bool:
        return not self.ssh

    @property
    def weight(self) -> float:
        return max(1, self.slots) * (self.gpu_gb if self.gpu_gb > 0 else 2.0)

    def can(self, agent: str) -> bool:
        return self.agents is None or agent in self.agents

    def to_json(self) -> dict:
        d = {"name": self.name}
        if self.ssh:
            d["ssh"] = self.ssh
        if self.repos:
            d["repos"] = dict(self.repos)
        for k in ("slots", "gpu_gb", "python", "opencode", "agents"):
            v = getattr(self, k)
            if v != Host.__dataclass_fields__[k].default and v is not None:
                d[k] = v
        return d


@dataclass
class Lane:
    """One task at a time: a machine's slot (its local model, ``model`` None) or one hosted model run here."""
    host: Host
    model: str | None = None
    strength: int = 2

    @property
    def name(self) -> str:
        return f"{self.model} on {self.host.name}" if self.model else f"{self.host.name} (local model)"


def _remote_path(p: str) -> str:
    return p[2:] if p.startswith("~/") else p


def local_gpu_gb() -> float:
    """This machine's largest NVIDIA GPU memory in GB (``nvidia-smi``), 0 without one."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return round(max(float(x) for x in out.split()) / 1024, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def raw_config(path: Path | None = None) -> dict:
    d = read_json(path or paths.config_file(), {})
    return d if isinstance(d, dict) else {}


def save_raw_config(d: dict, path: Path | None = None):
    write_json(path or paths.config_file(), d)


def host_from_json(h: dict) -> Host:
    h = {k: v for k, v in h.items() if k in HOST_KEYS}
    if "repo" in h and "repos" not in h:          # an older single-project entry
        h.pop("repo")
    host = Host(**h)
    host.repos = dict(host.repos) if isinstance(host.repos, dict) else {}
    return host


def load_config(path: Path | None = None, project_repo: str = "", project_name: str = "",
                project_root: Path | str | None = None) -> dict:
    """The machine configuration with its defaults, the hosts as ``Host`` objects, each resolved for the project
    (``project_repo`` the GitHub ``owner/name``, ``project_name`` the clone's folder name on other machines,
    ``project_root`` this machine's clone)."""
    path = Path(path or paths.config_file())
    cfg = dict(raw_config(path))
    hosts = []
    for h in cfg.get("hosts") or [{"name": "local"}]:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        host = host_from_json(h)
        if host.local:
            host.opencode = find_opencode(host.opencode)
            host.repo = str(project_root) if project_root else host.repos.get(project_repo, "")
            host.worktrees = host.worktrees or str(paths.worktrees_dir())
            if "gpu_gb" not in h:
                host.gpu_gb = local_gpu_gb()
        else:
            host.repo = _remote_path(host.repos.get(project_repo) or project_name or "repo")
            host.worktrees = _remote_path(host.worktrees or f"{paths.REMOTE_DATA}/worktrees")
        hosts.append(host)
    if not hosts:
        hosts = [Host("local")]
        hosts[0].opencode, hosts[0].repo = find_opencode(), str(project_root or "")
        hosts[0].worktrees = str(paths.worktrees_dir())
    cfg["hosts"] = hosts
    cfg["ssh"] = list(cfg.get("ssh") or DEFAULT_SSH)
    ident = cfg.get("identity")
    cfg["identity"] = dict(ident) if isinstance(ident, dict) and ident.get("name") else dict(DEFAULT_IDENTITY)
    cfg.setdefault("timeout_min", 60)
    cfg["parallel"] = max(1, int(cfg.get("parallel") or 1))
    cfg.setdefault("model", None)
    cfg.setdefault("ollama_url", OLLAMA_URL)
    cfg["cloud"] = bool(cfg.get("cloud"))
    cfg["probe"] = bool(cfg.get("probe", True))
    cfg["models"] = model_pool(cfg.get("models"))
    cfg["_path"] = str(path)
    return cfg


def model_pool(entries=None) -> list[cloud.Model]:
    """The hosted models that may be used: the registry of ``cloud.py``, extended or overridden by ``config.json``
    ``"models"`` (``{"id", "limits": {rpm, rpd, tpm, tpd}, "strength", "vision", "trains"}``; ``"use": false``
    drops one). A model without a declared free limit is dropped with no exception."""
    pool = {m.id: m for m in cloud.MODELS.values() if not m.local}
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        mid = str(e["id"])
        if e.get("use") is False:
            pool.pop(mid, None)
            continue
        base = pool.get(mid) or cloud.model(mid)
        lim = e.get("limits") or {}
        limits = cloud.Limits(**{k: lim.get(k, getattr(base.limits, k)) for k in ("rpm", "rpd", "tpm", "tpd")},
                              source=lim.get("source", base.limits.source or "config.json")) if lim else base.limits
        pool[mid] = cloud.Model(mid, e.get("name", base.name), limits, int(e.get("strength", base.strength)),
                                bool(e.get("vision", base.vision)), bool(e.get("trains", base.trains)),
                                e.get("policy", base.policy), int(e.get("context", base.context)))
    return [m for m in pool.values() if m.free and m.provider in cloud.PROVIDERS]


def usable_models(cfg: dict, opencode: str | None = None, probe: bool = True, usage: cloud.Usage | None = None,
                  settings: dict | None = None, listed: set[str] | None = None, models=None) -> list[tuple[cloud.Model, dict]]:
    """Each configured hosted model with its state: ``on`` (the switch, off for models that train unless switched
    on), ``key`` (where found), ``listed`` (``opencode models``), ``exhausted`` (today's count / benched), ``probe``
    (a 1-token answer, when ``probe``) and ``usable`` (all of them good). ``models``: the opencode-listed set (tests)."""
    from .opencode import opencode_models
    usage = usage or cloud.Usage()
    settings = settings if settings is not None else cloud.load_settings()
    if listed is None and opencode:
        listed = opencode_models(opencode)
    out = []
    for m in models or cfg["models"]:
        st = {"on": cloud.enabled(m, settings), "key": cloud.key_for(m.provider)[1], "listed": None, "probe": None,
              "exhausted": usage.exhausted(m), "usable": False}
        if listed is not None:
            st["listed"] = m.id in listed
        ok = st["on"] and bool(st["key"]) and st["listed"] is not False and not st["exhausted"]
        if ok and probe:
            good, msg = cloud.probe(m.id, usage=usage)
            st["probe"] = msg
            ok = good
        st["usable"] = ok
        out.append((m, st))
    return out
