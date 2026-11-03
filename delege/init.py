"""``delege init``: a starter ``delege.toml``, ``opencode.jsonc``, the issue template, and the agents'
``.opencode/agents/<name>.md`` files whose OpenCode 2 permissions are generated from the agents' areas, shell
commands and the project's forbidden paths (the files of ``template/`` in this package, the agents rendered)."""
from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

from .project import Agent, Project, github_repo, load, repo_root

READ_ONLY_SHELL = ("git status *", "git diff *", "git log *", "git show *", "ls *", "grep *", "rg *", "wc *",
                   "cat *", "head *", "tail *", "find *")
# a `python -m pytest *` rule also allows how agents write it (python3, pytest, xvfb-run …)
PYTHON_ALIASES = {"python -m pytest *": ("python3 -m pytest *", "pytest *"),
                  "xvfb-run -a python -m pytest *": ("xvfb-run -a python3 -m pytest *", "xvfb-run -a pytest *"),
                  "ruff check *": ("python -m ruff check *", "python3 -m ruff check *")}
DENIED_SHELL = ("git push *", "git commit *", "git reset *", "git checkout *", "git switch *", "git rebase *",
                "git worktree *", "git stash *", "rm -rf *", "curl *", "wget *", "pip *", "uv *", "npm *", "sudo *")
DENIED_ACTIONS = ("websearch", "external_directory", "subagent", "question", "skill", "execute",
                  "experimental_image_generate")
PRIVATE_READS = ("*/.config/delege/*", "~/.config/delege/*", "*/.local/share/delege/usage.json",
                 "*/.local/share/delege/reports/*", "*/.ssh/*", "~/.ssh/*")

RULES = """## Rules of the {name} repository (always)

- Everything you write is in **English**: code, comments, docstrings, documentation, reports.
- Work **only** in the files of your area (listed above). Anything else is refused by delege, which checks the diff
  before it commits: the whole task is then lost.
- **Do not commit, push, rebase, reset or switch branches.** You are in a git worktree made for this task; delege
  commits your changes on the branch `delegate/<task>-<agent>` and the owner reviews them. Never force anything.
- Never add trained models, data files, images or videos to the repository (delege refuses them).
- No network beyond what your area says. No package installs. No cache written into the repository.
- Keep the change **small**, and prove it: a test, a lint run or a check that passes. If the task is fuzzy or too
  big, do the smallest useful part and say what is left.
{rules}
## End of the task

Finish with a short report (it goes into the owner's review):

```
Changed: <files, one line each, why>
Proof: <the command you ran and its result>
Left: <what is not done, doubts, anything to check by hand>
```
"""

BODIES = {
    "tests": """You are the `tests` agent of {name}, run headless by delege on a local or a free hosted model.

You add **tests for one module** named in the task. You never change the module under test: if a test shows a
bug, mark it `@pytest.mark.xfail(reason="...")` and describe the bug in your report.

1. Read the module and the tests that exist for it (`tests/test_<module>.py`) and `tests/conftest.py` when there
   is one (fixtures).
2. Add tests to the existing file of that module when there is one, else `tests/test_<module>.py`. Test pure
   functions first; fake anything slow or external as the other tests do; no network, no display.
3. Run `python -m pytest -q <your test file>` until it passes, then `ruff check <your test file>`.

Your area: {areas}.
""",
    "fix": """You are the `fix` agent of {name}, run headless by delege on a local or a free hosted model.

You make **one small code change** described by the task: a bug fix or a gap named precisely (file, function,
expected behaviour). The task's `Area:` says which files you may change; stay inside it.

1. Read the files named in the task, the code that calls them (`grep`), and their tests.
2. **Write the test first**: a test that fails on the current code and states the expected behaviour (in the
   existing test file of that area, or a new `tests/test_<topic>.py`).
3. Make the smallest change that makes it pass. No refactoring, no renaming, no new dependency, no change of
   behaviour beyond the task.
4. Run `python -m pytest -q <the test files of that area>` and `ruff check <the files you changed>` until both
   pass.

If the task turns out bigger than described (several modules, a design choice), do nothing risky: write what
you found in the report under `Left:` and stop.

Your area: {areas} (narrowed by the task's `Area:`).
""",
    "lint": """You are the `lint` agent of {name}, run headless by delege on a local or a free hosted model.

You fix **ruff findings in one area** (a folder or files named in the task).

1. `ruff check <area>` (the repository's settings are in `pyproject.toml`), then the rules the task names.
2. Fix by hand or with `ruff check --fix <area>`; never `--unsafe-fixes`, never reformat whole files, never change
   behaviour. Unused names that are part of an API (`__all__`, re-exports) stay, with `# noqa: F401`.
3. Run the tests of that area (`python -m pytest -q tests/test_<area>.py`) and report ruff's counts before / after.

Your area: {areas}, narrowed by the task.
""",
    "docs": """You are the `docs` agent of {name}, run headless by delege on a local or a free hosted model.

You update **documentation from the code**: a README section, a table row, a page under `docs/`.

1. Read the code the task names; the documentation must say what the code does, nothing more. Quote names
   (functions, settings, files) exactly as in the code.
2. Change only the rows / paragraphs the task is about; keep the file's style.
3. Proof: `git diff` shows only those rows; every name you wrote exists (`grep` it in the code).

Your area: {areas}.
""",
}

