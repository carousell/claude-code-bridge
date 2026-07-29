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
7. **The server process is not stable — the task outlives it.** A client may run several bridge
   servers at once or restart one, and an in-memory-only registry meant a task was invisible to any
   process that did not spawn it (verified: the desktop app ran a second server 8 minutes after the
   first, and `get_task_status` answered "unknown task_id" while the run continued, unreachable).
   Every task therefore persists a `store.TaskRecord` sidecar; `store.snapshot` replays the stream
   log to rebuild the outcome. Keep the recovered snapshot's keys a superset of `Task.snapshot()` —
   a test enforces it — so callers never special-case recovery.

## Telling the caller what happened

The caller is usually a model, and it only knows what the tool surface says. So state changes it
cannot infer must be *in the payload*, not just in the docs: `recovered: true` plus a plain-language
`note` on recovered tasks, a `next_step` hint when a wait returns still-running, the constraint
written into `wait_for_task`'s docstring, and errors that say what to do instead of just what failed.
When adding behaviour that surprises a caller, add the words that explain it too.

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

- **Dispatched sessions must keep inheriting the full user environment** (all MCP servers, hooks,
  skills, `CLAUDE.md`) — that capability is the point, not a side effect. `cli.FORBIDDEN_FLAGS`
  enforces it: `--strict-mcp-config`, `--setting-sources`, `--safe-mode` and `--bare` are rejected by
  `assert_safe`, with tests, because any one of them would silently cut a dispatched task off from
  servers like owlex or argent. Do not add them as an optimisation; the cost floor (~51k
  cache-creation tokens, ~$0.50 a run) is a known and accepted trade.
- `mcp_servers` and `available_tool_count` in a snapshot come from the run's own `system/init`
  event. `pending` there usually means a deferred-tool server was still connecting — its tools are
  still reachable via `ToolSearch` and contribute nothing to `available_tool_count`, so do not read
  a low count or a `pending` status as a missing server. `needs-auth` genuinely is unusable
  headlessly.
- **Nothing is sandboxed.** `repo_path` is a working directory, not a boundary: a dispatched agent
  has full filesystem and network access as the user, plus every authenticated MCP connection. Only
  the commit/push deny patterns are enforced.
- Log to **stderr only**. stdout is the MCP wire; anything printed there corrupts the protocol.
