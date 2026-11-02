"""The ``delege`` command: ``init``, ``doctor``, ``plan``, ``run``, ``push``, ``models``, ``check-images``,
``settings``."""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import __version__, cloud, paths
from .machine import load_config, usable_models
from .opencode import find_opencode
from .project import ConfigError, Project
from .project import load as load_project
from .project import repo_root
from .tasks import Task, file_tasks, prompt

HELP = """delege: delegate small, well-scoped coding tasks to headless OpenCode 2 agents on a local model (Ollama) or
free cloud models, spread over your machines.

  delege init                        # starter delege.toml, opencode.jsonc, agents (run again after editing the toml)
  delege doctor [--no-load]          # is everything ready on this machine? one fix line per failure
  delege plan [--tasks FILE] [--only 12,15]
  delege run  [--tasks FILE] [--only 12,15] [--cloud] [--parallel N] [--redo] [--no-push] [--no-comment] [--dry-run]
  delege push delegate/12-tests …    # push reviewed branches (never the base branch, never force)
  delege models [--no-probe]         # the free hosted models usable now
  delege check-images docs/*.png     # an image proof: readable, large enough, not blank
  delege settings [--no-browser]     # the local settings page (keys, models, machines, switches)
"""


def _project(args) -> Project:
    try:
        return load_project(repo_root(getattr(args, "repo", None) or "."))
    except ConfigError as e:
        raise SystemExit(f"{e}") from None


def _config(args, project: Project | None) -> dict:
    from .migrate import migrate_once
    for line in migrate_once():
        print(line)
    return load_config(Path(args.config) if getattr(args, "config", None) else None,
                       project.repo if project else "", project.name if project else "",
                       project.root if project else None)


def _tasks(args, project: Project) -> list[Task]:
    from .github import issues_tasks
    if args.tasks:
        tasks = file_tasks(Path(args.tasks), project.agents)
    else:
        tasks, notes = issues_tasks(project)
        for n in notes:
            print("skipped", n)
    if args.only:
        keep = {s.strip().lstrip("#") for s in args.only.split(",")}
        tasks = [t for t in tasks if t.id in keep]
    return tasks