GENERIC_BODY = """You are the `{agent}` agent of {name}, run headless by delege on a local or a free hosted model.

{description}

Your area: {areas}.
"""


def _rule(action: str, resource: str, effect: str) -> str:
    return f'  - {{action: {action}, resource: {json.dumps(resource)}, effect: {effect}}}'


def permissions(agent: Agent, project: Project) -> list[tuple[str, str, str]]:
    """The OpenCode 2 rule list of an agent: ``(action, resource, effect)`` in order (the last match wins) — deny
    every edit, then allow the areas (relative and absolute); deny every shell command, then allow the read-only
    ones and the agent's own, then deny the dangerous ones again; never read the forbidden folders, delege's own
    files or ssh keys; no network but the agent's ``webfetch`` list; no sub-agent, question, skill, execution."""
    rules = [("edit", "*", "deny")]
    for a in agent.areas:
        rules += [("edit", a, "allow"), ("edit", f"*/{a}", "allow")]
    rules.append(("shell", "*", "deny"))
    rules += [("shell", c, "allow") for c in READ_ONLY_SHELL]
    rules += [("shell", c, "allow") for c in agent.shell]
    rules += [("shell", a, "allow") for c in agent.shell for a in PYTHON_ALIASES.get(c, ())]
    rules += [("shell", c, "deny") for c in DENIED_SHELL]
    f = project.forbidden
    rules += [("read", f"*/{d}/*", "deny") for d in f.dir_names]
    rules += [("read", f"*/{p.rstrip('/')}/*", "deny") for p in f.paths if not any(ch in p for ch in "*?")]
    rules += [("read", p, "deny") for p in PRIVATE_READS]
    rules.append(("webfetch", "*", "deny"))
    rules += [("webfetch", u, "allow") for u in agent.webfetch]
    rules += [(a, "*", "deny") for a in DENIED_ACTIONS]
    return rules


def front_matter(agent: Agent, project: Project) -> str:
    lines = ["---", f"description: {json.dumps(agent.description or f'The {agent.name} agent of {project.name}')}",
             "mode: all", f"steps: {agent.steps}", "permissions:",
             "  # OpenCode 2 rules, generated by delege from delege.toml (the last matching rule wins): do not edit",
             "  # by hand — change delege.toml and run `delege init` again."]
    lines += [_rule(*r) for r in permissions(agent, project)]
    return "\n".join(lines) + "\n---\n"


def body(agent: Agent, project: Project) -> str:
    areas = ", ".join(f"`{a}`" for a in agent.areas)
    tmpl = BODIES.get(agent.name, GENERIC_BODY)
    text = tmpl.format(name=project.name, agent=agent.name, areas=areas,
                       description=agent.description or "Do the task as described.")
    rules = ("\n".join(f"- {ln.lstrip('- ').strip()}" for ln in project.rules.splitlines() if ln.strip())
             + "\n") if project.rules else ""
    return "\n" + text + "\n" + RULES.format(name=project.name, rules=rules)


def split_agent_file(text: str) -> tuple[str, str]:
    """(front matter including the ``---`` lines, the body) of an agent file; ("", text) when it has none."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[:end + 5], text[end + 5:]
    return "", text


def write_agents(project: Project, keep_bodies: bool = True) -> list[Path]:
    """Writes (or refreshes) ``.opencode/agents/<name>.md`` for every agent of ``delege.toml``: the front matter is
    always regenerated; an existing body is kept (``keep_bodies``) — it is the agent's instructions, yours to edit."""
    project.agents_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for a in project.agents.values():
        p = project.agents_dir / f"{a.name}.md"
        old_body = split_agent_file(p.read_text())[1] if p.exists() and keep_bodies else None
        p.write_text(front_matter(a, project) + (old_body if old_body and old_body.strip() else body(a, project)))
        out.append(p)
    return out


