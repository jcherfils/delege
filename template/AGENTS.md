# Working in a project managed by Delege

Delege is the project's task coordinator. It reads configured GitHub issues or task files, starts an isolated
Git worktree and branch for each task, runs a coding agent, checks the changed files, runs the configured proof
commands, and records a report. Follow the existing project instructions and `delege.toml` for the task's scope.

The project may install Delege as a Python dependency. Use its documented command line (`delege` or
`python -m delege`) rather than importing private implementation modules. Do not change the dependency version or
the project's Delege configuration unless the task asks for that work.

## Engineering standards

- Write code, comments, docstrings, and user-facing messages in English.
- Prefer small functions with clear responsibilities and explicit names.
- Document inputs, outputs, and data shapes when they are not obvious.
- Read files with safe fallbacks and validate the shape of received data.
- Write files atomically to prevent partial or corrupted files.
- Never ignore errors silently: catch precise exceptions and explain failures.
- Load optional dependencies only when needed and explain how to install them when missing.
- Set timeouts on subprocesses and check their return codes.
- Test important behavior and edge cases; keep changes focused.
- Follow the existing style and avoid reformatting files without a reason.

## Before acting

- Read the repository's `delege.toml` and the task's `Agent:` and optional `Area:` fields.
- Keep edits inside both the selected agent's `areas` and the task's narrower `Area:`.
- Treat `forbidden` paths, suffixes, and names as private project data. Do not read, copy, stage, or transmit them.
- Do not start a nested `delege run` from inside a delegated task. Delege already owns that task's worktree and
  branch.

## Running Delege

- Use `delege doctor` to inspect local setup and `delege plan` to preview task assignment.
- Use `delege run --dry-run` to inspect the commands before a run.
- Run `delege run` only when the project owner or task explicitly asks you to execute delegated work. A normal
  successful run pushes its task branches and may comment on and label the corresponding GitHub issues. Use
  `--no-push --no-comment` when the requested run should stay local and silent.
- Do not merge task branches. The project owner reviews and merges them.

## While implementing a delegated task

- Do not commit, push, switch branches, or create worktrees. Delege owns those Git operations.
- Do not edit outside the task area, even if a change elsewhere seems useful. Report the follow-up instead.
- Do not add credentials, local configuration, generated caches, datasets, model weights, or media files unless
  the project explicitly permits them and Delege's checks allow them.
- Run only the proof commands configured for the selected agent. Delege runs those proofs again before committing.
- Finish with a concise report of changed files, proof run, and anything left unresolved.

## Engine notes

The project may run OpenCode or Aider, as selected in the local Delege machine configuration. Ollama serves a
local model; it does not coordinate tasks or provide Git isolation. Keep the same task boundaries and reporting
rules with either engine. OpenCode permissions provide additional read and tool restrictions; the final Delege
diff check does not replace those restrictions when using a different engine.
