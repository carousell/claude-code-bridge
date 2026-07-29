# Installer for claude-code-bridge

Date: 2026-07-29

## Problem

Installing the bridge currently means reading the README and hand-editing
`claude_desktop_config.json`. Doing that by hand surfaced a failure mode a coworker would have no
way to diagnose: GUI-launched processes do not inherit the shell `PATH`, so the server cannot find
the `claude` binary in `~/.local/bin` and *every* dispatch fails with a confusing error. The fix
(an explicit absolute command path plus a `PATH` entry in the MCP `env` block) must be encoded in
tooling rather than left to each person to rediscover.

Distribution was the other blocker: there was no git remote, so there was nothing for a coworker to
clone.

## Decisions

- **Target only the Claude desktop app.** Not the Claude Code CLI.
- **Bootstrap `uv` automatically** when missing. Only *check* for `claude` — it is an
  authenticated tool and installing it silently is not ours to do.
- **Distribution:** `github.com/carousell/claude-code-bridge` (public, MIT, already created with a
  LICENSE-only commit). PyPI publication is intended later.

## Approach: thin shell bootstrap + Python setup entry point

`install.sh` does only what must happen before Python code exists — ensure `uv`, then
`uv tool install .`. Config wiring lives in the package as a `claude-code-bridge-setup` console
script.

Rejected alternatives:

- *Shell-only.* JSON merging in shell means `sed` or `python3 -c`, and `/usr/bin/python3` is not
  guaranteed on a fresh Mac. Config corruption is the standard failure of this approach.
- *Python-only.* Cannot bootstrap itself before the package is installed.

The split also pays off for PyPI: once published, the clone disappears from the flow and the same
setup code still applies.

```bash
uv tool install claude-code-bridge && claude-code-bridge-setup
```

## Components

### `install.sh`

`bash`, `set -euo pipefail`, deliberately short enough to read before running. Ensures `uv`
(installing it via the official installer only if absent), runs `uv tool install --force`, then
hands over to `claude-code-bridge-setup`. Checks for `claude` and prints the fix rather than
installing it.

Takes **no arguments**, and validates that plus `HOME` and the checkout *before* installing
anything, so a typo cannot mutate the machine first. The executable directory comes from
`uv tool dir --bin` rather than assuming `~/.local/bin`, so a customised `UV_TOOL_BIN_DIR` still
works.

### `src/claude_code_bridge/setup_client.py`

Edits a file another application owns, so it is written defensively:

- Resolves the server via `shutil.which`, and builds the `PATH` env from the directories of
  `claude`, `git` (the server shells out to it) and the server itself, plus standard system paths.
  This is the whole point of the component.
- **Symlinks are never resolved.** Claude Code installs `~/.local/bin/claude` as a symlink into a
  versioned directory, so resolving it pins `PATH` to today's version and dispatch breaks silently
  at the next Claude Code update.
- **Merges** rather than replaces: only the `claude-code-bridge` key is touched; other MCP servers
  and unrelated keys such as `preferences` survive untouched.
- **Compare-and-swap:** the file is re-read immediately before writing and the run aborts if it
  changed, because the desktop app writes to this file too and a concurrent change would otherwise
  be silently discarded.
- Backup that never overwrites an existing one — timestamps are only second-precise, so two runs in
  the same second would otherwise destroy what the backup exists to protect.
- Refuses to proceed if the existing config is not valid JSON, instead of overwriting it.
- Atomic replace via a temp file in the same directory, `fsync`ed, with the original file's mode
  copied across (`NamedTemporaryFile` is `0600` and would otherwise tighten permissions silently).
  A symlinked config path has its target rewritten rather than being replaced by a regular file.
- Idempotent: re-running updates the entry in place.
- `--dry-run` prints the resulting config without writing.
- A missing `claude` warns rather than fails, so installing it afterwards still works. A command
  inside a virtualenv (from `uv run`) warns too, since that path disappears with the venv.
- macOS and Linux config paths; anything else errors with the manual JSON snippet.

## Testing

`tests/test_setup_client.py`, entirely against `tmp_path` — never the real config:

- merge preserves sibling MCP servers and unrelated top-level keys
- a backup is created holding the original contents, and successive backups never overwrite
- re-running is idempotent
- malformed existing JSON is refused and the file left untouched
- the generated `PATH` contains the directories holding `claude` and `git`
- `PATH` keeps the `claude` symlink's directory, not its versioned target
- a concurrent write is detected and nothing is written
- file permissions are preserved, and a symlinked config is rewritten through the link
- a missing config file is created

## Out of scope

Claude Code CLI registration, Windows support, and PyPI release plumbing.