def stale_agents(project: Project) -> list[str]:
    """The agents whose file is missing or whose front matter no longer matches ``delege.toml`` (``delege init``
    refreshes them), one line each."""
    out = []
    for a in project.agents.values():
        p = project.agents_dir / f"{a.name}.md"
        if not p.exists():
            out.append(f"{a.name}: no {p.relative_to(project.root)}")
        elif split_agent_file(p.read_text())[0] != front_matter(a, project):
            out.append(f"{a.name}: {p.relative_to(project.root)} does not match delege.toml")
    return out


def render_config(name: str, repo: str) -> str:
    """A commented starter ``delege.toml`` (the generic agents ``tests``, ``fix``, ``lint``, ``docs``)."""
    return f'''# delege: which agents may change what, and what proves their work.  https://github.com/jcherfils/delege
# Run `delege init` again after changing it: the agents' OpenCode permissions (.opencode/agents/*.md) are
# generated from the areas and the forbidden paths below.

[project]
name = "{name}"
repo = "{repo}"                   # GitHub owner/name: the open issues labelled `delegate` are the tasks
base = "main"                      # the branch the worktrees start from; never pushed by delege
# label = "delegate"
# max_file_bytes = 2000000         # a pushed file larger than this is refused
rules = """
- Caches and data only under the user's home folder, never in the repository.
"""

[forbidden]
# never committed nor pushed (the default list covers models, arrays, videos, audio)
suffixes = [".pt", ".pth", ".ckpt", ".onnx", ".safetensors", ".npz", ".npy", ".bin", ".gguf", ".pkl", ".h5",
            ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wav", ".mp3"]
# images: only an agent with `media = true`, inside its area; never pushed by delege
media_suffixes = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"]
dir_names = []                     # folders of that name anywhere: never read by agents, never committed
file_names = []
paths = []                         # repository-relative folders (datasets, secrets)

# Each agent: `areas` (globs; `*` matches `/` too), `proof` (commands delege runs in the worktree after the agent;
# placeholders {{python}} {{delege}} {{issue}} {{changed}} {{changed:GLOB|GLOB}} — see the README), `cost` (relative
# time, for the plan), `strong` (goes to the strongest cloud model), `media`, `shell` (commands the agent may run
# besides the read-only ones), `webfetch` (URL patterns it may fetch), `steps`.

[agents.tests]
description = "Adds pytest tests for one module (no change to the module itself)"
areas = ["tests/*"]
proof = ["{{python}} -m pytest -q -p no:cacheprovider {{changed:tests/*test_*.py}}"]
shell = ["python -m pytest *", "ruff check *"]

[agents.fix]
description = "Fixes one small, well-described bug or gap, with a test that proves it"
areas = ["{name}/*", "src/*", "tests/*"]
proof = ["ruff check {{changed:*.py}}", "{{python}} -m pytest -q -p no:cacheprovider {{changed:tests/*test_*.py!}}"]
cost = 1.5
strong = true
shell = ["python -m pytest *", "ruff check *"]

[agents.lint]
description = "Fixes ruff findings in one area, no behaviour change"
areas = ["*.py"]
proof = ["ruff check --select F {{changed:*.py}}"]
shell = ["python -m pytest *", "ruff check *"]

[agents.docs]
description = "Updates documentation from the code"
areas = ["README.md", "docs/*.md"]
cost = 0.5
'''


def template_dir() -> Path:
    """The ``template/`` folder: in the package when installed, beside it in a checkout."""
    p = Path(str(resources.files("delege"))) / "template"
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[1] / "template"


def init(root: Path | str = ".", force: bool = False, log=print) -> Project:
    """Writes the starter files that are missing (``delege.toml`` rendered for this repository; ``opencode.jsonc``
    and the issue template copied from ``template/``), then the agents' files from the loaded ``delege.toml``
    (front matter always regenerated, bodies kept). ``force`` rewrites ``delege.toml`` and ``opencode.jsonc``."""
    root = repo_root(root)
    toml = root / "delege.toml"
    if force or not toml.exists():
        name = root.name
        toml.write_text(render_config(name, github_repo(root) or f"owner/{name}"))
        log(f"wrote {toml}")
    else:
        log(f"kept {toml}")
    for rel in ("opencode.jsonc", ".github/ISSUE_TEMPLATE/delegate.md"):
        src, dst = template_dir() / rel, root / rel
        if force or not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
            log(f"wrote {dst}")
    project = load(root)
    for p in write_agents(project):
        log(f"wrote {p}")
    log("next: delege doctor, then label an issue `delegate` + `agent:<name>` and run `delege run`")
    return project
