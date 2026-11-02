# Changelog

## 0.1.0 — 2026-11-02

First release, extracted from FiFoo's `tools/delegate.py` and `fifoo/cloud.py` and made project-agnostic.

- `delege` command: `init`, `doctor`, `plan`, `run` (`--only`, `--redo`, `--parallel`, `--cloud`, `--no-push`,
  `--no-comment`, `--dry-run`, `--tasks`), `push`, `models`, `check-images`, `settings`.
- Per-project `delege.toml` (or `[tool.delege]` in `pyproject.toml`): agents with areas, proofs (placeholders
  `{python}`, `{delege}`, `{issue}`, `{changed:GLOB!}`), cost, strong, media, shell and webfetch lists; forbidden
  suffixes, folders, file names and paths; the rules text injected into every prompt.
- `delege init`: a starter `delege.toml`, `opencode.jsonc`, issue template and generic agents (`tests`, `fix`,
  `lint`, `docs`) whose OpenCode 2 permissions are generated from the configuration; bodies kept on re-runs.
- Machine configuration in `~/.config/delege/config.json`; keys in the OS keyring or `secrets.json` (0600); state
  in `~/.local/share/delege`; a one-time migration of FiFoo's `~/.fifoo/delegate.json`, `cloud.json`,
  `secrets.json` and usage file.
- `delege settings`: a local web page (127.0.0.1, random port, token) for keys, models on / off and Test, Ollama
  status, machines, parallel and cloud switches.
- Kept from FiFoo: the diff guard, proofs, the push guard, issue comments and labels, one task at a time by
  default, cloud lanes with quota benching, unreachable hosts left out, OpenCode 2 detection, doctor checks.
