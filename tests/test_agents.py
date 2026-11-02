"""The agents' OpenCode 2 files generated from delege.toml: their rule lists evaluated with OpenCode's semantics
(area allowed, the rest denied, push denied, forbidden folders never read…), ``delege init`` (idempotent, keeps the
bodies), the template kept in sync, ``opencode.jsonc`` local-only and the per-task copy with cloud providers."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from delege import init, opencode, project
from delege.util import read_jsonc

from .conftest import ROOT, PROJECT_TOML


def _wild(pattern: str, value: str) -> bool:
    """OpenCode 2's wildcards: ``*`` any characters (``/`` too), ``?`` one, the rest literal; a shell pattern ending
    in `` *`` also matches the bare command (https://opencode.ai/v2/docs/permissions/)."""
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    if re.fullmatch(rx, value, re.S):
        return True
    return pattern.endswith(" *") and value == pattern[:-2]


def decide(rules, action: str, resource: str, default="ask") -> str:
    """The effect of an OpenCode 2 rule list: the last matching rule wins; none → ``ask``."""
    effect = default
    for r in rules:
        if r["action"] == action and _wild(r["resource"], resource):
            effect = r["effect"]
    return effect


def _meta(path: Path) -> dict:
    return yaml.safe_load(init.split_agent_file(path.read_text())[0].strip("-\n"))


def test_generated_agents_match_the_config(tmp_path):
    (tmp_path / "delege.toml").write_text(PROJECT_TOML)
    p = project.load(tmp_path)
    files = {f.stem: f for f in init.write_agents(p)}
    assert set(files) == set(p.agents)
    wt = "/home/jm/.local/share/delege/worktrees/delegate_70-tests"
    area = {"checks": "proj/checks/n70.py", "tests": "tests/test_x.py", "lint": "proj/video.py",
            "docs": "docs/GLOSSARY.md", "screenshots": "docs/screens/a.png", "fix": "proj/video.py"}
    for name, path in files.items():
        meta = _meta(path)
        assert "permission" not in meta, "OpenCode 1 key: OpenCode 2 ignores V1 fields in a V2 file"
        assert meta["mode"] == "all" and meta["description"] and meta["steps"] > 0
        rules = meta["permissions"]
        assert all(set(r) == {"action", "resource", "effect"} for r in rules)
        assert rules[0] == {"action": "edit", "resource": "*", "effect": "deny"}
        allowed = {r["resource"] for r in rules if r["action"] == "edit" and r["effect"] == "allow"
                   and not r["resource"].startswith("*/")}
        assert allowed == set(p.agents[name].areas), name
        for f in (area[name], f"{wt}/{area[name]}"):
            assert decide(rules, "edit", f) == "allow", (name, f)
        for f in ("proj/remote.py", f"{wt}/proj/remote.py", "/etc/passwd", "opencode.jsonc", f"{wt}/AGENTS.md"):
            if not project.match(f.removeprefix(wt + "/"), p.agents[name].areas):
                assert decide(rules, "edit", f) == "deny", (name, f)
        for cmd in ("git push origin main", "git push", "git commit -m x", "git reset --hard", "git checkout main",
                    "curl https://x.org", "wget x", "pip install x", "rm -rf /", "bash -c 'git push'", "sh x.sh",
                    "sudo ls"):
            assert decide(rules, "shell", cmd) == "deny", (name, cmd)
        for cmd in ("git status", "git diff --stat", "ls", "grep -rn x proj"):
            assert decide(rules, "shell", cmd) == "allow", (name, cmd)
        for action in ("websearch", "external_directory", "subagent", "question", "skill", "execute"):
            assert decide(rules, action, "anything") == "deny", (name, action)
        # the forbidden folders, delege's own files and ssh keys are never read (they could leave the machine)
        for f in (f"{wt}/data/videos/a.mp4", f"{wt}/_dataset/x/result.json", f"{wt}/data/my_set/n.txt",
                  "/home/jm/.config/delege/secrets.json", "~/.config/delege/config.json",
                  "/home/jm/.local/share/delege/usage.json", "/home/jm/.ssh/id_ed25519"):
            assert decide(rules, "read", f) == "deny", (name, f)
        assert decide(rules, "read", f"{wt}/proj/video.py") != "deny", name   # the code: OpenCode's default
        ex = "https://example.org/x/LICENSE"
        assert decide(rules, "webfetch", ex) == ("allow" if name == "screenshots" else "deny"), name
        assert decide(rules, "webfetch", "https://evil.example/x") == "deny", name
        body = path.read_text()
        assert "English" in body and "Do not commit" in body and "bikeshed" in body
    tests_rules = _meta(files["tests"])["permissions"]
    assert decide(tests_rules, "shell", "python -m pytest -q tests/test_x.py") == "allow"
    assert decide(_meta(files["docs"])["permissions"], "shell", "python -m pytest -q x") == "deny"
    shots = _meta(files["screenshots"])["permissions"]
    assert decide(shots, "shell", "python docs/shots.py --all") == "allow"
    assert decide(shots, "shell", "python other.py") == "deny"


def test_init_writes_starter_files_and_keeps_bodies(tmp_path, monkeypatch):
    import subprocess
    r = tmp_path / "newproj"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:me/newproj.git"], cwd=r, check=True)
    lines = []
    p = init.init(r, log=lines.append)
    assert (r / "delege.toml").exists() and (r / "opencode.jsonc").exists()
    assert (r / ".github" / "ISSUE_TEMPLATE" / "delegate.md").exists()
    assert p.name == "newproj" and p.repo == "me/newproj" and set(p.agents) == {"tests", "fix", "lint", "docs"}
    assert 'repo = "me/newproj"' in (r / "delege.toml").read_text()
    assert sorted(f.stem for f in (r / ".opencode" / "agents").glob("*.md")) == ["docs", "fix", "lint", "tests"]
    assert init.stale_agents(p) == []
    # the body (the agent's instructions) is the owner's: kept on the next init; the front matter is refreshed
    f = r / ".opencode" / "agents" / "docs.md"
    head, body = init.split_agent_file(f.read_text())
    f.write_text(head + "\nMy own instructions.\n")
    toml = r / "delege.toml"
    toml.write_text(toml.read_text().replace('areas = ["README.md", "docs/*.md"]', 'areas = ["*.md"]'))
    p2 = project.load(r)
    assert init.stale_agents(p2) == ["docs: .opencode/agents/docs.md does not match delege.toml"]
    init.init(r, log=lines.append)
    head2, body2 = init.split_agent_file(f.read_text())
    assert body2 == "\nMy own instructions.\n" and head2 != head and '"*.md"' in head2
    assert init.stale_agents(project.load(r)) == []
    assert any("kept" in ln and "delege.toml" in ln for ln in lines)
    (f.parent / "lint.md").unlink()
    assert init.stale_agents(project.load(r)) == ["lint: no .opencode/agents/lint.md"]


def test_template_is_what_init_generates(tmp_path):
    """``template/`` (also usable as a GitHub template repository) is exactly ``delege init``'s output for the
    default project: the toml, and the agents regenerated from it."""
    t = ROOT / "template"
    assert (t / "delege.toml").read_text() == init.render_config("myproject", "owner/myproject")
    p = project.parse(__import__("tomllib").loads((t / "delege.toml").read_text()), t, t / "delege.toml")
    for a in p.agents.values():
        f = t / ".opencode" / "agents" / f"{a.name}.md"
        assert f.read_text() == init.front_matter(a, p) + init.body(a, p), f
    assert (t / ".github" / "ISSUE_TEMPLATE" / "delegate.md").read_text() == \
        (ROOT / ".github" / "ISSUE_TEMPLATE" / "delegate.md").read_text()


def test_opencode2_config_is_local_ollama_only(tmp_path):
    cfg = read_jsonc(ROOT / "template" / "opencode.jsonc")
    assert not {"provider", "permission", "autoupdate", "small_model", "share"} & set(cfg)   # V1 keys
    provider, model = cfg["model"].split("/", 1)
    ollama = cfg["providers"]["ollama"]
    assert provider == "ollama" and model in ollama["models"]
    assert ollama["models"][model]["limit"]["context"] >= opencode.MIN_CONTEXT
    assert ollama["settings"]["baseURL"] == "http://localhost:11434/v1"
    assert cfg["update"] == "disable" and cfg["agents"]["title"]["model"] == cfg["model"]
    pol = cfg["policies"]
    assert decide(pol, "provider.use", "openai", "allow") == "deny"
    assert decide(pol, "provider.use", "ollama", "allow") == "allow"
    assert decide(pol, "tool.use", "shell:git push origin main", "allow") == "deny"
    perm = cfg["permissions"]
    assert decide(perm, "shell", "git push") == "deny" and decide(perm, "webfetch", "https://x") == "deny"
    assert decide(perm, "external_directory", "/etc") == "deny"
    assert decide(perm, "read", "/home/jm/.config/delege/secrets.json") == "deny"
    # the per-task copy with cloud models: exactly ollama + the free providers in use, the rest unchanged
    pol2 = opencode.cloud_policies(pol, ["groq", "ollama", "groq"])
    assert decide(pol2, "provider.use", "groq", "allow") == "allow"
    assert decide(pol2, "provider.use", "ollama", "allow") == "allow"
    assert decide(pol2, "provider.use", "openrouter", "allow") == "deny"
    assert decide(pol2, "tool.use", "shell:git push origin main", "allow") == "deny"
    assert [p["resource"] for p in pol2 if p["action"] == "provider.use"] == ["*", "groq", "ollama"]
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / "opencode.jsonc").write_text((ROOT / "template" / "opencode.jsonc").read_text())
    p = opencode.write_task_config(wt, ["ollama", "openrouter"])
    c = read_jsonc(p)
    assert c["model"] == "ollama/qwen3-coder:30b" and "$schema" not in c
    assert decide(c["policies"], "provider.use", "openrouter", "allow") == "allow"
    assert decide(c["policies"], "provider.use", "google", "allow") == "deny"
    assert decide(c["permissions"], "shell", "git push") == "deny"


