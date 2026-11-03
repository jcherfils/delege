"""Shared fixtures: delege's files in a fresh folder (``DELEGE_HOME``), no keyring, no key in the environment; a
project repository with a ``delege.toml``; fake ``opencode`` (OpenCode 2), ``ssh`` and ``gh`` commands."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from delege import cloud

ROOT = Path(__file__).resolve().parents[1]
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@localhost"]

PROJECT_TOML = '''
[project]
name = "proj"
repo = "o/r"
base = "main"
rules = """
- Never mention the colour of the bikeshed.
"""

[forbidden]
dir_names = ["videos", "_dataset"]
file_names = ["result.json"]
paths = ["data/my_set"]

[agents.tests]
description = "Adds tests"
areas = ["tests/*"]
proof = ["{python} -m pytest -q -p no:cacheprovider {changed:tests/*test_*.py}"]
shell = ["python -m pytest *"]

[agents.fix]
areas = ["proj/*", "tests/*"]
proof = ["{python} -c 'print(1)' {changed:*.py}", "{python} -m pytest -q -p no:cacheprovider {changed:tests/*test_*.py!}"]
cost = 1.5
strong = true

[agents.lint]
areas = ["*.py"]
proof = ["{python} -c 'import sys; print(sys.argv)' {changed:*.py}"]

[agents.docs]
areas = ["README.md", "docs/*.md"]
cost = 0.5

[agents.checks]
areas = ["proj/checks/*", "tests/test_checks*.py"]
proof = ["{python} -c 'print({issue})'"]
strong = true
prompt = "The check to write or run: python -m proj.checks {issue}"

[agents.screenshots]
areas = ["docs/screens/*"]
proof = ["{delege} check-images {changed:*.png}"]
cost = 3.0
media = true
shell = ["python docs/shots.py *"]
webfetch = ["https://example.org/*"]
'''


def git(*args, cwd):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """delege's configuration and state in ``tmp_path/home``; no keyring, no OpenCode store, no key in the env."""
    h = tmp_path / "home"
    monkeypatch.setenv("DELEGE_HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("FIFOO_HOME", str(tmp_path / "no-fifoo"))
    monkeypatch.setattr(cloud, "_keyring", lambda: None)
    for p in cloud.PROVIDERS.values():
        if p.key_env:
            monkeypatch.delenv(p.key_env, raising=False)
    return h


@pytest.fixture
def repo(tmp_path):
    """A git repository with ``delege.toml``, the template's ``opencode.jsonc`` and the generated agents."""
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "proj").mkdir()
    (r / "README.md").write_text("# test\n")
    (r / "tests" / "__init__.py").write_text("")
    (r / "proj" / "__init__.py").write_text("")
    (r / "delege.toml").write_text(PROJECT_TOML)
    (r / "opencode.jsonc").write_text((ROOT / "template" / "opencode.jsonc").read_text())
    from delege.init import write_agents
    from delege.project import load
    write_agents(load(r))
    git("init", "-q", "-b", "main", cwd=r)
    git("add", "-A", cwd=r)
    git("commit", "-q", "-m", "base", cwd=r)
    return r


