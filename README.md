# claude-code-bridge

A local MCP server that lets an MCP client — Claude Cowork, or any other host on the same Mac —
hand a coding task to a **headless Claude Code session** and carry on with something else.

Dispatch is never blocking. `start_claude_code_task` spawns `claude -p` in the background and
returns a `task_id` straight away. You then poll it, await it with a timeout that doesn't kill
anything, or resume the same conversation with follow-up instructions.

## Installation

```bash
git clone https://github.com/carousell/claude-code-bridge.git
cd claude-code-bridge
./install.sh
```

Then **restart the Claude desktop app** — MCP servers are only read at launch.

The script installs `uv` if you don't have it, installs the server, and registers it with the
desktop app. It merges into your existing config rather than replacing it, and takes a timestamped
backup first. Running it again is safe.

It does not install Claude Code itself, which the bridge dispatches to. If that's missing the
script tells you how to get it; install it and re-run `./install.sh`.

To see what it would write without touching anything:

```bash
claude-code-bridge-setup --dry-run
```

### Configuring it by hand

If you'd rather not run the script, note that **both paths must be absolute**:

```json
{
  "mcpServers": {
    "claude-code-bridge": {
      "command": "/Users/you/.local/bin/claude-code-bridge-server",
      "env": { "PATH": "/Users/you/.local/bin:/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

A desktop app doesn't inherit your shell `PATH`, so `"command": "claude-code-bridge-server"` won't
resolve, and without the `PATH` entry the server can't find `claude` — every dispatch then fails
with a misleading error. Point `PATH` at the directory holding the `claude` symlink
(`~/.local/bin`), not at whatever it resolves to: that target is version-specific and changes when
Claude Code updates itself.

Set `CCB_LOG_LEVEL=DEBUG` in the server's environment for per-event logging.

## Tools

| Tool | Blocking? | What it does |
|---|---|---|
| `start_claude_code_task(prompt, repo_path, max_turns=50, model=None)` | No | Spawns a new headless session in `repo_path`, returns a `task_id` immediately |
| `get_task_status(task_id)` | No | Current status, closing summary, cost, turns, permission denials, stream tail |
| `wait_for_task(task_id, timeout_seconds=600)` | Until done or timeout | On timeout returns `status: "running"` and **leaves the run alone** |
| `resume_claude_code_task(task_id, followup_prompt, max_turns=50)` | No | Continues that task's session as a **new** `task_id` with the same `session_id` |
| `list_tasks(status=None)` | No | All tasks, oldest first, optionally filtered |
| `cancel_task(task_id)` | Until dead | SIGTERM the run's process group, SIGKILL after 5s |

Statuses are `running`, `completed`, `failed`, `timed_out` (turn limit exhausted), and
`cancelled`. Each run's full raw event stream is kept at
`~/.claude-code-bridge/tasks/<task_id>.jsonl` for post-mortems; `get_task_status` returns the path.

The task registry is in memory. It targets 200 entries, reclaiming space from the oldest *finished*
tasks whenever a task starts or finishes. Live tasks are never evicted, so the registry can exceed
that target while more than 200 runs are in flight and settles back as they finish. Restarting the
server loses the registry, but the `session_id`s it reported stay resumable with
`claude --resume <session_id>` directly.

A session is only ever driven by one process at a time: resuming a task is refused while any run
on its session is still live, since two `--resume` processes would fight over the same conversation
state.

## Safety model

Dispatched agents run with `--permission-mode bypassPermissions`, so they never stop to ask about
editing files or running commands. Two things bound that:

1. **Scope** — each agent's working directory is the `repo_path` the caller passed. Nothing
   restricts it to that directory at the OS level, but it has no reason to wander outside it.
2. **`--disallowedTools "Bash(git commit:*),Bash(git push:*)"`** on *every* invocation, start and
   resume alike. Enforced by the CLI, not by asking the agent nicely. All argv construction goes
   through one function (`cli.build_claude_argv`) that asserts the flag is present before
   returning, so there is no code path without it.

So an agent can freely read, write and run things in the repo, but it cannot land or publish
anything. **You review and commit its work yourself.**

### Where that boundary actually falls

Measured against `claude` 2.1.220 rather than assumed — deny rules *do* take precedence over
`bypassPermissions`, and the matcher decomposes some compound commands but not all:

| Attempted command | Outcome |
|---|---|
| `git commit -m x` | **Denied** |
| `git add a.txt && git commit -m x` | **Denied** — chained forms are decomposed |
| `git push origin main` | **Denied** |
| `git -C /path commit -m x` | **Ran** — a flag between `git` and `commit` evades the pattern |
| `bash -c 'git commit -m x'` | **Ran** — wrapping evades the pattern |

The block stops an agent that is going about its work normally, which is what it is for. It is
not armour against one actively trying to get around it. If you need it tighter, broaden
`DISALLOWED_TOOLS` in `src/claude_code_bridge/cli.py` and/or add a `PreToolUse` deny hook that
pattern-matches the whole Bash command string.

Every blocked attempt is recorded: `get_task_status` surfaces the run's `permission_denials`, so
you can see when an agent tried to commit and was stopped.

### Inherited environment

Dispatched sessions inherit your full Claude Code environment — every MCP server, hook, skill and
`CLAUDE.md` your own sessions load. That makes them as capable as you are, and it is not free: a
one-word task measured ~51k cache-creation tokens (~$0.50) purely loading that context. Add
`--strict-mcp-config` in `cli.py` if you would rather trade capability for cost.

## Development

```bash
uv sync
uv run pytest                                  # unit tests, no auth needed, no tokens spent
CCB_INTEGRATION=1 uv run pytest -m integration  # dispatches a real run; costs real money
uv run mcp dev src/claude_code_bridge/server.py  # drive the tools in MCP Inspector
```
