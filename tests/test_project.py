"""delege.toml / [tool.delege]: parsing, errors, proof placeholders, areas; tasks from issues and files; the
prompt; the diff guard and the push guard."""
from __future__ import annotations

import json
import sys

import pytest

from delege import checks, project
from delege.github import issues_tasks
from delege.tasks import Task, file_tasks, prompt

from .conftest import PROJECT_TOML


def test_load_toml_and_pyproject(tmp_path):
    (tmp_path / "delege.toml").write_text(PROJECT_TOML)
    p = project.load(tmp_path)
    assert p.name == "proj" and p.repo == "o/r" and p.base == "main" and p.config_path.name == "delege.toml"
    assert set(p.agents) == {"tests", "fix", "lint", "docs", "checks", "screenshots"}
    assert p.agents["fix"].cost == 1.5 and p.agents["fix"].strong and p.agents["screenshots"].media
    assert p.forbidden.dir_names == ("videos", "_dataset") and p.forbidden.paths == ("data/my_set",)
    assert ".pt" in p.forbidden.suffixes and ".png" in p.forbidden.media_suffixes
    assert "bikeshed" in p.rules
    # [tool.delege] in pyproject.toml, when there is no delege.toml
    (tmp_path / "delege.toml").unlink()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n[tool.delege.project]\nname = "x"\n'
                                              '[tool.delege.agents.docs]\nareas = ["*.md"]\n')
    p = project.load(tmp_path)
    assert p.config_path.name == "pyproject.toml" and list(p.agents) == ["docs"] and p.name == "x"
    assert p.forbidden.suffixes == project.MODEL_SUFFIXES + project.VIDEO_SUFFIXES   # the defaults


def test_config_errors(tmp_path):
    with pytest.raises(project.ConfigError, match="delege init"):
        project.load(tmp_path)
    for text, msg in (('[agents.docs]\nproof = ["x"]\n', "areas"),
                      ('[agents.Docs]\nareas = ["*"]\n', "lower-case"),
                      ('[agents.docs]\nareas = ["*"]\nnope = 1\n', "unknown key"),
                      ('[project]\nname = "x"\n', "no agent"),
                      ('[agents.docs]\nareas = "*.md"\n[forbidden]\nsuffixes = 3\n', "list of strings")):
        (tmp_path / "delege.toml").write_text(text)
        with pytest.raises(project.ConfigError, match=msg):
            project.load(tmp_path)


