"""The diff guard: which changed files a task may commit, and which files may be pushed to GitHub."""
from __future__ import annotations

from pathlib import PurePosixPath

from .project import MODEL_SUFFIXES, VIDEO_SUFFIXES, Project, in_area, match, suffix


def _forbidden_path(path: str, project: Project) -> bool:
    f = project.forbidden
    p = PurePosixPath(path)
    if p.name in f.file_names or set(f.dir_names) & set(p.parts[:-1]):
        return True
    return any(path == d or path.startswith(d + "/") or match(path, [d]) for d in f.paths)


def content_violations(files, project: Project, media_ok: bool = False) -> list[str]:
    """Files that never go into the repository: the forbidden suffixes (models, arrays, videos…), the forbidden
    folders and file names (datasets…), and media unless ``media_ok``."""
    f = project.forbidden
    out = []
    for path in files:
        suf = suffix(path)
        if suf in f.suffixes:
            kind = ("model / array file" if suf in MODEL_SUFFIXES else "video / audio" if suf in VIDEO_SUFFIXES
                    else "forbidden file type")
            out.append(f"{path}: {kind}")
        elif suf in f.media_suffixes and not media_ok:
            out.append(f"{path}: image / media")
        elif _forbidden_path(path, project):
            out.append(f"{path}: forbidden path (data)")
    return out


def area_violations(files, project: Project, agent: str, area: str | None = None) -> list[str]:
    globs = project.agents[agent].areas
    out = [f"{f}: outside the {agent} agent's area ({', '.join(globs)})" for f in files if not match(f, globs)]
    if area:
        out += [f"{f}: outside the task's area {area}" for f in files if match(f, globs) and not in_area(f, area)]
    return out


def task_violations(files, task, project: Project) -> list[str]:
    a = project.agents[task.agent]
    media = [f for f in files if suffix(f) in project.forbidden.media_suffixes]
    return (area_violations(files, project, task.agent, task.area)
            + content_violations(files, project, media_ok=a.media and all(match(f, a.areas) for f in media)))


def push_violations(files, project: Project, sizes: dict | None = None) -> list[str]:
    """What may go to GitHub: configs, reports and code only — no forbidden file, no media, nothing larger than
    ``max_file_bytes``."""
    out = content_violations(files, project)
    out += [f"{f}: {n} bytes (> {project.max_file_bytes})" for f, n in (sizes or {}).items()
            if n > project.max_file_bytes]
    return out
