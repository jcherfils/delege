"""Reports under ``~/.local/share/delege/reports/<time>/`` and the push guard (only ``delegate/*`` branches, never
the base branch, never force, only checked files)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import paths
from .checks import push_violations
from .project import BRANCH_PREFIX, Project
from .runner import git


def write_reports(results: list[dict], out: Path | None = None, base: str = "main") -> Path:
    out = out or paths.reports_dir() / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, indent=1))
    for r in results:
        lines = [f"# {r['branch']} — {r['status']}", "",
                 f"- Task: {r.get('title', '')}" + (f" (issue #{r['issue']})" if r.get("issue") else ""),
                 f"- Agent: {r['agent']} · model: {r.get('model', '')} · machine: {r.get('host')} · "
                 f"base: {str(r.get('base', ''))[:12]} · {r.get('seconds', 0)} s"]
        if r.get("note"):
            lines.append(f"- Note: {r['note']}")
        if r.get("worktree") and r["status"] in ("failed", "refused"):
            lines.append(f"- Worktree kept: {r['worktree']} (git worktree remove --force … when done)")
        lines += ["", "## Files", ""] + ([f"- {f}" for f in r.get("files") or []] or ["- (none)"])
        if r.get("violations"):
            lines += ["", "## Refused", ""] + [f"- {v}" for v in r["violations"]]
        for p in r.get("proof") or []:
            lines += ["", f"## Proof: `{p['cmd']}` → {'ok' if p['rc'] == 0 else 'failed'}", "", "```", p["out"], "```"]
        if r.get("output"):
            lines += ["", "## Agent output (end)", "", "```", r["output"], "```"]
        lines += ["", f"Review: `git log -p {base}..{r['branch']}`; then merge it yourself, or "
                  f"`delege push {r['branch']}` to see it on GitHub."]
        (out / (r["branch"].replace("/", "_") + ".md")).write_text("\n".join(lines) + "\n")
    return out


def branch_files(branch: str, base: str, repo_dir: Path) -> tuple[list[str], dict]:
    mb = git("merge-base", base, branch, cwd=repo_dir)
    files = [f for f in git("diff", "--name-only", "--no-renames", "--diff-filter=AMCR", mb, branch,
                            cwd=repo_dir).splitlines() if f]
    sizes = {}
    if files:
        for line in git("ls-tree", "-r", "-l", branch, "--", *files, cwd=repo_dir).splitlines():
            meta, path = line.split("\t", 1)
            n = meta.split()[3]
            sizes[path] = int(n) if n.isdigit() else 0
    return files, sizes


def push_branch(branch: str, project: Project) -> tuple[bool, str]:
    """Pushes one reviewed ``delegate/*`` branch: never the base branch, never force, only checked files."""
    if not branch.startswith(BRANCH_PREFIX) or branch.split("/")[-1] in ("main", "master", project.base):
        return False, f"{branch}: only {BRANCH_PREFIX}* branches are pushed"
    files, sizes = branch_files(branch, project.base, project.root)
    bad = push_violations(files, project, sizes)
    if bad:
        return False, f"{branch}: refused — " + "; ".join(bad)
    r = subprocess.run(["git", "push", project.remote, f"refs/heads/{branch}:refs/heads/{branch}"],
                       cwd=str(project.root), capture_output=True, text=True)
    return r.returncode == 0, f"{branch}: " + ("pushed" if r.returncode == 0 else (r.stderr or r.stdout).strip())
