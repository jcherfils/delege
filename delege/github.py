"""GitHub through ``gh``: the open issues labelled ``delegate``, and the comment + label each finished task leaves."""
from __future__ import annotations

import json
import shutil
import subprocess

from .project import Project
from .tasks import Task, task_from_issue

DONE_LABEL, FAILED_LABEL = "delegate:done", "delegate:failed"
COMMENT_MARK = "<!-- delege -->"


def gh_api(path: str):
    """``gh api PATH`` → JSON (``gh`` must be installed and logged in: ``delege doctor`` checks it)."""
    if not shutil.which("gh"):
        raise SystemExit("gh is not installed: https://cli.github.com (sudo apt install gh), then gh auth login")
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise SystemExit(f"gh api {path}: {(r.stderr or r.stdout).strip()[-400:]} — fix: gh auth login")
    return json.loads(r.stdout)


def issues_tasks(project: Project, gh=None) -> tuple[list[Task], list[str]]:
    """Open issues labelled ``delegate`` → (tasks, notes about the skipped ones). ``gh``: path → items (tests)."""
    if not project.repo:
        raise SystemExit(f"no GitHub repository: set [project] repo = \"owner/name\" in {project.config_path}")
    gh = gh or gh_api
    # GitHub caps a page at 100. Fetch until the last (short) page so projects with
    # more than 100 delegated issues do not silently leave work out of the plan.
    items = []
    page = 1
    while True:
        batch = gh(f"repos/{project.repo}/issues?labels={project.label}&state=open&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise SystemExit("gh api returned an unexpected issue list")
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    tasks, notes = [], []
    for it in items:
        if "pull_request" in it:
            continue
        t = task_from_issue(it, project.agents)
        if t:
            tasks.append(t)
        else:
            notes.append(f"#{it['number']}: no agent (label agent:<name> or a line 'Agent: <name>'; "
                         f"agents: {', '.join(project.agents)})")
    return tasks, notes


def issue_comment(r: dict, base: str = "main") -> str:
    """The comment a finished task leaves on its issue: the outcome, the branch, the files, each proof and the end
    of the agent's own report — enough to review without the machine."""
    ok = r["status"] == "ok"
    head = ("✅ **Done**" if ok else f"❌ **{r['status']}**") + f" — `{r['branch']}` by the `{r['agent']}` agent " \
        f"using `{r.get('engine', 'opencode')}` " \
        f"with `{r.get('model') or 'the local model'}` on {r.get('host')} in {r.get('seconds', 0)} s " \
        f"(base {str(r.get('base', ''))[:12]})."
    lines = [COMMENT_MARK, head, ""]
    if r.get("tokens"):
        tk = r["tokens"]
        lines.append(f"Usage: {tk.get('steps', 0)} requests, {tk.get('input', 0)} tokens in, {tk.get('output', 0)} out.")
    if r.get("pushed"):
        lines.append(f"Branch on GitHub: `{r['branch']}` — review with `git fetch origin {r['branch']} && "
                     f"git log -p {base}..origin/{r['branch']}`.")
    elif ok:
        lines.append(f"Branch only on {r.get('host')} (not pushed): `delege push {r['branch']}`.")
    if r.get("note"):
        lines.append(f"Note: {r['note']}")
    lines += ["", "**Files**: " + (", ".join(f"`{f}`" for f in r.get("files") or []) or "none")]
    if r.get("violations"):
        lines += ["", "**Refused**:"] + [f"- {v}" for v in r["violations"]]
    for p in r.get("proof") or []:
        lines.append(f"- proof `{p['cmd'][:160]}` → {'ok' if p['rc'] == 0 else 'failed'}")
    if r.get("output"):
        lines += ["", "<details><summary>Agent report (end)</summary>", "", "```", r["output"][-3000:], "```",
                  "", "</details>"]
    return "\n".join(lines)


def post_outcomes(results: list[dict], repo: str, base: str = "main", run=subprocess.run) -> list[str]:
    """Comments each finished issue task and labels it ``delegate:done`` / ``delegate:failed`` (with ``gh``).
    Skipped tasks get nothing. Returns what was done, one line each."""
    if not shutil.which("gh") and run is subprocess.run:
        return ["gh not found: no comment posted on the issues"]
    out = []
    for r in results:
        if not r.get("issue") or r["status"] == "skipped":
            continue
        n, ok = r["issue"], r["status"] == "ok"
        c = run(["gh", "api", f"repos/{repo}/issues/{n}/comments", "-f", f"body={issue_comment(r, base)}"],
                capture_output=True, text=True, timeout=60)
        add, drop = (DONE_LABEL, FAILED_LABEL) if ok else (FAILED_LABEL, DONE_LABEL)
        run(["gh", "api", f"repos/{repo}/issues/{n}/labels", "-f", f"labels[]={add}"],
            capture_output=True, text=True, timeout=60)
        run(["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/{n}/labels/{drop}"],
            capture_output=True, text=True, timeout=60)
        out.append(f"#{n}: {'commented, ' + add if c.returncode == 0 else 'comment failed: ' + (c.stderr or '')[-200:]}")
    return out
