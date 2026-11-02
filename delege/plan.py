"""The plan: which machine runs which task (local mode), and the lanes of cloud mode."""
from __future__ import annotations

from .machine import Host
from .project import Project
from .tasks import Task


def plan(tasks: list[Task], hosts: list[Host], project: Project) -> list[tuple[Task, Host | None]]:
    """Heaviest task first, each to the machine that would finish it first given what it already has (its load ÷
    its weight: GPU memory × slots). A task no machine takes (the hosts' ``agents`` lists) gets None. The result
    keeps the tasks' order."""
    cost = {n: a.cost for n, a in project.agents.items()}
    load = {h.name: 0.0 for h in hosts}
    out = []
    for t in sorted(tasks, key=lambda t: -cost.get(t.agent, 1.0)):
        c = cost.get(t.agent, 1.0)
        ok = [h for h in hosts if h.can(t.agent)]
        if not ok:
            out.append((t, None))
            continue
        h = min(ok, key=lambda h: ((load[h.name] + c) / h.weight, hosts.index(h)))
        load[h.name] += c
        out.append((t, h))
    order = {id(t): i for i, t in enumerate(tasks)}
    return sorted(out, key=lambda th: order[id(th[0])])
