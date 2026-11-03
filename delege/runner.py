"""Running tasks in isolated Git worktrees with OpenCode or Aider, then checking diffs, running proofs and
committing branches. Cloud mode adds a lane per usable hosted model alongside local-model lanes."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cloud
from .aider import aider_argv
from .checks import task_violations
from .machine import Host, Lane, usable_models
from .opencode import OPENCODE_CONFIG, OPENCODE_INSTALL, opencode_argv, parse_json_output, version_problem, \
    write_task_config
from .plan import plan
from .project import BRANCH_PREFIX, Project, proof_commands
from .tasks import Task, prompt
from .util import read_jsonc, tail


class Shell:
    """Runs a command on a host: here, or over ssh (``cd DIR && CMD``)."""

    def __init__(self, host: Host, ssh: list[str]):
        self.host, self.ssh = host, ssh

    def __call__(self, argv: list[str], cwd: str, timeout: float | None = None, env: dict | None = None,
                 check: bool = False) -> subprocess.CompletedProcess:
        if self.host.local:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, **(env or {})})
        else:
            pre = " ".join(f'{k}="{v}"' if v.endswith("$PATH") and '"' not in v else f"{k}={shlex.quote(v)}"
                           for k, v in (env or {}).items())    # PATH=<folder>:$PATH expands on the host
            cmd = f"cd {shlex.quote(cwd)} && {pre + ' ' if pre else ''}{shlex.join(argv)}"
            r = subprocess.run([*self.ssh, self.host.ssh, cmd], capture_output=True, text=True, timeout=timeout)
        if check and r.returncode:
            err = (r.stderr or r.stdout)[-800:]
            raise RuntimeError(f"{self.host.name}: {shlex.join(argv)} → {r.returncode}: {err}")
        return r


def host_python(h) -> str:
    """The Python of a host's tests: its ``python`` setting; on this machine the default ``python`` is the Python
    delege runs in (it has the project installed — Debian has no ``python`` command, only ``python3``)."""
    if h.local and h.python in ("", "python"):
        return sys.executable
    return h.python


def python_env(py: str, worktree: str, local: bool) -> dict:
    """Environment for the agent and the proofs: ``python`` / ``pytest`` resolve to the host's Python (its folder first
    on PATH) and imports come from the task's worktree, not from an editable install of the main checkout."""
    env = {"PYTHONPATH": worktree, "PYTHONDONTWRITEBYTECODE": "1"}
    folder = os.path.dirname(py)
    if folder:
        env["PATH"] = folder + os.pathsep + (os.environ.get("PATH", "") if local else "$PATH")
    return env


def git(*args, cwd, check=True, env=None) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-800:]}")
    return r.stdout.strip()


def status_files(out: str) -> list[str]:
    """Paths of ``git status --porcelain=v1 -z`` (a rename / copy is followed by its old path: skipped)."""
    files, recs, i = [], out.split("\0"), 0
    while i < len(recs):
        rec = recs[i]
        if len(rec) > 3:
            files.append(rec[3:])
            if rec[0] in "RC":
                i += 1
        i += 1
    return files