def test_opencode_version_and_jsonc(tmp_path, monkeypatch):
    assert opencode.parse_version("opencode v2.1.4") == (2, 1, 4) and opencode.parse_version("1.19.4") == (1, 19, 4)
    assert opencode.version_problem("opencode v2.0.0") is None
    assert "upgrade --major" in opencode.version_problem("1.19.4") and "OpenCode 1.19.4" in opencode.version_problem("1.19.4")
    assert "no version" in opencode.version_problem("")
    p = tmp_path / "c.jsonc"
    p.write_text('// comment\n{\n  "a": [1, 2,],\n  // another\n  "b": {"c": "http://x//y",},\n}\n')
    assert read_jsonc(p) == {"a": [1, 2], "b": {"c": "http://x//y"}}
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert opencode.find_opencode() == "opencode"
    (tmp_path / ".opencode" / "bin").mkdir(parents=True)
    (tmp_path / ".opencode" / "bin" / "opencode").write_text("")
    assert opencode.find_opencode() == str(tmp_path / ".opencode" / "bin" / "opencode")
    assert opencode.find_opencode("/opt/oc") == "/opt/oc"
    argv = opencode.opencode_argv("oc", "tests", "do it", "groq/x", json_output=True)
    assert argv == ["oc", "run", "--standalone", "--agent", "tests", "--model", "groq/x", "--format", "json", "do it"]