def test_proof_placeholders(tmp_path):
    (tmp_path / "delege.toml").write_text(PROJECT_TOML)
    p = project.load(tmp_path)
    py = sys.executable
    files = ["tests/test_a.py", "proj/x.py", "README.md"]
    assert project.proof_commands(p.agents["tests"], files, 12, py) == \
        [[py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_a.py"]]
    assert project.proof_commands(p.agents["tests"], ["README.md"], None, py) == []     # nothing matches: skipped
    # {changed:GLOB!}: a fix without a test is a failing command, not a skipped one
    cmds = project.proof_commands(p.agents["fix"], ["proj/x.py"], None, py)
    assert cmds[0] == [py, "-c", "print(1)", "proj/x.py"]
    assert cmds[1][:2] == [py, "-c"] and "sys.exit" in cmds[1][2]
    # {issue}: skipped without an issue; {delege}: this Python's module
    assert project.proof_commands(p.agents["checks"], files, None, py) == []
    assert project.proof_commands(p.agents["checks"], files, 51, py) == [[py, "-c", "print(51)"]]
    assert project.proof_commands(p.agents["screenshots"], ["docs/screens/a.png"], None, py) == \
        [[py, "-m", "delege", "check-images", "docs/screens/a.png"]]
    assert project.proof_commands(p.agents["docs"], files, 1, py) == []


def test_github_repo_from_remote(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert project.github_repo(tmp_path) == ""
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:owner/thing.git"], cwd=tmp_path, check=True)
    assert project.github_repo(tmp_path) == "owner/thing"
    subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/owner/thing"], cwd=tmp_path, check=True)
    assert project.github_repo(tmp_path) == "owner/thing"
    (tmp_path / "delege.toml").write_text('[agents.docs]\nareas = ["*.md"]\n')
    assert project.load(tmp_path).repo == "owner/thing"                   # filled from the remote


# ------------------------------------------------------------------ tasks
@pytest.fixture
def proj(tmp_path):
    (tmp_path / "delege.toml").write_text(PROJECT_TOML)
    return project.load(tmp_path)


def test_tasks_from_issues_label_or_body_line(proj):
    items = [
        {"number": 12, "title": "Tests for budget", "labels": [{"name": "delegate"}, {"name": "agent:tests"}],
         "body": "Add tests for proj/budget.py\nArea: tests/test_budget.py"},
        {"number": 13, "title": "Lint ui", "labels": [{"name": "delegate"}], "body": "Agent: lint\nArea: proj/ui/*"},
        {"number": 14, "title": "Vague", "labels": [{"name": "delegate"}], "body": "do something"},
        {"number": 15, "title": "a PR", "labels": [], "body": "", "pull_request": {}},
    ]
    seen = []
    tasks, notes = issues_tasks(proj, gh=lambda p: seen.append(p) or items)
    assert seen == ["repos/o/r/issues?labels=delegate&state=open&per_page=100"]
    assert [(t.id, t.agent, t.area, t.issue) for t in tasks] == [
        ("12", "tests", "tests/test_budget.py", 12), ("13", "lint", "proj/ui/*", 13)]
    assert tasks[0].branch == "delegate/12-tests"
    assert len(notes) == 1 and notes[0].startswith("#14")
    proj.repo = ""
    with pytest.raises(SystemExit, match="no GitHub repository"):
        issues_tasks(proj, gh=lambda p: items)


def test_task_file_text_and_json(tmp_path, proj):
    txt = tmp_path / "tasks.txt"
    txt.write_text("# comment\n\ntests: Add tests for proj/dataset/budget.py\ndocs: README rows for Where it writes\n")
    ts = file_tasks(txt, proj.agents)
    assert [(t.agent, t.id) for t in ts] == [("tests", "t-add-tests-for-proj-dataset"),
                                             ("docs", "t-readme-rows-for-where-it-writes")]
    js = tmp_path / "tasks.json"
    js.write_text(json.dumps([{"agent": "lint", "title": "Lint ui", "id": "ui7", "area": "proj/ui/*"},
                              {"agent": "checks", "title": "Check", "issue": 51}]))
    ts = file_tasks(js, proj.agents)
    assert [t.branch for t in ts] == ["delegate/ui7-lint", "delegate/51-checks"]
    assert ts[0].area == "proj/ui/*" and ts[1].issue == 51
    txt.write_text("nobody: do it\n")
    with pytest.raises(SystemExit):
        file_tasks(txt, proj.agents)


def test_prompt_says_area_rules_and_never_commit(proj):
    t = Task("51", "checks", "Sharpness", "body text", issue=51)
    p = prompt(t, proj)
    assert "Issue #51: Sharpness" in p and "python -m proj.checks 51" in p
    assert "Do not commit or push" in p and "proj/checks/*" in p and "bikeshed" in p
    assert "Work only in: proj/ui/*." in prompt(Task("x", "lint", "t", area="proj/ui/*"), proj)
    assert "proj.checks" not in prompt(Task("x", "checks", "t"), proj)          # no issue: the {issue} line dropped


# ------------------------------------------------------------------ diff guard
def test_area_and_content_violations(proj):
    t = Task("1", "tests", "x")
    assert checks.task_violations(["tests/test_a.py"], t, proj) == []
    bad = checks.task_violations(["proj/x.py", "tests/w.pt", "tests/data/videos/a/result.json", "tests/f.png"], t, proj)
    assert any("proj/x.py: outside" in b for b in bad)
    assert any("w.pt: model" in b for b in bad) and any("forbidden path" in b for b in bad)
    assert any("f.png: image" in b for b in bad)
    t2 = Task("2", "lint", "x", area="proj/ui/*")
    assert checks.task_violations(["proj/ui/app.py"], t2, proj) == []
    assert "outside the task's area" in checks.task_violations(["proj/remote.py"], t2, proj)[0]
    s = Task("3", "screenshots", "x")
    assert checks.task_violations(["docs/screens/overview.png"], s, proj) == []
    assert checks.task_violations(["docs/x.png"], s, proj)
    assert checks.push_violations(["docs/screens/overview.png"], proj)
    assert checks.push_violations(["docs/NOTES.md", "proj/x.py"], proj, {"proj/x.py": 10}) == []
    assert checks.push_violations(["configs/a.json"], proj, {"configs/a.json": proj.max_file_bytes + 1})
    assert checks.push_violations(["data/my_set/notes.txt"], proj)
    assert checks.push_violations(["any/where/result.json"], proj)
    for f in ("m.onnx", "w.safetensors", "a.npz", "clip.mp4", "x.npy"):
        assert checks.push_violations([f], proj), f