class Delegator:
    def __init__(self, cfg: dict, project: Project, log=print):
        self.cfg, self.project, self.log = cfg, project, log
        self.repo_dir = Path(project.root)
        self.base_sha = git("rev-parse", project.base, cwd=self.repo_dir)
        self.local_lock = threading.Lock()
        self.host_locks = {h.name: threading.Lock() for h in cfg["hosts"]}
        self.base_sent: set[str] = set()
        self.resolved: set[str] = set()
        self.opencode_ok: dict[str, str | None] = {}  # host → None (OpenCode 2) or the problem
        self.aider_ok: dict[str, str | None] = {}
        self.git_ssh = {"GIT_SSH_COMMAND": shlex.join(cfg["ssh"])}
        self.usage = cloud.Usage(Path(cfg["usage_path"]) if cfg.get("usage_path") else None)
        try:
            self.default_model = read_jsonc(project.opencode_config).get("model")
        except (OSError, ValueError):
            self.default_model = None

    # ------------------------------------------------------------ hosts
    def _remote_url(self, h: Host) -> str:
        return f"{h.ssh}:{h.repo}"

    def _send_base(self, h: Host):
        """The base commit to a remote clone (only the missing objects travel), under a private ref — never one of
        its branches."""
        with self.host_locks[h.name]:
            if h.name in self.base_sent:
                return
            git("push", "--quiet", self._remote_url(h), f"{self.base_sha}:refs/delegate/base-{self.base_sha[:12]}",
                cwd=self.repo_dir, env=self.git_ssh)
            self.base_sent.add(h.name)

    def _resolve(self, sh: Shell, h: Host):
        """A remote machine's paths relative to its home → absolute (commands run inside the clone)."""
        with self.host_locks[h.name]:
            if h.name in self.resolved:
                return
            out = sh(["sh", "-c", 'echo "$HOME"; command -v opencode || true; command -v aider || true'],
                     cwd=".", check=True).stdout.split("\n")
            home, found = out[0].strip(), (out[1].strip() if len(out) > 1 else "")
            aider_found = out[2].strip() if len(out) > 2 else ""
            h.repo = h.repo if h.repo.startswith("/") else f"{home}/{h.repo}"
            h.worktrees = h.worktrees if h.worktrees.startswith("/") else f"{home}/{h.worktrees}"
            if h.opencode.startswith("~/"):
                h.opencode = f"{home}/{h.opencode[2:]}"
            elif h.opencode == "opencode" and not found:
                h.opencode = f"{home}/.opencode/bin/opencode"   # OpenCode 2's installer; ssh does not read ~/.bashrc
            if h.aider.startswith("~/"):
                h.aider = f"{home}/{h.aider[2:]}"
            if h.aider == "aider" and aider_found:
                h.aider = aider_found
            self.resolved.add(h.name)

    def _check_opencode(self, sh: Shell, h: Host):
        """OpenCode 2 on that machine, once per machine; raises with the fix otherwise."""
        with self.host_locks[h.name]:
            if h.name not in self.opencode_ok:
                try:
                    r = sh([h.opencode, "--version"], cwd=h.repo, timeout=60)
                    text = r.stdout + r.stderr if r.returncode == 0 else ""
                    self.opencode_ok[h.name] = version_problem(text, h.name)
                except (OSError, subprocess.SubprocessError) as e:
                    self.opencode_ok[h.name] = f"opencode not found on {h.name} ({e}) — install: {OPENCODE_INSTALL}"
            if self.opencode_ok[h.name]:
                raise RuntimeError(self.opencode_ok[h.name])

    def _check_aider(self, sh: Shell, h: Host):
        """Aider is an optional local engine; any installed version with the supported CLI is accepted."""
        with self.host_locks[h.name]:
            if h.name not in self.aider_ok:
                try:
                    r = sh([h.aider, "--version"], cwd=h.repo, timeout=30)
                    self.aider_ok[h.name] = None if r.returncode == 0 else (
                        f"aider --version failed on {h.name}: {(r.stderr or r.stdout).strip()[-300:]}")
                except (OSError, subprocess.SubprocessError) as e:
                    self.aider_ok[h.name] = f"aider not found on {h.name} ({e}) — install: python -m pip install aider-chat"
            if self.aider_ok[h.name]:
                raise RuntimeError(self.aider_ok[h.name])

    def reachable_hosts(self) -> list[Host]:
        """The machines that answer now: local ones always, ssh ones that connect within 8 s. Unreachable ones are
        left out of this run's plan (their tasks go to the others), with a line saying so."""
        out = []
        for h in self.cfg["hosts"]:
            if h.local:
                out.append(h)
                continue
            try:
                r = subprocess.run([*self.cfg["ssh"], "-o", "ConnectTimeout=8", h.ssh, "true"],
                                   capture_output=True, text=True, timeout=20)
                ok, why = r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            except (OSError, subprocess.SubprocessError) as e:
                ok, why = False, [str(e)]
            if ok:
                out.append(h)
            else:
                self.log(f"   {h.name} unreachable ({why[0][:120]}): left out of this run — fix or remove it "
                         f"from {self.cfg.get('_path', 'config.json')}")
        return out

    # ------------------------------------------------------------ one task
    def run_task(self, task: Task, h: Host, model: str | None = None) -> dict:
        t0 = time.time()
        res = self._run_task(task, h, model)
        res["seconds"] = round(time.time() - t0, 1)
        return res

    def _model_name(self, model: str | None) -> str:
        return model or self.cfg.get("model") or self.default_model or "project default model"

    def _run_task(self, task: Task, h: Host, model: str | None = None) -> dict:
        """``model``: a hosted model of the pool (``provider/model``, run here with ``--format json``, the
        worktree's config allowing its provider, the keys in the environment); None: the host's local model."""
        hosted = bool(model) and not cloud.model(model).local
        engine = "opencode" if hosted else h.engine
        res = {"task": task.id, "agent": task.agent, "title": task.title, "issue": task.issue, "host": h.name,
               "engine": engine,
               "model": self._model_name(model), "branch": task.branch, "base": self.base_sha, "files": [],
               "violations": [], "proof": []}
        if git("branch", "--list", task.branch, cwd=self.repo_dir):
            return {**res, "status": "skipped",
                    "note": f"branch {task.branch} exists here (review it, or run again with --redo)"}
        sh = Shell(h, self.cfg["ssh"])
        try:
            if not h.local:
                self._resolve(sh, h)
            if engine == "aider":
                self._check_aider(sh, h)
            else:
                self._check_opencode(sh, h)
            if not h.local:
                self._send_base(h)
            wt = f"{h.worktrees}/{task.branch.replace('/', '_')}"
            res["worktree"] = wt
            with self.host_locks[h.name] if not h.local else self.local_lock:
                sh(["git", "worktree", "add", "--quiet", "-b", task.branch, wt, self.base_sha], cwd=h.repo, check=True)
            env = None
            if hosted:
                if not h.local:
                    raise RuntimeError("hosted models run on this machine only")
                if not cloud.model(model).free:
                    raise RuntimeError(f"{model}: no declared free limit — refused (never a paid path)")
                write_task_config(Path(wt), ["ollama", cloud.model(model).provider])
                env = cloud.key_env()
            py = host_python(h)
            env = {**(env or {}), **python_env(py, wt, h.local)}           # the agent's `python` = the host's, on this code
            task_prompt = prompt(task, self.project)
            if engine == "aider":
                aider_model_id = model or self.cfg.get("model") or self.default_model
                if not aider_model_id or not aider_model_id.startswith(("ollama/", "ollama_chat/")):
                    raise RuntimeError("the Aider engine currently requires a local Ollama model")
                if h.local:
                    env["OLLAMA_API_BASE"] = self.cfg.get("ollama_url") or "http://localhost:11434"
                from .init import split_agent_file
                agent_file = self.project.agents_dir / f"{task.agent}.md"
                if agent_file.exists():
                    instructions = split_agent_file(agent_file.read_text())[1].strip()
                    if instructions:
                        task_prompt += "\n\nInstructions for this agent:\n" + instructions
                argv = aider_argv(h.aider, task_prompt, aider_model_id)
            else:
                argv = opencode_argv(h.opencode, task.agent, task_prompt, model or self.cfg.get("model"),
                                     json_output=hosted)
            try:
                r = sh(argv, cwd=wt, timeout=self.cfg["timeout_min"] * 60, env=env)
                res["agent_rc"], out = r.returncode, r.stdout + "\n" + r.stderr
            except subprocess.TimeoutExpired:
                res["agent_rc"], out = -1, f"timeout after {self.cfg['timeout_min']} min"
            if hosted:
                ev = parse_json_output(out)
                res["tokens"] = {"input": ev["input"], "output": ev["output"], "steps": ev["steps"]}
                self.usage.record(model, ev["steps"], ev["input"] + ev["output"])
                res["output"] = tail(ev["text"] + ("\n" + ev["error"] if ev["error"] else ""), 80)
                (Path(wt) / OPENCODE_CONFIG).unlink(missing_ok=True)                  # delege's own file:
                sh(["git", "checkout", "--quiet", "--", OPENCODE_CONFIG], cwd=wt)      # back to the base commit's
                if res["agent_rc"] != 0 and cloud.looks_rate_limited(ev["error"] or out[-2000:], ev["status"]):
                    res["rate_limited"] = ev["error"] or f"HTTP {ev['status']}"
                    self._drop(sh, h, wt, task.branch)
                    return {**res, "status": "failed",
                            "note": f"{model}: free limit reached ({res['rate_limited'][:120]})"}
            else:
                res["output"] = tail(out, 80)
            st = sh(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=wt, check=True).stdout
            made = sh(["git", "diff", "--name-only", self.base_sha, "HEAD"], cwd=wt, check=True).stdout.split()
            files = sorted(set(status_files(st)) | set(made))
            res["files"] = files
            if made:
                res["violations"].append("the agent committed by itself (it must not)")
            res["violations"] += task_violations(files, task, self.project)
            if res["agent_rc"] != 0:
                return {**res, "status": "failed", "note": f"{engine} did not finish; worktree kept for a look"}
            if res["violations"]:
                return {**res, "status": "refused", "note": "nothing committed; worktree kept for a look"}
            if not files:
                self._drop(sh, h, wt, task.branch)
                return {**res, "status": "no-change"}
            ok = True
            for cmd in proof_commands(self.project.agents[task.agent], files, task.issue, py):
                p = sh(cmd, cwd=wt, timeout=1800, env={"PYTHONDONTWRITEBYTECODE": "1", **python_env(py, wt, h.local)})
                res["proof"].append({"cmd": shlex.join(cmd), "rc": p.returncode, "out": tail(p.stdout + p.stderr, 25)})
                ok &= p.returncode == 0
            ident = self.cfg["identity"]
            msg = self._message(task, h, res)
            sh(["git", "add", "-A", "--", *files], cwd=wt, check=True)       # only what was checked
            sh(["git", "-c", f"user.name={ident['name']}", "-c", f"user.email={ident['email']}", "commit", "--quiet",
                "-m", msg], cwd=wt, check=True)
            res["commit"] = sh(["git", "rev-parse", "HEAD"], cwd=wt, check=True).stdout.strip()
            if not h.local:
                with self.local_lock:
                    git("fetch", "--quiet", self._remote_url(h), f"refs/heads/{task.branch}:refs/heads/{task.branch}",
                        cwd=self.repo_dir, env=self.git_ssh)
                self._drop(sh, h, wt, task.branch)
            else:
                with self.local_lock:
                    sh(["git", "worktree", "remove", "--force", wt], cwd=h.repo)
            return {**res, "status": "ok" if ok else "proof-failed"}
        except Exception as e:  # noqa: BLE001  (one task's failure never stops the others; reported as "failed")
            return {**res, "status": "failed", "note": str(e)[-800:]}

    def _drop(self, sh, h: Host, wt: str, branch: str):
        lock = self.local_lock if h.local else self.host_locks[h.name]
        with lock:
            sh(["git", "worktree", "remove", "--force", wt], cwd=h.repo)
            sh(["git", "branch", "-D", branch], cwd=h.repo)

    def _message(self, task: Task, h: Host, res: dict) -> str:
        ref = f", #{task.issue}" if task.issue else ""
        proof = "; ".join(f"{p['cmd']} → {'ok' if p['rc'] == 0 else 'FAILED'}" for p in res["proof"]) or "none"
        engine = {"opencode": "OpenCode", "aider": "Aider"}.get(res.get("engine", h.engine), res.get("engine"))
        lines = [f"delegate: {task.title[:60]} ({task.agent}{ref})", "",
                 f"Made by {engine} ({res.get('model') or self._model_name(None)}) on {h.name} "
                 "with delege; "
                 "not reviewed yet.", f"Proof: {proof}"]
        if task.issue:
            lines.append(f"Refs #{task.issue}")
        return "\n".join(lines)

    def drop_branch(self, branch: str):
        """``--redo``: removes an earlier result of this task (its branch and worktree) so the task runs again."""
        if not branch.startswith(BRANCH_PREFIX) or not git("branch", "--list", branch, cwd=self.repo_dir):
            return
        for block in git("worktree", "list", "--porcelain", cwd=self.repo_dir).split("\n\n"):
            lines = block.splitlines()
            if f"branch refs/heads/{branch}" in lines and lines and lines[0].startswith("worktree "):
                subprocess.run(["git", "worktree", "remove", "--force", lines[0][9:]], cwd=self.repo_dir,
                               capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo_dir, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch], cwd=self.repo_dir, capture_output=True)
        self.log(f"   {branch}: earlier result removed (--redo)")

    # ------------------------------------------------------------ scheduling
    def cloud_on(self) -> bool:
        return bool(self.cfg.get("cloud"))

    def cloud_lanes(self, hosts: list[Host], probe: bool = True) -> list[Lane]:
        """With cloud models on: one lane per local host slot (its Ollama model) plus one per usable hosted model
        (run on the first local host); parallelism is their number."""
        local = [h for h in hosts if h.local]
        lanes = [Lane(h, None, cloud.model(self.cfg.get("model") or self.default_model or "").strength)
                 for h in hosts for _ in range(max(1, h.slots))]
        if not local:
            self.log("   no local machine: hosted models run from here only — none used")
            return lanes
        for m, st in usable_models(self.cfg, local[0].opencode, probe=probe, usage=self.usage):
            if st["usable"]:
                lanes.append(Lane(local[0], m.id, m.strength))
            else:
                why = (st["exhausted"] or ("off" if not st["on"] else "no key" if not st["key"] else
                       "not listed by opencode models" if st["listed"] is False else st["probe"] or "?"))
                self.log(f"   {m.id}: not used ({why})")
        return lanes

    def run(self, tasks: list[Task], redo: bool = False) -> list[dict]:
        hosts = self.reachable_hosts()
        if not hosts:
            return [{"task": t.id, "agent": t.agent, "branch": t.branch, "status": "skipped",
                     "note": "no machine reachable"} for t in tasks]
        if redo:
            for t in tasks:
                self.drop_branch(t.branch)
        if self.cloud_on():
            return self.run_lanes(tasks, self.cloud_lanes(hosts, probe=self.cfg.get("probe", True)))
        assigned = plan(tasks, hosts, self.project)
        results = []
        parallel = max(1, int(self.cfg.get("parallel") or 1))
        gate = threading.Semaphore(parallel)              # tasks at once over all machines (default 1: one by one)

        def one(t, h):
            with gate:
                self.log(f"→ {t.branch} on {h.name}")
                return self.run_task(t, h)
        pools = {h.name: ThreadPoolExecutor(max_workers=max(1, min(h.slots, parallel))) for h in self.cfg["hosts"]}
        futs = []
        for t, h in assigned:
            if h is None:
                results.append({"task": t.id, "agent": t.agent, "branch": t.branch, "status": "skipped",
                                "note": "no machine takes this agent (hosts' agents lists)"})
                continue
            futs.append(pools[h.name].submit(one, t, h))
        for f in futs:
            r = f.result()
            self.log(f"{r['status']:>12}  {r['branch']}  ({r.get('host')}) {r.get('note', '')}")
            results.append(r)
        for p in pools.values():
            p.shutdown()
        return results

    def run_lanes(self, tasks: list[Task], lanes: list[Lane]) -> list[dict]:
        """Cloud scheduling: each lane (a local slot, or a hosted model) takes the heaviest task left that suits it
        — the ``strong`` agents wait for the strongest lanes still running — and goes on until the queue is empty.
        A hosted model that hits its free limit (429, a quota message, its daily count) is benched until its reset,
        its lane stops and the task goes back to the queue for another lane; a task no lane is left for is
        ``skipped``. Parallelism = the number of lanes."""
        agents = self.project.agents
        order = {id(t): i for i, t in enumerate(tasks)}
        queue = sorted(tasks, key=lambda t: (-agents[t.agent].cost, order[id(t)]))
        results, done, alive, busy = [], [], list(lanes), set()
        cond = threading.Condition()
        self.log(f"   {len(lanes)} lane(s): " + ", ".join(ln.name for ln in lanes))

        def pick(lane: Lane) -> Task | None:
            with cond:
                while True:
                    best = max((ln.strength for ln in alive), default=lane.strength)
                    for i, t in enumerate(queue):
                        if agents[t.agent].strong and lane.strength < best and any(
                                ln is not lane and ln.strength > lane.strength for ln in alive):
                            continue
                        busy.add(id(lane))
                        return queue.pop(i)
                    if not busy and not queue:
                        return None
                    cond.wait(0.1)                         # a stronger lane will take what is left, or give back

        def work(lane: Lane):
            while True:
                t = pick(lane)
                if t is None:
                    break
                self.log(f"→ {t.branch} on {lane.name}")
                r = self.run_task(t, lane.host, lane.model)
                with cond:
                    busy.discard(id(lane))
                    if r.get("rate_limited") and lane.model:
                        self.usage.bench(lane.model, why=r["rate_limited"])
                        self.log(f"   {lane.model} benched until its reset: {r['rate_limited'][:100]} — "
                                 f"{t.branch} goes to another model")
                        queue.insert(0, t)
                        alive.remove(lane)
                        cond.notify_all()
                        return
                    done.append(r)
                    cond.notify_all()
                self.log(f"{r['status']:>12}  {r['branch']}  ({r.get('model')} on {r.get('host')}) {r.get('note', '')}")
            with cond:
                if lane in alive:
                    alive.remove(lane)
                cond.notify_all()

        threads = [threading.Thread(target=work, args=(ln,), daemon=True) for ln in lanes]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        results.extend(done)
        for t in queue:
            results.append({"task": t.id, "agent": t.agent, "branch": t.branch, "status": "skipped",
                            "note": "every model reached its free limit (benched until its reset): run again later"})
        return sorted(results, key=lambda r: order.get(id(next((t for t in tasks if t.id == r["task"]), None)), 0))
