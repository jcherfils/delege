"""Runs with a fake OpenCode 2 on this machine and on fake ssh hosts: the branch, the commit, the diff guard, the
proofs, reports, the push guard, --redo, unreachable hosts, one task at a time, cloud lanes and benching."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from delege import cli, cloud, paths
from delege.github import issue_comment, post_outcomes
from delege.machine import Host, Lane, load_config
from delege.plan import plan
from delege.project import load
from delege.reports import push_branch, write_reports
from delege.runner import Delegator, git
from delege.tasks import Task

from .conftest import git as tgit
from .conftest import machine_config
from .test_agents import decide

quiet = {"log": lambda *a: None}


def test_plan_weighted_by_gpu_memory_and_slots(repo):
    p = load(repo)
    hosts = [Host("big", ssh="a", slots=2, gpu_gb=32), Host("small", ssh="b", slots=1, gpu_gb=16),
             Host("cpu", ssh="c", slots=1)]
    tasks = [Task(str(i), "tests", f"t{i}") for i in range(14)]
    counts = {}
    for _t, h in plan(tasks, hosts, p):
        counts[h.name] = counts.get(h.name, 0) + 1
    assert counts["big"] > counts["small"] > counts.get("cpu", 0)
    assert 10 <= counts["big"] <= 12 and counts["small"] >= 2
    hosts[0].agents = hosts[1].agents = ["docs"]
    out = plan([Task("a", "screenshots", "x"), Task("b", "docs", "y")], hosts, p)
    assert [h.name for _t, h in out] == ["cpu", "big"]
    hosts[2].agents = ["docs"]
    assert plan([Task("a", "lint", "x")], hosts, p)[0][1] is None


def test_config_defaults(repo, tmp_path):
    p = load(repo)
    cfg = load_config(tmp_path / "none.json", p.repo, p.name, p.root)
    [h] = cfg["hosts"]
    assert h.local and h.repo == str(repo) and h.worktrees == str(paths.worktrees_dir())
    assert cfg["identity"] == {"name": "OpenCode", "email": "opencode@localhost"} and cfg["parallel"] == 1
    c = tmp_path / "d.json"
    c.write_text(json.dumps({"hosts": [{"name": "rtx", "ssh": "jm@rtx", "repos": {"o/r": "~/Thing"}, "gpu_gb": 32},
                                       {"name": "other", "ssh": "jm@o", "gpu_gb": 8}]}))
    r, o = load_config(c, p.repo, p.name, p.root)["hosts"]
    assert not r.local and r.repo == "Thing" and r.worktrees == ".local/share/delege/worktrees" and r.weight == 32
    assert o.repo == "proj"                                            # default: ~/<project name>


def test_dry_run_runs_nothing(repo, fakes, capsys, monkeypatch):
    tasks = fakes["tmp"] / "tasks.txt"
    tasks.write_text("tests: Add tests for budget\n")
    machine_config(fakes, repo)
    monkeypatch.chdir(repo)
    assert cli.main(["run", "--dry-run", "--tasks", str(tasks)]) == 0
    out = capsys.readouterr().out
    assert "delegate/t-add-tests-for-budget-tests" in out and "opencode run --standalone --agent tests" in out
    assert not fakes["log"].exists() and not paths.worktrees_dir().exists()
    assert "delegate/" not in tgit("branch", cwd=repo)
    assert cli.main(["plan", "--tasks", str(tasks)]) == 0


def test_run_local_commits_a_branch_and_never_pushes(repo, fakes, monkeypatch):
    cfg = machine_config(fakes, repo, identity={"name": "Owner", "email": "owner@example.org"})
    monkeypatch.setenv("FAKE_WRITE", json.dumps({"tests/test_new.py": "def test_ok():\n    assert True\n"}))
    p = load(repo)
    d = Delegator(cfg, p, **quiet)
    [r] = d.run([Task("12", "tests", "Tests for budget", "body", issue=12)])
    assert r["status"] == "ok", r
    assert r["files"] == ["tests/test_new.py"]
    assert r["proof"][0]["rc"] == 0 and "pytest" in r["proof"][0]["cmd"]
    log = [json.loads(x) for x in fakes["log"].read_text().splitlines()]
    assert log[0]["args"][:4] == ["run", "--standalone", "--agent", "tests"] and "Issue #12" in log[0]["args"][-1]
    assert "bikeshed" in log[0]["args"][-1]                     # the project's rules reach the agent
    assert "--auto" not in log[0]["args"]
    assert log[0]["cwd"].startswith(str(paths.worktrees_dir()))
    assert tgit("log", "-1", "--format=%an <%ae>|%s", "delegate/12-tests", cwd=repo) == \
        "Owner <owner@example.org>|delegate: Tests for budget (tests, #12)"
    assert "Refs #12" in tgit("log", "-1", "--format=%b", "delegate/12-tests", cwd=repo)
    assert tgit("diff", "--name-only", "main", "delegate/12-tests", cwd=repo) == "tests/test_new.py"
    assert tgit("rev-parse", "main", cwd=repo) == r["base"]
    assert not Path(r["worktree"]).exists()
    out = write_reports([r], fakes["tmp"] / "reports")
    rep = (out / "delegate_12-tests.md").read_text()
    assert "ok" in rep and "tests/test_new.py" in rep and "Review:" in rep
    assert json.loads((out / "summary.json").read_text())[0]["branch"] == "delegate/12-tests"
    [again] = Delegator(cfg, p, **quiet).run([Task("12", "tests", "Tests", issue=12)])
    assert again["status"] == "skipped"
    # --redo: the earlier branch is dropped and the task runs again
    [redo] = Delegator(cfg, p, **quiet).run([Task("12", "tests", "Tests", issue=12)], redo=True)
    assert redo["status"] == "ok"


def test_run_refuses_outside_area_models_and_proof_failures(repo, fakes, monkeypatch):
    cfg = machine_config(fakes, repo)
    p = load(repo)
    monkeypatch.setenv("FAKE_WRITE", json.dumps({"proj/x.py": "x = 1\n", "tests/w.pt": "bin"}))
    [r] = Delegator(cfg, p, **quiet).run([Task("7", "tests", "x")])
    assert r["status"] == "refused" and len(r["violations"]) == 2
    assert tgit("rev-parse", "delegate/7-tests", cwd=repo) == tgit("rev-parse", "main", cwd=repo)   # no commit
    assert Path(r["worktree"]).exists()                                                              # kept
    monkeypatch.setenv("FAKE_WRITE", "{}")
    [n] = Delegator(cfg, p, **quiet).run([Task("8", "docs", "x")])
    assert n["status"] == "no-change" and "delegate/8-docs" not in tgit("branch", cwd=repo)
    monkeypatch.setenv("FAKE_RC", "3")
    [f] = Delegator(cfg, p, **quiet).run([Task("9", "docs", "x")])
    assert f["status"] == "failed"
    monkeypatch.delenv("FAKE_RC")
    # a fix without a test: committed, but the proof fails ({changed:...!})
    monkeypatch.setenv("FAKE_WRITE", json.dumps({"proj/x.py": "x = 1\n"}))
    [pf] = Delegator(cfg, p, **quiet).run([Task("10", "fix", "x")])
    assert pf["status"] == "proof-failed" and "no changed file matches" in pf["proof"][1]["out"]
    assert tgit("diff", "--name-only", "main", "delegate/10-fix", cwd=repo) == "proj/x.py"


def test_run_distributed_over_fake_hosts(repo, fakes, monkeypatch):
    """Two ssh machines (fake ssh: each its own home) and this one: the base commit travels to each clone under a
    private ref, each runs its tasks in worktrees under its home, and the branches come back here."""
    hosts_dir = fakes["tmp"] / "hosts"
    for name in ("gpu1", "gpu2"):
        (hosts_dir / name).mkdir(parents=True)
        tgit("clone", "-q", str(repo), str(hosts_dir / name / "proj"), cwd=fakes["tmp"])
    hosts = [{"name": "gpu1", "ssh": "gpu1", "slots": 2, "gpu_gb": 32, "python": sys.executable,
              "opencode": fakes["opencode"]},
             {"name": "gpu2", "ssh": "gpu2", "repos": {"o/r": "~/proj"}, "slots": 2, "gpu_gb": 32,
              "python": sys.executable, "opencode": fakes["opencode"]}]
    cfg = machine_config(fakes, repo, hosts=hosts, parallel=3)
    (repo / "NEW.md").write_text("new\n")
    tgit("add", "-A", cwd=repo)
    tgit("commit", "-q", "-m", "local only", cwd=repo)
    monkeypatch.setenv("FAKE_WRITE", json.dumps({"tests/test_new.py": "def test_ok():\n    assert True\n"}))
    tasks = [Task(f"r{i}", "tests", f"tests {i}") for i in range(3)]
    results = Delegator(cfg, load(repo), **quiet).run(tasks)
    assert [r["status"] for r in results] == ["ok"] * 3, results
    assert {r["host"] for r in results} == {"gpu1", "gpu2"}
    for r in results:
        assert "def test_ok" in tgit("show", f"{r['branch']}:tests/test_new.py", cwd=repo)
        assert tgit("show", f"{r['branch']}:NEW.md", cwd=repo) == "new"
        assert r["proof"][0]["cmd"].endswith("pytest -q -p no:cacheprovider tests/test_new.py")
        assert r["proof"][0]["rc"] == 0
    log = [json.loads(x) for x in fakes["log"].read_text().splitlines()]
    for e in log:
        assert e["cwd"].startswith(e["home"] + "/.local/share/delege/worktrees/")
    for name in ("gpu1", "gpu2"):
        clone = hosts_dir / name / "proj"
        assert "delegate/" not in tgit("branch", cwd=clone)
        assert tgit("rev-parse", "HEAD", cwd=clone) != tgit("rev-parse", "main", cwd=repo)


def test_push_only_checked_delegate_branches(repo, fakes, tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    tgit("init", "-q", "--bare", str(origin), cwd=tmp_path)
    tgit("remote", "add", "origin", str(origin), cwd=repo)
    p = load(repo)
    for name, files in (("delegate/1-tests", {"tests/test_x.py": "", "docs/NOTES.md": "notes"}),
                        ("delegate/2-tests", {"tests/best.pt": "w"}),
                        ("delegate/3-screenshots", {"docs/screens/a.png": "p"}),
                        ("feature", {"x.py": ""})):
        tgit("checkout", "-q", "-b", name, "main", cwd=repo)
        for f, text in files.items():
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text(text)
        tgit("add", "-A", cwd=repo)
        tgit("commit", "-q", "-m", name, cwd=repo)
    tgit("checkout", "-q", "main", cwd=repo)
    ok, msg = push_branch("delegate/1-tests", p)
    assert ok, msg
    assert tgit("rev-parse", "delegate/1-tests", cwd=origin) == tgit("rev-parse", "delegate/1-tests", cwd=repo)
    for b in ("delegate/2-tests", "delegate/3-screenshots", "feature", "main"):
        ok, msg = push_branch(b, p)
        assert not ok, b
    assert "model" in push_branch("delegate/2-tests", p)[1]
    heads = tgit("for-each-ref", "--format=%(refname)", cwd=origin).split()
    assert heads == ["refs/heads/delegate/1-tests"]
    machine_config(fakes, repo)
    monkeypatch.chdir(repo)
    assert cli.main(["push", "delegate/1-tests", "delegate/2-tests"]) == 1


def test_run_refuses_opencode_1(repo, fakes, monkeypatch):
    cfg = machine_config(fakes, repo)
    monkeypatch.setenv("FAKE_VERSION", "1.19.4")
    [r] = Delegator(cfg, load(repo), **quiet).run([Task("70", "tests", "x", issue=70)])
    assert r["status"] == "failed" and "OpenCode 1.19.4" in r["note"] and "v2/install" in r["note"]
    assert not fakes["log"].exists() and "delegate/" not in tgit("branch", cwd=repo)


def test_redo_drops_an_earlier_branch_and_unreachable_hosts_are_left_out(repo):
    tgit("branch", "delegate/9-docs", cwd=repo)
    p = load(repo)
    cfg = {"hosts": [Host(name="here"), Host(name="far", ssh="nobody@far.invalid")], "ssh": ["false"],
           "parallel": 1, "identity": {}, "timeout_min": 1}
    lines = []
    d = Delegator(cfg, p, log=lines.append)
    assert [h.name for h in d.reachable_hosts()] == ["here"]
    assert any("far unreachable" in ln for ln in lines)
    d.drop_branch("delegate/9-docs")
    assert not git("branch", "--list", "delegate/9-docs", cwd=repo)
    d.drop_branch("main")
    assert git("branch", "--list", "main", cwd=repo)


def test_finished_tasks_comment_their_issue_and_are_labelled():
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        calls.append(argv)
        return R()
    ok = {"issue": 70, "status": "ok", "branch": "delegate/70-tests", "agent": "tests", "host": "rtx", "seconds": 42,
          "base": "abc", "files": ["tests/test_x.py"], "proof": [{"cmd": "pytest tests/test_x.py", "rc": 0, "out": ""}],
          "output": "Changed: tests/test_x.py\nProof: 3 passed", "pushed": True}
    bad = {**ok, "issue": 71, "status": "failed", "branch": "delegate/71-lint", "pushed": False}
    skip = {**ok, "issue": 72, "status": "skipped"}
    lines = post_outcomes([ok, bad, skip, {**ok, "issue": None}], repo="o/r", run=run)
    assert len(lines) == 2 and "delegate:done" in lines[0] and "delegate:failed" in lines[1]
    bodies = [a[-1] for a in calls if a[2].endswith("/comments")]
    assert len(bodies) == 2 and "Done" in bodies[0] and "origin/delegate/70-tests" in bodies[0]
    assert "3 passed" in bodies[0] and "failed" in bodies[1]
    assert ["gh", "api", "repos/o/r/issues/70/labels", "-f", "labels[]=delegate:done"] in calls
    assert ["gh", "api", "-X", "DELETE", "repos/o/r/issues/71/labels/delegate:done"] in calls
    assert "delege push delegate/70-tests" in issue_comment({**ok, "pushed": False})


def test_one_task_at_a_time_by_default(repo):
    p = load(repo)
    for parallel, expected in ((1, 1), (2, 2)):
        cfg = {"hosts": [Host(name="here", slots=3)], "ssh": ["false"], "parallel": parallel, "identity": {},
               "timeout_min": 1}
        d = Delegator(cfg, p, **quiet)
        busy, peak, lock = [0], [0], threading.Lock()

        def fake(t, h, busy=busy, peak=peak, lock=lock):
            with lock:
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            time.sleep(0.05)
            with lock:
                busy[0] -= 1
            return {"task": t.id, "agent": t.agent, "branch": t.branch, "status": "ok", "host": h.name}
        d.run_task = fake
        tasks = [Task(id=str(i), agent="docs", title=f"t{i}") for i in range(4)]
        res = d.run(tasks)
        assert len(res) == 4 and peak[0] == expected, (parallel, peak[0])


def test_run_end_to_end_through_the_cli(repo, fakes, monkeypatch, capsys):
    """``delege run`` with the fake gh: the issues become tasks, the branches are made, the reports written, the
    issues commented and labelled; --no-push since there is no remote."""
    machine_config(fakes, repo)
    monkeypatch.setenv("PATH", f"{fakes['bin']}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_WRITE_BY_AGENT", json.dumps({
        "docs": {"README.md": "# new\n"}, "lint": {"proj/__init__.py": "# lint\n"},
        "tests": {"tests/test_new.py": "def test_ok():\n    assert True\n"}}))
    monkeypatch.chdir(repo)
    assert cli.main(["run", "--no-push"]) == 0
    out = capsys.readouterr().out
    assert "reports:" in out and "#72: commented, delegate:done" in out
    assert {"delegate/70-tests", "delegate/71-lint", "delegate/72-docs"} <= set(tgit("branch", "--format=%(refname:short)", cwd=repo).split())
    gh = [json.loads(x) for x in (fakes["tmp"] / "gh.log").read_text().splitlines()]
    assert any(a[1] == "repos/o/r/issues/70/comments" for a in gh)
    assert any(a[1:] == ["repos/o/r/issues/70/labels", "-f", "labels[]=delegate:done"] for a in gh)
    assert cli.main(["run", "--no-push", "--only", "70"]) == 0            # skipped: the branch exists


# ------------------------------------------------------------------ cloud lanes
def test_run_cloud_spreads_tasks_over_usable_models(repo, fakes, monkeypatch):
    cfg = machine_config(fakes, repo, cloud=True, probe=False)
    monkeypatch.setenv("FAKE_MODELS", "ollama/qwen3:14b,groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b")
    cloud.set_key("groq", "gsk_secret")
    peak, busy, lock = [0], [0], threading.Lock()
    real = Delegator.run_task

    def counting(self, t, h, model=None):
        with lock:
            busy[0] += 1
            peak[0] = max(peak[0], busy[0])
        try:
            time.sleep(0.2)
            return real(self, t, h, model)
        finally:
            with lock:
                busy[0] -= 1
    monkeypatch.setattr(Delegator, "run_task", counting)
    monkeypatch.setenv("FAKE_WRITE_BY_AGENT", json.dumps({
        "docs": {"README.md": "# new\n"}, "fix": {"proj/x.py": "x = 1\n", "tests/test_x.py": "def test_x():\n    pass\n"},
        "tests": {"tests/test_new.py": "def test_ok():\n    assert True\n"}, "lint": {"tests/__init__.py": "# lint\n"}}))
    lines = []
    d = Delegator(cfg, load(repo), log=lines.append)
    tasks = [Task("1", "docs", "d1", issue=1), Task("2", "fix", "f2", issue=2), Task("3", "tests", "t3"),
             Task("4", "lint", "l4"), Task("5", "docs", "d5")]
    results = d.run(tasks)
    assert any("3 lane(s)" in ln for ln in lines), lines
    assert peak[0] == 3
    by = {r["task"]: r for r in results}
    assert {r["status"] for r in results} <= {"ok", "proof-failed"}, [(r["status"], r.get("note")) for r in results]
    assert by["2"]["model"] == "groq/openai/gpt-oss-120b"                      # strong → the strongest (3)
    models = {r["model"] for r in results}
    assert "groq/openai/gpt-oss-20b" in models and "ollama/qwen3:14b" in models
    log = [json.loads(x) for x in fakes["log"].read_text().splitlines()]
    hosted = [e for e in log if e["model"] and e["model"].startswith("groq/")]
    assert hosted and all(e["keys"].get("GROQ_API_KEY") == "gsk_secret" for e in hosted)
    assert all("--format" in e["args"] for e in hosted)
    assert all("--format" not in e["args"] for e in log if not e["model"])
    for r in results:
        if r["model"].startswith("groq/"):
            assert r["tokens"] == {"input": 220, "output": 50, "steps": 2}
        msg = tgit("log", "-1", "--format=%B", r["branch"], cwd=repo)
        assert f"Made by OpenCode ({r['model']})" in msg
        assert "opencode.jsonc" not in tgit("diff", "--name-only", "main", r["branch"], cwd=repo)
    u = cloud.Usage()
    assert sum(u.counts(m)[0] for m in ("groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b")) == 2 * len(hosted)
    assert "Usage: 2 requests, 220 tokens in, 50 out" in issue_comment(by["2"])
    assert "with `groq/openai/gpt-oss-120b` on local" in issue_comment(by["2"])


def test_run_cloud_benches_a_rate_limited_model_and_retries(repo, fakes, monkeypatch):
    from delege import opencode
    cfg = machine_config(fakes, repo, cloud=True, probe=False, models=[{"id": "groq/openai/gpt-oss-20b", "use": False},
                                                                      {"id": "groq/qwen/qwen3.8-27b", "use": False}])
    monkeypatch.setenv("FAKE_MODELS", "ollama/qwen3:14b,groq/openai/gpt-oss-120b,"
                                      "openrouter/poolside/laguna-s-2.1:free")
    cloud.set_key("groq", "a")
    cloud.set_key("openrouter", "b")
    monkeypatch.setenv("FAKE_429", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("FAKE_WRITE", json.dumps({"tests/test_new.py": "def test_ok():\n    assert True\n"}))
    seen = []
    orig = opencode.write_task_config

    def spy(wt, providers, src=None):
        seen.append(sorted(providers))
        p = orig(wt, providers, src)
        pol = opencode.read_jsonc(p)["policies"]
        assert decide(pol, "provider.use", providers[-1], "allow") == "allow"
        assert decide(pol, "provider.use", "anthropic", "allow") == "deny"
        return p
    monkeypatch.setattr("delege.runner.write_task_config", spy)
    lines = []
    p = load(repo)
    d = Delegator(cfg, p, log=lines.append)
    [r] = d.run([Task("9", "fix", "f", issue=9)])
    assert r["status"] in ("ok", "proof-failed") and r["model"] != "groq/openai/gpt-oss-120b", (r, lines)
    assert any("benched until its reset" in ln for ln in lines), lines
    u = cloud.Usage()
    assert u.benched_until("groq/openai/gpt-oss-120b")
    assert "benched" in u.exhausted(cloud.MODELS["groq/openai/gpt-oss-120b"])
    assert ["groq", "ollama"] in seen
    assert "delegate/9-fix" in tgit("branch", cwd=repo)
    assert not (paths.worktrees_dir() / "delegate_9-fix").exists()
    lines.clear()
    d = Delegator(cfg, p, log=lines.append)
    [r2] = d.run([Task("10", "docs", "d")])
    assert any("groq/openai/gpt-oss-120b: not used (benched" in ln for ln in lines), lines
    assert any("2 lane(s)" in ln for ln in lines)
    monkeypatch.setenv("FAKE_429", "groq/openai/gpt-oss-120b,openrouter/poolside/laguna-s-2.1:free")
    d = Delegator(cfg, p, **quiet)
    d.cloud_lanes = lambda hosts, probe=True: [Lane(hosts[0], "openrouter/poolside/laguna-s-2.1:free", 2)]
    [r3] = d.run([Task("11", "docs", "d")])
    assert r3["status"] == "skipped" and "free limit" in r3["note"]


def test_dry_run_cloud_shows_lanes(repo, fakes, monkeypatch, capsys):
    machine_config(fakes, repo)
    monkeypatch.setenv("FAKE_MODELS", "ollama/qwen3:14b,groq/openai/gpt-oss-20b")
    cloud.set_key("groq", "k")
    tasks = fakes["tmp"] / "tasks.txt"
    tasks.write_text("docs: README rows\n")
    monkeypatch.chdir(repo)
    rc = cli.main(["run", "--cloud", "--no-probe", "--dry-run", "--tasks", str(tasks)])
    out = capsys.readouterr().out
    assert rc == 0 and "cloud models on: 2 task(s) at once" in out and "groq/openai/gpt-oss-20b on local" in out
    assert not paths.worktrees_dir().exists() and "delegate/" not in tgit("branch", cwd=repo)


def test_agents_and_proofs_use_the_hosts_python_on_the_worktree(tmp_path):
    """Debian has no `python` command: on this machine the agent's and the proofs' `python` is the Python delege runs
    in (its folder first on PATH), and imports come from the task's worktree, not an editable install."""
    import sys

    from delege import runner
    from delege.machine import Host
    here = Host(name="here")
    assert runner.host_python(here) == sys.executable
    env = runner.python_env(sys.executable, str(tmp_path), True)
    assert env["PYTHONPATH"] == str(tmp_path) and env["PATH"].split(":")[0] == str(Path(sys.executable).parent)
    far = Host(name="far", ssh="me@far", python="/opt/venv/bin/python")
    assert runner.host_python(far) == "/opt/venv/bin/python"
    assert runner.python_env(far.python, "/w", False)["PATH"] == "/opt/venv/bin:$PATH"
