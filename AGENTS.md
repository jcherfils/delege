# Instructions for coding agents

These instructions apply to all work in the Delege repository, including delegated tasks and handoffs.

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

## Delege-specific guidance

- This repository is eligible for task handoffs through Delege when a task is well-scoped and its configured agent
  has a suitable area. Keep each handoff isolated to its task branch and review its diff before merging.
- Preserve Delege's guardrails: agent areas, forbidden paths, proof commands, and the rule that Delege owns commits
  and pushes for delegated work.
- Keep optional engines and providers optional; do not make the standard-library-only installation depend on them.
- Update the project README and `template/AGENTS.md` when a user-facing workflow or generated project guidance changes.
