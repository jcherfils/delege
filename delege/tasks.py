"""Tasks: from GitHub issues (``github.py``) or a task file, and the prompt an agent gets."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .project import BRANCH_PREFIX, Project


@dataclass
class Task:
    id: str                           # issue number, or a slug for a task-file entry
    agent: str
    title: str
    body: str = ""
    area: str | None = None           # narrows the agent's area (a glob, a folder or a file)
    issue: int | None = None

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.id}-{self.agent}"


def slug(text: str, n: int = 32) -> str:
    """Whole words only, at most ``n`` characters (the first word cut when it alone is longer)."""
    out = ""
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if out and len(out) + 1 + len(w) > n:
            break
        out = f"{out}-{w}" if out else w[:n]
    return out or "task"


def task_from_issue(issue: dict, agents) -> Task | None:
    """An issue → a task, or None when it names no known agent (a label ``agent:<name>`` or a body line
    ``Agent: <name>``; an optional ``Area: <glob>`` line)."""
    labels = [lab["name"] if isinstance(lab, dict) else lab for lab in issue.get("labels") or []]
    body = issue.get("body") or ""
    agent = next((lab.split(":", 1)[1].strip() for lab in labels if lab.lower().startswith("agent:")), None)
    m = re.search(r"^\s*agent:\s*([\w-]+)", body, re.I | re.M)
    agent = agent or (m.group(1) if m else None)
    if agent not in agents:
        return None
    area = re.search(r"^\s*area:\s*(\S+)", body, re.I | re.M)
    return Task(str(issue["number"]), agent, issue.get("title", ""), body, area.group(1) if area else None,
                int(issue["number"]))


def file_tasks(path: Path, agents) -> list[Task]:
    """A task file: JSON (a list of {agent, title | task, body?, area?, id?, issue?}) or text lines
    ``<agent>: <task>`` (``#`` comments)."""
    text = Path(path).read_text()
    out = []
    if Path(path).suffix == ".json":
        for e in json.loads(text):
            title = e.get("title") or e.get("task") or ""
            issue = e.get("issue")
            out.append(Task(str(e.get("id") or issue or "t-" + slug(title)), e["agent"], title, e.get("body", ""),
                            e.get("area"), issue))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            agent, title = (s.strip() for s in line.split(":", 1))
            out.append(Task("t-" + slug(title), agent, title))
    bad = [t.agent for t in out if t.agent not in agents]
    if bad:
        raise SystemExit(f"unknown agent(s) {', '.join(sorted(set(bad)))} — agents: {', '.join(agents)}")
    return out


def prompt(task: Task, project: Project) -> str:
    """The text given to ``opencode run``: the task, where to work, never commit, the report, the project's rules."""
    a = project.agents[task.agent]
    where = task.area or ", ".join(a.areas)
    head = f"Issue #{task.issue}" if task.issue else "Task"
    lines = [f"{head}: {task.title}", ""] + ([task.body.strip(), ""] if task.body.strip() else []) + [
             f"Work only in: {where}.",
             f"Do not commit or push: delege checks your diff and commits it on {task.branch} for the owner's "
             "review. Finish with the report (Changed / Proof / Left)."]
    for line in a.prompt.strip().splitlines():
        if "{issue}" in line and task.issue is None:
            continue
        lines.append(line.replace("{issue}", str(task.issue)).replace("{branch}", task.branch)
                     .replace("{area}", where))
    if project.rules:
        lines += ["", f"Rules of the {project.name} repository (always):", project.rules]
    return "\n".join(lines)