def test_parse_json_output():
    import json
    lines = [json.dumps({"type": "step_start", "part": {}}),
             json.dumps({"type": "step_finish", "part": {"tokens": {"input": 100, "output": 20, "reasoning": 5,
                                                                    "cache": {"read": 10, "write": 2}}}}),
             json.dumps({"type": "text", "part": {"text": "Changed: x"}}),
             json.dumps({"type": "step_finish", "part": {"tokens": {"input": 50, "output": 1}}}),
             "not json at all",
             json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "Rate limit exceeded",
                                                                                  "statusCode": 429}}})]
    ev = opencode.parse_json_output("\n".join(lines))
    assert ev["steps"] == 2 and ev["input"] == 162 and ev["output"] == 26
    assert ev["text"] == "Changed: x\nnot json at all" and ev["error"] == "Rate limit exceeded" and ev["status"] == 429
    assert opencode.parse_json_output("") == {"steps": 0, "input": 0, "output": 0, "text": "", "error": "", "status": None}


def test_fifoo_example_config_parses(tmp_path):
    """``examples/fifoo.delege.toml``: FiFoo's seven agents, its dataset folders and files; the generated rules
    deny the dataset folders and let only the licences agent fetch model-card sites."""
    import tomllib
    data = tomllib.loads((ROOT / "examples" / "fifoo.delege.toml").read_text())
    p = project.parse(data, tmp_path, tmp_path / "delege.toml")
    assert list(p.agents) == ["checks", "tests", "fix", "lint", "docs", "licences", "screenshots"]
    assert p.repo == "jcherfils/FiFoo" and p.forbidden.dir_names == ("videos", "_dataset", "avatars")
    assert "result.json" in p.forbidden.file_names and ".safetensors" in p.forbidden.suffixes
    assert p.agents["screenshots"].media and p.agents["fix"].strong and p.agents["checks"].strong
    for name, a in p.agents.items():
        rules = [{"action": x, "resource": y, "effect": z} for x, y, z in init.permissions(a, p)]
        assert decide(rules, "read", "/home/jm/FiFoo/data/videos/a/result.json") == "deny"
        hf = "https://huggingface.co/org/model/blob/main/LICENSE"
        assert decide(rules, "webfetch", hf) == ("allow" if name == "licences" else "deny"), name
        assert decide(rules, "shell", "git push") == "deny"
    shots = [{"action": x, "resource": y, "effect": z} for x, y, z in init.permissions(p.agents["screenshots"], p)]
    assert decide(shots, "shell", 'xvfb-run -a -s "-screen 0 1920x1200x24" python docs/make_screenshots.py') == "allow"
    assert decide(shots, "shell", "xvfb-run -a bash") == "deny"
    from delege.checks import task_violations
    from delege.tasks import Task
    assert task_violations(["docs/screens/a.png"], Task("1", "screenshots", "x"), p) == []
    assert task_violations(["fifoo/x.py", "videos/a.mp4"], Task("1", "tests", "x"), p)
