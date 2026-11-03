# delege

Delegate small, well-scoped coding tasks to headless [OpenCode 2](https://opencode.ai/v2/docs/) agents or optional
[Aider](https://aider.chat/) workers using a **local model** (Ollama), or to **free cloud models** through OpenCode,
spread over your own machines like a small cluster.

A task is a GitHub issue labelled `delegate` + `agent:<name>` (or a line in a task file). For each task delege makes
one git worktree and one `delegate/<id>-<agent>` branch, runs `opencode run --standalone --agent <name>` in it,
**checks the diff** against the agent's area and the forbidden files, runs the agent's **proof commands** (tests,
lint, a check), commits, pushes the branch (never the base branch, never with force, never a model / data / media
file), comments the issue and labels it `delegate:done` or `delegate:failed`. Nothing is merged: you review.

- Standard library only (Python ≥ 3.11); `git`, `gh`, OpenCode 2 and Ollama on the machines. Aider is optional.
- Project-agnostic: each repository describes its agents in a `delege.toml`; the OpenCode permissions are
  generated from it.
- One task at a time by default; `--parallel N` over the machines; `--cloud` adds one lane per usable free hosted
  model, benched when its free limit is hit.

## Install

```bash
pip install git+https://github.com/jcherfils/delege                              # the tool
pip install "delege[keyring] @ git+https://github.com/jcherfils/delege"          # + API keys in the OS keyring
pip install "delege[images] @ git+https://github.com/jcherfils/delege"           # + `delege check-images` (Pillow)
```

On each machine that runs agents: [Ollama](https://ollama.com) with a coder model and a 64 K context
(`OLLAMA_CONTEXT_LENGTH=65536`, e.g. `ollama pull qwen3-coder:30b`), OpenCode 2 for the default engine and cloud
lanes (`curl -fsSL https://opencode.ai/v2/install | bash`), and [`gh`](https://cli.github.com) logged in on the
machine that runs delege. To use Aider for local tasks, install it on that host with
`python -m pip install aider-chat`.

## Quick start

```bash
cd ~/myproject
delege init          # writes delege.toml, opencode.jsonc, .opencode/agents/{tests,fix,lint,docs}.md, an issue template
delege doctor        # project, OpenCode 2 and its agents, Ollama (context, GPU share), gh, git identity, ssh machines
delege plan          # the open `delegate` issues → branch, agent, machine (runs nothing)
delege run --only 12 # one issue; then: git log -p main..delegate/12-tests
```

Commit `delege.toml`, `opencode.jsonc`, `.opencode/agents/` and `AGENTS.md` to the repository. Edit the agents'
instructions (the body of each `.md`) as you like; run `delege init` again after changing `delege.toml` — the front
matter (permissions) is regenerated, the bodies are kept. `delege init` preserves an existing project-owned
`AGENTS.md` and only creates it when missing.

Give a task: label an issue `delegate` and name the agent with a label `agent:tests` or a body line `Agent: tests`;
an optional `Area: tests/test_x.py` line narrows the agent's area. Write it as you would for a new colleague: small,
concrete, with a test or a command that proves it. Or a task file: `delege run --tasks tasks.txt` with lines
`<agent>: <task>`, or a JSON list `[{"agent": "lint", "title": "…", "body": "…", "area": "pkg/ui/*"}]`.

## Commands

| Command | What it does |
|---|---|
| `delege init [--force]` | starter `delege.toml` (filled from the git remote), `opencode.jsonc`, `.github/ISSUE_TEMPLATE/delegate.md`, and the agents' files generated from the toml |
| `delege doctor [--no-load]` | one line per check, each failure with its fix line; exit 1 while something fails |
| `delege plan [--tasks F] [--only 12,15] [--cloud]` | who would run what (heaviest first, each task to the machine that finishes it first: load ÷ GPU memory × slots) |
| `delege run [--tasks F] [--only …] [--cloud] [--parallel N] [--redo] [--no-push] [--no-comment] [--dry-run]` | the run; `--redo` deletes a task's earlier branch first; `--dry-run` prints the commands |
| `delege push delegate/12-tests …` | push reviewed branches (only `delegate/*`, never force, only checked files) |
| `delege models [--no-probe]` | the free hosted models: usable now, or why not (key? listed by opencode? on? exhausted? probe?) |
| `delege check-images a.png …` | an image proof: readable, ≥ 200 px on a side, not blank |
| `delege settings [--no-browser]` | the local settings page (see below) |

Statuses: `ok`, `proof-failed` (committed, a proof failed: read it first), `refused` (a file outside the area or
forbidden: nothing committed, worktree kept), `failed` (OpenCode error or timeout), `no-change`, `skipped` (the
branch exists, no machine, or every cloud model benched). Reports: `~/.local/share/delege/reports/<time>/`.

## Configuration

### The project: `delege.toml` (or `[tool.delege]` in `pyproject.toml`)

```toml
[project]
name = "myproject"
repo = "owner/myproject"          # GitHub owner/name (default: from the origin remote)
base = "main"                      # the branch the worktrees start from; never pushed by delege
rules = """
- Caches and data only under the user's home folder, never in the repository.
"""                                # appended to every task's prompt and to the generated agents

[forbidden]
suffixes = [".pt", ".onnx", ".safetensors", ".npz", ".npy", ".mp4"]   # never committed nor pushed
media_suffixes = [".png", ".jpg"]  # only an agent with media = true, in its area; never pushed by delege
dir_names = ["videos", "_dataset"] # folders of that name anywhere: never read by agents, never committed
file_names = ["result.json"]
paths = ["data/my_set"]            # repository-relative folders or globs

[agents.tests]
description = "Adds pytest tests for one module"
areas = ["tests/*"]                # the files it may change (globs; `*` matches `/` too)
proof = ["{python} -m pytest -q -p no:cacheprovider {changed:tests/*test_*.py}"]
shell = ["python -m pytest *"]     # commands it may run besides the read-only ones (OpenCode patterns)
cost = 1.0                         # relative time, for the plan
strong = false                     # true: goes to the strongest cloud model running (fix, checks)
media = false                      # true: may change images in its area
webfetch = []                      # URL patterns it may fetch (none by default)
prompt = ""                        # extra prompt lines ({issue}, {branch}, {area})
steps = 60
```

Proof placeholders: `{python}` (the host's Python), `{delege}` (`python -m delege`), `{issue}` (the issue number;
the command is skipped without one), `{changed}` (the changed files), `{changed:GLOB|GLOB}` (those matching; the
command is skipped when none does; `{changed:GLOB!}` makes that a failure — a fix without a test).

Each agent's OpenCode 2 file (`.opencode/agents/<name>.md`) starts with a generated `permissions` list: every edit
denied, then the areas allowed (relative and absolute); every shell command denied, then the read-only ones and the
agent's `shell` list allowed, then `git push` / `commit` / `reset` / `checkout` / `rebase` / `worktree` / `stash`,
`rm -rf`, `curl`, `wget`, `pip`, `uv`, `npm`, `sudo` denied again; the forbidden folders, `~/.config/delege` and
`~/.ssh` never read; `webfetch` denied but the agent's list; `websearch`, `external_directory`, `subagent`,
`question`, `skill`, `execute` denied. OpenCode's rules are the first guard; **delege's own diff check is the
guarantee** (`tests/test_agents.py` evaluates the generated rules with OpenCode's semantics). The body of the file
is the agent's instructions: yours to edit. `examples/fifoo.delege.toml` is a full example (seven agents, dataset
folders, licence sites for one agent). The `template/` folder is what `delege init` copies; the repository can also
be used as a GitHub template.

### The machine: `~/.config/delege/config.json`

Written by `delege settings` (or by hand): the machines, the ssh command, the commit identity, the timeout, the
parallelism, the cloud switch, the models on / off and registry overrides.

```json
{
  "hosts": [
    {"name": "rtx", "slots": 1, "gpu_gb": 32, "engine": "opencode"},
    {"name": "box2", "ssh": "jm@box2.local", "repos": {"owner/myproject": "~/myproject"}, "slots": 1,
     "gpu_gb": 16, "python": ".venv/bin/python", "agents": ["tests", "lint", "docs"]}
  ],
  "identity": {"name": "OpenCode", "email": "opencode@localhost"},
  "model": null, "timeout_min": 60, "parallel": 1, "cloud": false,
  "enabled": {"google/gemini-2.5-flash": true},
  "models": [{"id": "groq/openai/gpt-oss-20b", "limits": {"rpd": 10}, "strength": 3}]
}
```

A host without `ssh` is this machine (`gpu_gb` from `nvidia-smi` when left out). An ssh host needs a clone of each
project (`repos`, default `~/<project name>`), OpenCode 2 and Ollama, and key-based ssh (`BatchMode`); the base
commit is sent to its clone under a private ref, the task runs in a worktree under its `~/.local/share/delege`, and
the branch comes back. A machine that does not answer within 8 s is left out of the run. Without the file: this
machine, one slot. Keys live in the OS keyring (service `delege`) or `~/.config/delege/secrets.json` (mode 0600);
usage counts in `~/.local/share/delege/usage.json`. `DELEGE_HOME` puts everything in one folder. FiFoo's old
`~/.fifoo/delegate.json`, `cloud.json` and `secrets.json` are read once, as a migration, when there is no
`config.json` yet.

Set a host's `"engine": "aider"` to use Aider for its local-model tasks; optionally set `"aider": "/path/to/aider"`
to choose its executable. Delege translates an `ollama/<model>` id to Aider's `ollama_chat/<model>` driver and
passes the generated agent instructions. Cloud-model lanes continue to use OpenCode. Aider does not enforce
OpenCode's per-agent read and tool permissions, so its final Delege diff check protects commits but does not provide
the same read isolation. Use Aider only where that weaker boundary is acceptable.

### The settings page

`delege settings` serves one page on `127.0.0.1` (a random port, a secret token in the URL; requests from another
`Host` or without the token are refused; nothing listens beyond localhost) and opens it in the browser: the cloud
switch and the parallelism it gives, parallel / timeout / model / identity, the local Ollama's status and models,
the machines table, and for each provider its key field, *Create a key…* and *Policy* links, each model's free
limits, today's usage, on / off and a *Test* button (a 1-token question). Standard-library HTTP server only.

## Cloud models with a free tier

`delege run --cloud` (or the switch): one lane per usable hosted model plus the local model, each lane one task at a
time; tasks go out heaviest first, `strong` agents to the strongest model running. **Only free tiers**: each model
is listed with its free limit as the provider publishes it (or a budget delege imposes when no figure is published);
a model without a declared free limit is refused, so nothing runs on a paid path. The usage of each model (requests
and tokens, from `opencode run --format json`) is counted per UTC day; a model that reaches its count or answers
429 is **benched** until its reset (or `Retry-After`), its task goes back to the queue. Each task's worktree gets an
`opencode.jsonc` whose provider policies allow exactly `ollama` and that task's provider; the repository's file stays
Ollama-only. Keys reach OpenCode through the environment of each run.

| Provider (OpenCode id) | Key variable | Create a key | Data on the free tier |
|---|---|---|---|
| OpenCode free models (`opencode`) | `OPENCODE_API_KEY` | <https://opencode.ai/auth> | per model: zero retention for GLM-5.3-Flash and DeepSeek V4.1 Flash; others may improve the model ([policy](https://opencode.ai/docs/zen)) |
| OpenRouter `:free` (`openrouter`) | `OPENROUTER_API_KEY` | <https://openrouter.ai/settings/keys> | no prompt stored unless you opt in; upstream policies per model ([logging](https://openrouter.ai/docs/guides/privacy/logging)) |
| Google AI Studio (`google`) | `GOOGLE_GENERATIVE_AI_API_KEY` | <https://aistudio.google.com/apikey> | may be used to improve Google's products, except in the EEA / UK / Switzerland ([terms](https://ai.google.dev/gemini-api/terms)) — off by default |
| Groq (`groq`) | `GROQ_API_KEY` | <https://console.groq.com/keys> | no training, no retention by default ([your data](https://console.groq.com/docs/your-data)) |
| Mistral Free mode (`mistral`) | `MISTRAL_API_KEY` | <https://console.mistral.ai/api-keys> | trains unless opted out ([opt out](https://help.mistral.ai/en/articles/455207-can-i-opt-out-of-my-input-or-output-data-being-used-for-training)) — off by default |

The registry (`delege/cloud.py`) is a snapshot of the providers' pages read on 2026-11-02; limits change, and the
daily count and the 429 answers are what protects the free tier. `config.json` › `"models"` adjusts it.

## Privacy

By default nothing leaves your machines: the model runs in Ollama on `localhost`, OpenCode is configured with that
one provider, the agents have no network except what an agent's `webfetch` list allows. Between machines: ssh and
git over ssh, your own. GitHub sees the issues delege reads with `gh api` and the `delegate/*` branches it pushes.
**With `--cloud`** (off by default): the task's text (the issue) and the **repository code the agent reads** go to
the free providers you enabled, under each one's data policy (linked above); never the forbidden folders (their
`read` rules), nor `~/.config/delege`, nor a model, data or media file (refused in the diff anyway). Models whose
provider may train on the prompts are off until you switch them on. Each issue comment and commit message names the
model that did the task and the requests and tokens it used.

## Development

```bash
pip install -e .[dev]
ruff check delege tests && python -m pytest -q
```

The tests use a fake OpenCode 2 (`--version`, `debug agents`, `models`, `run --format json`), fake ssh machines,
a fake `gh`, a fake Ollama and a fake OpenAI-compatible API — no network, no model.

MIT licence — Jean-Marc Cherfils.
