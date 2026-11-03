"""``delege doctor``: checks what a delegated run needs on this machine, each failure with one concrete fix line."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import paths
from .github import issues_tasks
from .init import stale_agents
from .machine import DEFAULT_IDENTITY, OLLAMA_URL, load_config
from .opencode import MIN_CONTEXT, OPENCODE_CONFIG, OPENCODE_INSTALL, find_opencode, version_problem
from .project import ConfigError, Project
from .project import load as load_project
from .util import read_jsonc

OLLAMA_FIX_CONTEXT = ('sudo systemctl edit ollama.service   # under [Service]: Environment="OLLAMA_CONTEXT_LENGTH=65536" '
                      '(+ OLLAMA_FLASH_ATTENTION=1, OLLAMA_KV_CACHE_TYPE=q8_0); then: sudo systemctl restart ollama')
DEFAULT_MODEL = "ollama/qwen3:14b"


def http_json(url: str, data=None, timeout: float = 10):
    """A JSON request to Ollama on this machine (never through a proxy)."""
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if data is None else "POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def model_context(show: dict) -> tuple[int, int | None]:
    """``/api/show`` → (the model's own context length, ``num_ctx`` set in its parameters or None)."""
    native = max((int(v) for k, v in (show.get("model_info") or {}).items()
                  if k.endswith(".context_length") and str(v).isdigit()), default=0)
    m = re.search(r"^\s*num_ctx\s+(\d+)", show.get("parameters") or "", re.M)
    return native, int(m.group(1)) if m else None


def ollama_status(url: str = OLLAMA_URL, timeout: float = 5) -> dict:
    """``{"up": bool, "models": [names], "loaded": [names], "error": str}`` — the settings page and ``doctor``."""
    url = url.rstrip("/")
    try:
        tags = http_json(f"{url}/api/tags", timeout=timeout) or {}
        ps = http_json(f"{url}/api/ps", timeout=timeout) or {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"up": False, "models": [], "loaded": [], "error": str(e)}
    return {"up": True, "models": [m.get("name") for m in tags.get("models") or []],
            "loaded": [m.get("name") for m in ps.get("models") or []], "error": ""}


class Doctor:
    """One check per line; a failure prints its fix. Exit 1 while something fails."""

    def __init__(self, repo_dir: Path | str = ".", cfg_path: Path | None = None, out=print, load_model: bool = True):
        self.repo_dir = Path(repo_dir).resolve()
        self.cfg_path = Path(cfg_path) if cfg_path else paths.config_file()
        self.out, self.load_model = out, load_model
        self.failed = 0
        self.cfg: dict = {}
        self.project: Project | None = None

    def report(self, status: str, name: str, detail: str, fix: str | None = None):
        if status == "FAIL":
            self.failed += 1
        self.out(f"{status:5} {name:10} {detail}")
        if fix and status != "ok":
            self.out(f"{'':16} fix: {fix}")

    def _run(self, argv, cwd=None, timeout=60) -> tuple[int, str]:
        try:
            r = subprocess.run(argv, cwd=cwd or self.repo_dir, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            return -1, str(e)

    def run(self) -> int:
        for step in (self.check_project, self.check_config, self.check_repo, self.check_python, self.check_opencode,
                     self.check_ollama, self.check_github, self.check_git, self.check_hosts):
            try:
                step()
            except Exception as e:  # noqa: BLE001  (one broken check never hides the others; shown as a failure)
                self.report("FAIL", step.__name__.removeprefix("check_"), f"the check itself failed: {e}")
        self.out("")
        self.out(f"{self.failed} problem(s): fix them, then run doctor again." if self.failed else
                 "Ready: delege plan   then   delege run --only <issue>")
        return 1 if self.failed else 0

    # ------------------------------------------------------------ checks
    def check_project(self):
        try:
            self.project = load_project(self.repo_dir)
        except ConfigError as e:
            self.report("FAIL", "project", str(e), f"cd {self.repo_dir} && delege init")
            return
        p = self.project
        self.report("ok", "project", f"{p.config_path}: {p.name}, repo {p.repo or '?'}, base {p.base}, agents "
                    + ", ".join(p.agents))
        if not p.repo:
            self.report("warn", "project", "no GitHub repository known (issues cannot be read)",
                        f'[project] repo = "owner/name" in {p.config_path}')

    def _agents(self) -> list[str]:
        return list(self.project.agents) if self.project else []

    def check_config(self):
        p = self.cfg_path
        proj = self.project
        self.cfg = load_config(p, proj.repo if proj else "", proj.name if proj else "", self.repo_dir)
        hosts = ", ".join(f"{h.name} ({'here' if h.local else h.ssh}, {h.slots} slot(s), {h.gpu_gb:g} GB)"
                          for h in self.cfg["hosts"])
        bad = sorted({a for h in self.cfg["hosts"] for a in (h.agents or []) if a not in self._agents()})
        if bad:
            self.report("FAIL", "config", f"{p}: unknown agent(s) {', '.join(bad)}",
                        f"agents: {', '.join(self._agents())}")
        elif p.exists():
            self.report("ok", "config", f"{p}: {hosts}; parallel {self.cfg['parallel']}, cloud "
                        f"{'on' if self.cfg['cloud'] else 'off'}")
        else:
            self.report("warn", "config", f"no {p}: this machine, one slot ({hosts})",
                        "delege settings   (hosts, parallel, cloud models and keys)")

    def check_repo(self):
        rc, top = self._run(["git", "rev-parse", "--show-toplevel"])
        if rc or Path(top).resolve() != self.repo_dir.resolve():
            self.report("FAIL", "repo", f"{self.repo_dir} is not the root of a git clone ({top[-200:]})",
                        "cd <the clone's root>")
            return
        if not self.project:
            return
        base = self.project.base
        problems = []
        if not (self.repo_dir / OPENCODE_CONFIG).exists():
            problems.append(f"no {OPENCODE_CONFIG} (delege init)")
        if self._run(["git", "rev-parse", "--verify", "--quiet", base])[0]:
            problems.append(f"no branch {base}")
        stale = stale_agents(self.project)
        if stale:
            problems.append("agents to refresh: " + "; ".join(stale))
        if problems:
            self.report("FAIL", "repo", f"{self.repo_dir}: " + "; ".join(problems),
                        f"cd {self.repo_dir} && delege init && git switch {base} && git pull")
            return
        sha = self._run(["git", "rev-parse", "--short", base])[1]
        self.report("ok", "repo", f"{self.repo_dir} ({base} {sha}, {OPENCODE_CONFIG}, {len(self._agents())} agents)")
        if (self.repo_dir / "opencode.json").exists():
            self.report("warn", "repo", "an OpenCode 1 opencode.json is still at the root (OpenCode 2 would mix both)",
                        f"rm {self.repo_dir / 'opencode.json'}   (if it is not tracked by git)")

    def check_python(self):
        """The proofs (and the agents' own `python -m pytest`) need a Python with the project's test requirements."""
        local = [h for h in self.cfg.get("hosts", []) if h.local]
        py = local[0].python if local else "python"
        rc, out = self._run([py, "-c", "import sys, pytest; print(sys.version.split()[0], sys.executable)"])
        if rc:
            self.report("FAIL", "python", f"`{py}` cannot run pytest: {out.splitlines()[-1] if out else rc}",
                        f"cd {self.repo_dir} && source .venv/bin/activate   (or \"python\": \"<venv>/bin/python\" "
                        "on this host in config.json)")
        else:
            self.report("ok", "python", f"{py}: Python {out}")

    def _opencode(self) -> str:
        local = [h for h in self.cfg.get("hosts", []) if h.local]
        return local[0].opencode if local else find_opencode()

    def check_opencode(self):
        local = [h for h in self.cfg.get("hosts", []) if h.local]
        if local and local[0].engine == "aider":
            aider = local[0].aider
            rc, text = self._run([aider, "--version"])
            if rc:
                self.report("FAIL", "aider", f"`{aider} --version` failed: {text[-200:]}",
                            "python -m pip install aider-chat")
            else:
                self.report("ok", "aider", f"{text.splitlines()[0]} ({shutil.which(aider) or aider})")
            self.report("warn", "aider", "Delege checks changed files, but Aider does not enforce OpenCode's read permissions")
            if not self.cfg.get("cloud"):
                return
        oc = self._opencode()
        rc, text = self._run([oc, "--version"])
        problem = version_problem(text if rc == 0 else "", "this machine")
        if problem:
            self.report("FAIL", "opencode", problem if rc == 0 else f"`{oc} --version` failed: {text[-200:]}",
                        OPENCODE_INSTALL)
            return
        where = shutil.which(oc) or oc
        self.report("ok", "opencode", f"{text.splitlines()[0]} ({where})")
        if not shutil.which("opencode"):
            self.report("warn", "opencode", f"`opencode` is not on PATH (found {oc})",
                        'echo \'export PATH="$HOME/.opencode/bin:$PATH"\' >> ~/.profile   (then open a new shell)')
        agents = self._agents()
        if not agents:
            return
        out = ""
        for argv in ([oc, "debug", "agents", "--standalone"], [oc, "debug", "agents"]):
            rc, out = self._run(argv, timeout=120)
            if rc == 0:
                break
        if rc:
            self.report("warn", "agents", f"`opencode debug agents` failed: {out[-300:]}",
                        f"cd {self.repo_dir} && opencode debug config   (shows which configuration files OpenCode reads)")
            return
        seen = [a for a in agents if re.search(rf"(?<![\w/-]){re.escape(a)}(?![\w/-])", out)]
        missing = [a for a in agents if a not in seen]
        if missing:
            self.report("FAIL", "agents", f"OpenCode does not list {', '.join(missing)} (sees {', '.join(seen) or 'none'})",
                        f"cd {self.repo_dir} && delege init && opencode reload   (the agents are "
                        f".opencode/agents/*.md; the background service keeps the config it read at start)")
        else:
            self.report("ok", "agents", "OpenCode sees " + ", ".join(seen))

    def _model(self) -> str:
        model = self.cfg.get("model")
        if not model:
            try:
                model = read_jsonc(self.repo_dir / OPENCODE_CONFIG).get("model")
            except (OSError, ValueError):
                model = None
        return model or DEFAULT_MODEL

    def check_ollama(self):
        url = str(self.cfg.get("ollama_url") or OLLAMA_URL).rstrip("/")
        model = self._model()
        if not model.startswith("ollama/"):
            self.report("warn", "ollama", f"the model is {model}, not an Ollama one: not checked")
            return
        name = model.split("/", 1)[1]
        try:
            ps = http_json(f"{url}/api/ps") or {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.report("FAIL", "ollama", f"{url} not reachable ({e})",
                        "sudo systemctl enable --now ollama   (not installed: curl -fsSL https://ollama.com/install.sh | sh)")
            return
        try:
            show = http_json(f"{url}/api/show", {"model": name}) or {}
        except urllib.error.HTTPError as e:
            self.report("FAIL", "ollama", f"model {name} not in Ollama (HTTP {e.code})", f"ollama pull {name}")
            return
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.report("FAIL", "ollama", f"/api/show {name}: {e}", "sudo systemctl restart ollama")
            return
        native, num_ctx = model_context(show)
        need = min(MIN_CONTEXT, native) if native else MIN_CONTEXT     # a smaller model: its whole context
        if native and native < MIN_CONTEXT:
            self.report("warn", "model", f"{name}: its context is {native} tokens (< {MIN_CONTEXT}): keep its "
                        "tasks small", "a model with 64 K tokens of context for bigger tasks")
        if num_ctx is not None and num_ctx < need:
            self.report("FAIL", "model", f"{name}: num_ctx {num_ctx} in its Modelfile (< {need})",
                        f"a Modelfile with PARAMETER num_ctx {need} (ollama create), or another tag")
        else:
            self.report("ok", "model", f"{name} in Ollama ({native or '?'} tokens of context natively"
                        + (f", num_ctx {num_ctx}" if num_ctx else "") + ")")
        loaded = self._loaded(ps, name)
        if loaded is None and self.load_model:
            self.out(f"{'':16} loading {name} to measure it (the first time takes up to a minute)…")
            try:
                http_json(f"{url}/api/generate", {"model": name, "keep_alive": "10m"}, timeout=600)
                loaded = self._loaded(http_json(f"{url}/api/ps") or {}, name)
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.report("FAIL", "load", f"Ollama could not load {name}: {e}",
                            "journalctl -u ollama -n 50   (out of GPU memory?)")
                return
        if loaded is None:
            self.report("warn", "load", f"{name} is not loaded: its context and GPU share are not measured",
                        f'ollama run {name} "hello" && delege doctor')
            return
        ctx = loaded.get("context_length")
        if not ctx:
            self.report("warn", "context", "this Ollama does not report the context in use", "ollama ps   (CONTEXT ≥ 65536)")
        elif int(ctx) < need:
            self.report("FAIL", "context", f"{name} runs with {ctx} tokens of context (< {need})", OLLAMA_FIX_CONTEXT)
        else:
            self.report("ok", "context", f"{name} runs with {ctx} tokens of context")
        size, vram = int(loaded.get("size") or 0), int(loaded.get("size_vram") or 0)
        share = vram / size if size else 0.0
        gb = f"{vram / 1e9:.1f} of {size / 1e9:.1f} GB on the GPU"
        if share < 0.99:
            self.report("FAIL", "gpu", f"{name}: {share:.0%} on the GPU ({gb}): the rest runs on the CPU, many times slower",
                        "free the GPU (nvidia-smi), set OLLAMA_KV_CACHE_TYPE=q8_0 + OLLAMA_FLASH_ATTENTION=1 in "
                        "the ollama service, or a smaller context; then: sudo systemctl restart ollama")
        else:
            self.report("ok", "gpu", f"{name}: 100% GPU ({gb})")

    @staticmethod
    def _loaded(ps: dict, name: str) -> dict | None:
        for m in ps.get("models") or []:
            if name in (m.get("name"), m.get("model")) or str(m.get("name", "")).startswith(name + ":"):
                return m
        return None

    def check_github(self):
        if not shutil.which("gh"):
            self.report("FAIL", "github", "gh is not installed (delege reads the issues with gh api)",
                        "sudo apt install gh && gh auth login")
            return
        rc, out = self._run(["gh", "auth", "status"])
        if rc:
            self.report("FAIL", "github", f"gh is not logged in: {out.splitlines()[-1] if out else rc}", "gh auth login")
            return
        if not self.project or not self.project.repo:
            return
        try:
            tasks, notes = issues_tasks(self.project)
        except SystemExit as e:
            self.report("FAIL", "github", str(e), "gh auth login")
            return
        label = self.project.label
        found = ", ".join(f"#{t.id} {t.agent}" for t in tasks) or "none"
        if tasks:
            self.report("ok", "github", f"issues labelled {label}: {found}")
        else:
            self.report("warn", "github", f"no open issue labelled {label} with an agent",
                        f"label an issue `{label}` + `agent:<name>`")
        for n in notes:
            self.report("warn", "github", n)

    def check_git(self):
        name = self._run(["git", "config", "user.name"])[1]
        email = self._run(["git", "config", "user.email"])[1]
        ident = self.cfg.get("identity") or DEFAULT_IDENTITY
        commits = f"delegated commits by {ident.get('name')} <{ident.get('email')}> (config.json \"identity\")"
        if name and email:
            self.report("ok", "git", f"you: {name} <{email}>; {commits}")
        else:
            self.report("warn", "git", f"no git identity here (needed for your own merges); {commits}",
                        'git config --global user.name "Your Name" && git config --global user.email you@example.org')

    def check_hosts(self):
        for h in self.cfg.get("hosts", []):
            if h.local:
                continue
            ssh = [*self.cfg["ssh"], h.ssh]
            if h.engine == "aider":
                aider = shlex.quote(h.aider)
                rc, out = self._run([*ssh, f"{aider} --version"], timeout=30)
                if rc:
                    self.report("FAIL", h.name, f"aider over ssh: {out[-200:] or rc}",
                                f"on {h.name}: python -m pip install aider-chat")
                else:
                    self.report("ok", h.name, f"{out.splitlines()[0]} over ssh")
                if not self.cfg.get("cloud"):
                    continue
            oc = h.opencode if h.opencode != "opencode" else '"$(command -v opencode || echo ~/.opencode/bin/opencode)"'
            rc, out = self._run([*ssh, f"{oc} --version"], timeout=30)
            if rc:
                fix = f"ssh-copy-id {h.ssh}" if "denied" in out.lower() or rc == 255 else f"on {h.name}: {OPENCODE_INSTALL}"
                self.report("FAIL", h.name, f"ssh {h.ssh}: {out[-200:] or rc}", fix)
                continue
            problem = version_problem(out, h.name)
            self.report("FAIL" if problem else "ok", h.name, problem or f"{out.splitlines()[0]} over ssh",
                        f"on {h.name}: {OPENCODE_INSTALL}")