def list_models(cfg: dict, probe: bool = True, out=print) -> int:
    """``delege models``: one line per hosted model of the pool — usable now or why not — then the parallelism
    cloud mode would give. Exit 1 when none is usable."""
    local = [h for h in cfg["hosts"] if h.local]
    oc = local[0].opencode if local else find_opencode()
    rows = usable_models(cfg, oc, probe=probe)
    n = 0
    for m, st in rows:
        p = cloud.PROVIDERS[m.provider]
        if st["usable"]:
            n += 1
            why = "usable" + (f" — {st['probe']}" if st["probe"] else "")
        elif not st["on"]:
            why = "off" + (" (may train on the prompts: switch it on in `delege settings`)" if m.trains else "")
        elif not st["key"]:
            why = f"no key: export {p.key_env}=…  (create one at {p.key_url}), or `delege settings`"
        elif st["listed"] is False:
            why = "not listed by `opencode models` (opencode reload, or OpenCode 2 has no such model)"
        elif st["exhausted"]:
            why = st["exhausted"]
        else:
            why = f"probe failed: {st['probe']}"
        used = cloud.Usage().counts(m.id)
        out(f"{'ok' if st['usable'] else '--':3} {m.id:44} {m.limits.text():38} key: {st['key'] or '-':8} "
            f"today: {used[0]} req / {used[1]} tok — {why}")
        out(f"    {m.name}; strength {m.strength}{', vision' if m.vision else ''}; data: {m.policy or p.policy} "
            f"({p.policy_url})")
    out("")
    slots = sum(max(1, h.slots) for h in local)
    out(f"{n} usable hosted model(s) → cloud parallelism {n + slots} (+ {slots} local); cloud mode is "
        f"{'on' if cfg.get('cloud') else 'off'} (run --cloud, or the switch in `delege settings`)")
    return 0 if n else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="delege", description=HELP, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--version", action="version", version=f"delege {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="the project's clone (default: the git root around the current folder)")
    common.add_argument("--config", help=f"the machine configuration (default {paths.config_file()})")
    for name in ("plan", "run"):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("--tasks", help="a task file (.json or '<agent>: <task>' lines) instead of the issues")
        p.add_argument("--only", help="task ids / issue numbers, comma-separated")
        p.add_argument("--cloud", action="store_true",
                       help="use the free hosted models too (or the switch in `delege settings`): as many tasks at "
                            "once as usable models")
        p.add_argument("--no-probe", action="store_true", help="do not send each hosted model a 1-token question")
        if name == "run":
            p.add_argument("--dry-run", action="store_true", help="print the plan and the commands, run nothing")
            p.add_argument("--parallel", type=int, default=None,
                           help="tasks at once over all machines (default 1, or config.json \"parallel\"; with "
                                "cloud models: the number of usable models)")
            p.add_argument("--redo", action="store_true",
                           help="run again tasks whose delegate/* branch already exists (deletes that branch first)")
            p.add_argument("--no-push", action="store_true",
                           help="keep successful delegate/* branches here (by default they are pushed, checked)")
            p.add_argument("--no-comment", action="store_true",
                           help="do not comment the issues (by default each finished task comments its issue and "
                                "labels it delegate:done / delegate:failed)")
    p = sub.add_parser("push", parents=[common])
    p.add_argument("branches", nargs="+")
    p = sub.add_parser("check-images")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("doctor", parents=[common], help="check this machine: project, OpenCode 2 and its agents, Ollama, gh, git")
    p.add_argument("--no-load", action="store_true", help="do not load the model to measure its context / GPU share")
    p = sub.add_parser("models", parents=[common], help="the free hosted models: which are usable now")
    p.add_argument("--no-probe", action="store_true", help="do not send each model a 1-token question")
    p = sub.add_parser("init", parents=[common], help="write delege.toml, opencode.jsonc and the agents' files")
    p.add_argument("--force", action="store_true", help="rewrite delege.toml and opencode.jsonc even if present")
    p = sub.add_parser("settings", help="open the local settings page")
    p.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "settings":
        from .migrate import migrate_once
        for line in migrate_once():
            print(line)
        from .settings import serve
        return serve(open_browser=not args.no_browser)
    if args.cmd == "init":
        from .init import init
        init(args.repo or ".", force=args.force)
        return 0
    if args.cmd == "check-images":
        from .images import check_images
        bad = check_images(args.paths)
        print("\n".join(bad) if bad else f"{len(args.paths)} image(s) ok")
        return 1 if bad else 0
    if args.cmd == "doctor":
        from .doctor import Doctor
        from .migrate import migrate_once
        for line in migrate_once():
            print(line)
        return Doctor(repo_root(args.repo or "."), Path(args.config) if args.config else None,
                      load_model=not args.no_load).run()

    project = _project(args)
    cfg = _config(args, project)
    if args.cmd == "models":
        return list_models(cfg, probe=not args.no_probe)

    from .reports import push_branch
    if args.cmd == "push":
        ok = True
        for b in args.branches:
            good, msg = push_branch(b, project)
            print(msg)
            ok &= good
        return 0 if ok else 1

    from .runner import Delegator
    tasks = _tasks(args, project)
    if not tasks:
        print(f"no task (label an issue `{project.label}` and name its agent, or pass --tasks FILE)")
        return 0
    if args.cloud:
        cfg["cloud"] = True
    cfg["probe"] = not args.no_probe
    if args.cmd == "plan" or args.dry_run:
        from .opencode import opencode_argv
        from .plan import plan
        d = Delegator(cfg, project)
        if d.cloud_on():
            lanes = d.cloud_lanes([h for h in cfg["hosts"] if h.local], probe=cfg["probe"])
            print(f"cloud models on: {len(lanes)} task(s) at once — " + ", ".join(ln.name for ln in lanes))
            print("  (each lane takes the heaviest task left; strong agents go to the strongest model running)")
        for t, h in plan(tasks, cfg["hosts"], project):
            where = h.name if h else "— no machine can run it"
            print(f"{t.branch:40} {t.agent:12} on {where:12} {t.title[:60]}")
            if h and args.cmd == "run":
                argv = opencode_argv(h.opencode, t.agent, prompt(t, project), cfg["model"])
                print("    " + shlex.join([*argv[:-1], argv[-1][:120] + "…"]))
        return 0
    if args.parallel:
        cfg["parallel"] = args.parallel
    d = Delegator(cfg, project)
    results = d.run(tasks, redo=args.redo)
    if not args.no_push:
        for r in results:
            if r["status"] == "ok":
                good, msg = push_branch(r["branch"], project)
                r["pushed"] = good
                print(msg)
    from .reports import write_reports
    out = write_reports(results, base=project.base)
    print(f"reports: {out}")
    if not args.no_comment and project.repo:
        from .github import post_outcomes
        for line in post_outcomes(results, project.repo, project.base):
            print(line)
    return 0 if all(r["status"] in ("ok", "no-change", "skipped") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
