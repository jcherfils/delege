"""delege: delegate small, well-scoped coding tasks to headless OpenCode 2 agents running on a local model (Ollama)
or on free hosted models, spread over your machines. One git worktree and one ``delegate/<id>-<agent>`` branch per
task; the diff is checked against the agent's area and the forbidden files, the proof commands run, the branch is
pushed (never the base branch, never with force) and the issue is commented and labelled."""

__version__ = "0.1.0"