FAKE_OPENCODE = """#!{py}
# fake OpenCode 2: --version, debug agents (the .opencode/agents of its directory), run --standalone --agent A MSG
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "opencode v2.1.4"))
    sys.exit(0)
if args[:2] == ["debug", "agents"]:
    names = sorted(p[:-3] for p in os.listdir(".opencode/agents") if p.endswith(".md"))
    print("\\n".join(["build  primary", "plan  primary"] + [n + "  all" for n in names]))
    sys.exit(0)
if args == ["models"]:
    print(os.environ.get("FAKE_MODELS", "ollama/qwen3:14b").replace(",", "\\n"))
    sys.exit(0)
assert args[:4] == ["run", "--standalone", "--agent", args[3]], args
model = args[args.index("--model") + 1] if "--model" in args else None
keys = {{k: v for k, v in os.environ.items() if k.endswith("_API_KEY")}}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"cwd": os.getcwd(), "args": args, "home": os.environ.get("HOME"), "model": model,
                        "keys": keys}}) + "\\n")
as_json = "--format" in args and args[args.index("--format") + 1] == "json"
if as_json and model in os.environ.get("FAKE_429", "").split(","):
    print(json.dumps({{"type": "error", "sessionID": "ses_1", "error": {{"name": "APIError", "data": {{
        "message": "Rate limit exceeded", "statusCode": 429, "isRetryable": True}}}}}}))
    sys.exit(1)
writes = json.loads(os.environ.get("FAKE_WRITE", "{{}}"))
by_agent = json.loads(os.environ.get("FAKE_WRITE_BY_AGENT", "{{}}"))
writes.update(by_agent.get(args[3], {{}}))
for path, text in writes.items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write(text)
if as_json:
    for i in range(2):
        print(json.dumps({{"type": "step_start", "sessionID": "ses_1", "part": {{"type": "step-start"}}}}))
        print(json.dumps({{"type": "step_finish", "sessionID": "ses_1", "part": {{"reason": "tool-calls",
            "cost": 0, "tokens": {{"input": 100, "output": 20, "reasoning": 5, "cache": {{"read": 10, "write": 0}}}}}}}}))
    print(json.dumps({{"type": "text", "sessionID": "ses_1", "part": {{"text": "Changed: done\\nProof: fake\\nLeft: nothing"}}}}))
else:
    print("Changed: done\\nProof: fake\\nLeft: nothing")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""

FAKE_SSH = """#!/bin/bash
# fake ssh: [-o OPT]... HOST COMMAND — runs COMMAND in $FAKE_HOSTS/HOST (its home)
while [[ "$1" == -* ]]; do shift 2; done
host=$1; shift
export HOME="$FAKE_HOSTS/$host"
cd "$HOME" && exec bash -c "$*"
"""

FAKE_GH = """#!{py}
import json, os, sys
args = sys.argv[1:]
ok = os.environ.get("FAKE_GH_AUTH", "1") == "1"
with open(os.environ.get("FAKE_GH_LOG", os.devnull), "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["auth", "status"]:
    print("github.com: Logged in to github.com account jm" if ok
          else "You are not logged into any GitHub hosts. To log in, run: gh auth login", file=sys.stderr)
    sys.exit(0 if ok else 1)
assert args[0] == "api", args
if "issues?labels=delegate" in args[1]:
    print(json.dumps([
        {{"number": 72, "title": "Glossary", "labels": [{{"name": "delegate"}}, {{"name": "agent:docs"}}], "body": ""}},
        {{"number": 71, "title": "except pass", "labels": [{{"name": "delegate"}}, {{"name": "agent:lint"}}], "body": ""}},
        {{"number": 70, "title": "budget tests", "labels": [{{"name": "delegate"}}], "body": "Agent: tests"}}]))
else:
    print("{{}}")
"""


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    oc = b / "opencode"
    oc.write_text(FAKE_OPENCODE.format(py=sys.executable))
    ssh = b / "fake-ssh"
    ssh.write_text(FAKE_SSH)
    gh = b / "gh"
    gh.write_text(FAKE_GH.format(py=sys.executable))
    for f in (oc, ssh, gh):
        f.chmod(0o755)
    log = tmp_path / "opencode.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_HOSTS", str(tmp_path / "hosts"))
    for v in ("FAKE_WRITE", "FAKE_WRITE_BY_AGENT", "FAKE_RC", "FAKE_VERSION", "FAKE_429", "FAKE_MODELS"):
        monkeypatch.delenv(v, raising=False)
    return {"opencode": str(oc), "ssh": str(ssh), "gh": str(gh), "bin": b, "log": log, "tmp": tmp_path}


def machine_config(fakes, repo, hosts=None, **kw) -> dict:
    """Writes ``config.json`` (this machine by default: the fake opencode, this Python) and loads it for ``repo``."""
    from delege import paths
    from delege.machine import load_config
    from delege.project import load
    hosts = hosts or [{"name": "local", "python": sys.executable, "opencode": fakes["opencode"], "gpu_gb": 8}]
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(json.dumps({"hosts": hosts, "ssh": [fakes["ssh"]], **kw}))
    p = load(repo)
    return load_config(None, p.repo, p.name, p.root)
