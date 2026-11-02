"""One-time migration from FiFoo's ``tools/delegate.py`` files: ``~/.fifoo/delegate.json`` (machines…),
``~/.fifoo/cloud.json`` (the switch, models on / off), ``~/.fifoo/secrets.json`` (keys), the OS keyring entries of
service ``fifoo-cloud`` and ``~/.fifoo/delegate/usage.json``. Runs only while ``~/.config/delege/config.json`` does
not exist; the old files are read, never changed."""
from __future__ import annotations

import os

from . import cloud, paths
from .util import read_json, warn_once, write_json

FIFOO_REPO = "jcherfils/FiFoo"
MACHINE_KEYS = ("identity", "model", "timeout_min", "parallel", "ollama_url", "ssh", "models")


def migrate_once(log=None) -> list[str]:
    """Writes ``config.json`` (and ``secrets.json``, ``usage.json``) from the old files when there are some and no
    ``config.json`` yet. Returns what was done, one line each (empty when nothing was)."""
    if paths.config_file().exists():
        return []
    old = paths.legacy_dir()
    dj, cj = old / "delegate.json", old / "cloud.json"
    sj, uj = old / "secrets.json", old / "delegate" / "usage.json"
    if not any(p.exists() for p in (dj, cj, sj, uj)):
        return []
    out = []
    cfg: dict = {"migrated_from": str(old)}
    d = read_json(dj, {})
    d = d if isinstance(d, dict) else {}
    if d:
        repo = d.get("repo") or FIFOO_REPO
        hosts = []
        for h in d.get("hosts") or []:
            if not isinstance(h, dict):
                continue
            h = dict(h)
            if h.get("repo") and h.get("ssh"):
                h["repos"] = {repo: h.pop("repo")}           # the clone of that project on that machine
            else:
                h.pop("repo", None)                          # this machine: the project delege runs in
            hosts.append(h)
        if hosts:
            cfg["hosts"] = hosts
        cfg.update({k: d[k] for k in MACHINE_KEYS if k in d})
        if d.get("cloud") is not None:
            cfg["cloud"] = bool(d["cloud"])
        out.append(f"{dj}: machines and settings → {paths.config_file()}")
        if d.get("dataset_dirs"):
            out.append(f"  dataset_dirs {d['dataset_dirs']} are per project now: add them to delege.toml "
                       "[forbidden] paths")
    c = read_json(cj, {})
    if isinstance(c, dict) and c:
        if "cloud" not in cfg and "cloud" in c:
            cfg["cloud"] = bool(c["cloud"])
        if isinstance(c.get("models"), dict):
            cfg["enabled"] = {str(k): bool(v) for k, v in c["models"].items()}
        out.append(f"{cj}: cloud switch and models on / off → {paths.config_file()}")
    write_json(paths.config_file(), cfg)
    s = read_json(sj, {})
    if isinstance(s, dict) and s:
        cur = cloud._secrets()
        cur.update({k: v for k, v in s.items() if isinstance(v, str) and v and k not in cur})
        cloud._write_secrets(cur)
        out.append(f"{sj}: {len(s)} key(s) → {paths.secrets_file()} (0600)")
    kr = cloud._keyring()
    if kr is not None:
        n = 0
        for p in cloud.PROVIDERS.values():
            try:
                k = kr.get_password(cloud.LEGACY_KEYRING_SERVICE, p.id)
                if k and not kr.get_password(cloud.KEYRING_SERVICE, p.id):
                    kr.set_password(cloud.KEYRING_SERVICE, p.id, k)
                    n += 1
            except Exception as e:                            # noqa: BLE001  (no keyring backend: nothing to copy)
                warn_once("migrate:keyring", "the OS keyring cannot be read for the migration", e)
                break
        if n:
            out.append(f"keyring: {n} key(s) of service {cloud.LEGACY_KEYRING_SERVICE} → {cloud.KEYRING_SERVICE}")
    if uj.exists() and not paths.usage_file().exists():
        u = read_json(uj, {})
        if isinstance(u, dict):
            write_json(paths.usage_file(), u)
            out.append(f"{uj}: today's usage and benched models → {paths.usage_file()}")
    out.insert(0, f"migrated FiFoo's delegate settings from {old} (read once; the old files are left as they are)")
    for line in out:
        if log:
            log(line)
    os.environ.get("DELEGE_HOME")                            # (nothing else to do)
    return out
