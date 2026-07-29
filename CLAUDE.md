# claude-code-bridge

MCP server (stdio) that dispatches coding tasks to headless `claude -p` subprocesses without
blocking the caller. See README.md for what it exposes; this file is what you need to change it
safely.

## Commands

```bash
uv sync
uv run pytest                                    # unit: fast, no auth, no tokens
CCB_INTEGRATION=1 uv run pytest -m integration   # spawns real claude runs; costs real money
uv run mcp dev src/claude_code_bridge/server.py  # MCP Inspector
uv tool install . --force                        # reinstall the console scripts after changes
./install.sh                                     # full install + desktop-app registration
claude-code-bridge-setup --dry-run               # show the config change without writing
```

## Installer

`install.sh` only bootstraps (`uv`, then `uv tool install`); all config editing lives in
`setup_client.py` so it can be unit-tested. Two non-obvious rules there:

- **Never symlink-resolve the `claude` path.** Claude Code installs `~/.local/bin/claude` as a
  symlink into a versioned directory (`~/.local/share/claude/versions/<v>`). Resolving it pins
  `PATH` to today's version, and dispatch breaks silently the next time Claude Code updates.
- **`setup_client.py` edits a file it does not own.** Merge, never replace; back up first; refuse a
  config that doesn't parse rather than overwriting it; write via tempfile + `os.replace`.

The whole reason the installer exists: a GUI-launched process gets no useful `PATH`, so the command
and `claude`'s directory both have to be written in absolutely. That failure mode is invisible
until a dispatch fails with a confusing error.

## Verified CLI facts

Established empirically against `claude` 2.1.220. Do not "simplify" these away:

- **`--verbose` is mandatory** with `-p --output-format stream-json`. Without it the CLI exits
  immediately with `Error: When using --print, --output-format=stream-json requires --verbose`.
- **`--disallowedTools` is variadic** (`<tools...>`). The patterns must stay a *single*
  comma-separated argv value, or the option swallows whatever follows it.
- **`--max-turns` works but is absent from `--help`.** Don't remove it because you can't find it
  documented; do re-probe it if a CLI upgrade breaks things.
- **`--session-id` and `--resume` are mutually exclusive.** A fresh run gets a `--session-id` we
  generate, so the session is known before any output arrives; a continuation gets `--resume`.
- The stream carries `system`/`init`, `system`/`hook_*`, `assistant`, `user`, `rate_limit_event`
  and a final `result` event. Only `init`/`result` matter; everything else must be ignored, not
  assumed absent.

## MCP SDK

This uses **mcp v2**, where `FastMCP` was renamed: `from mcp.server import MCPServer`. Tool errors
are `MCPError(INVALID_PARAMS, ...)`. Tests use the in-memory `Client(mcp)`.

Gotcha: an `MCPError` that escapes an `async with Client(...)` block comes back wrapped in an anyio
`ExceptionGroup`, which `pytest.raises` won't match. `tests/test_validation.py::call` captures it
inside the block and re-raises after closing — copy that pattern.

## Invariants — break these and the design stops holding

1. **All `claude` argv construction goes through `cli.build_claude_argv`.** It is the single place
   the commit/push deny rules are applied, and `cli.assert_safe` is called both there and again in
   `tasks.TaskRegistry._spawn` right before `create_subprocess_exec`. Never assemble an argv
   elsewhere, and never add a spawn path that skips `assert_safe`.
2. **Only `_monitor` publishes a terminal status and sets `Task.done`**, and only after the process
   has exited *and* its pipes are fully read. Anything else writing terminal status reintroduces
   the race where a live run looks finished — which lets it be resumed or evicted mid-flight.
   Cancellation therefore sets `cancel_requested`, and `_monitor` reads it.
3. **The stdout drainer is load-bearing.** stream-json is verbose; an unread pipe fills and blocks
   `claude` forever. The raw log is best-effort for exactly this reason — a full disk must never
   cost us the drain. If the drainer dies anyway it SIGKILLs the group rather than leaving a
   wedged process.
   Conversely, waiting on the drainers is bounded (`DRAIN_GRACE_SECONDS`): `proc.wait()` returning
   does not mean the pipes are closed, because a backgrounded grandchild can inherit stdout and
   hold it open forever. Without that bound the run would never be markable as finished.
4. **Termination is owned by `Task.termination`, not by the caller.** Callers `await shield(...)`
   it, so a disconnecting client cannot abandon the SIGKILL escalation.
5. **Signal `Task.pgid` (captured at spawn), never `os.getpgid(proc.pid)`.** Once the child is
   reaped its pid may name an unrelated process, and signalling that process's group would be
   catastrophic. Signalling is attempted even after the leader exits, because `claude`'s Bash
   children can outlive it holding the pipes open.
6. **One live run per session.** `resume` is refused while any task on that `session_id` is
   unfinished; two `--resume` processes would corrupt shared conversation state.

## Where the safety boundary actually falls

Measured, not assumed. Deny rules *do* beat `--permission-mode bypassPermissions`, and the matcher
decomposes `&&` chains but not all forms:

| Command | Outcome |
|---|---|
| `git commit -m x` | Denied |
| `git add a.txt && git commit -m x` | Denied |
| `git push origin main` | Denied |
| `git -C /path commit -m x` | **Runs** |
| `bash -c 'git commit -m x'` | **Runs** |

Deliberate: it stops an agent working normally, not one trying to escape. `tests/test_integration.py`
pins the denial end to end. If you widen `DISALLOWED_TOOLS`, update that table in both this file and
README.md — and re-measure rather than reasoning about it.

## Notes

- Dispatched sessions inherit the full user environment (all MCP servers, hooks, skills,
  `CLAUDE.md`). That is a deliberate choice, and it costs ~51k cache-creation tokens (~$0.50) per
  run as a floor. `--strict-mcp-config` is the lever if that changes.
- Log to **stderr only**. stdout is the MCP wire; anything printed there corrupts the protocol.
