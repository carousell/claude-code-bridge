"""MCP server exposing headless Claude Code dispatch as tools."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import INVALID_PARAMS

from . import store
from .cli import CLAUDE_BINARY, DEFAULT_MAX_TURNS, DISALLOWED_TOOLS
from .tasks import (
    TERMINAL_STATUSES,
    RepoUnavailableError,
    SessionBusyError,
    Task,
    TaskRegistry,
)

log = logging.getLogger("claude_code_bridge")

VALID_STATUSES = frozenset({"running"}) | TERMINAL_STATUSES

# MCP clients impose their own per-request timeout — commonly 60s — and exceeding it surfaces to
# the caller as a transport error (-32001) even though the dispatched run is unaffected. So the
# default wait stays under that, and callers are expected to come back rather than hold one
# request open for the length of a coding task.
DEFAULT_WAIT_SECONDS = 55

# Emitted while waiting so the client can see the wait is alive; per the MCP spec a client may
# also reset its request timeout on progress, which is what makes longer explicit waits viable.
PROGRESS_INTERVAL_SECONDS = 5.0

mcp = MCPServer(
    "claude-code-bridge",
    instructions=(
        "Dispatch coding tasks to headless Claude Code sessions on this machine. "
        "start_claude_code_task returns immediately with a task_id; poll it with "
        "get_task_status or await it with wait_for_task, then continue the same session with "
        "resume_claude_code_task. Dispatched agents run with permission prompts bypassed but "
        "with git commit and git push denied, so review and commit their work yourself.\n\n"
        "Two things worth knowing. A wait_for_task that comes back still 'running' has not failed "
        "— the run is untouched, so call again or poll get_task_status. And tasks outlive this "
        "server process: ones started by an earlier bridge server are still reported, marked "
        "'recovered: true' with a 'note' saying what is known about them."
    ),
)

_registry: TaskRegistry | None = None


def _reg() -> TaskRegistry:
    # Built lazily so its asyncio primitives belong to the loop `mcp.run()` creates.
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry


def _require_task(task_id: str) -> Task:
    task = _reg().get(task_id)
    if task is None:
        raise MCPError(INVALID_PARAMS, f"unknown task_id: {task_id}")
    return task


def _check_max_turns(max_turns: int) -> None:
    if max_turns < 1:
        raise MCPError(INVALID_PARAMS, f"max_turns must be >= 1, got {max_turns}")


def _resolve_repo_path(repo_path: str) -> Path:
    if not repo_path or not repo_path.strip():
        raise MCPError(INVALID_PARAMS, "repo_path must be a non-empty path")

    path = Path(repo_path).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise MCPError(INVALID_PARAMS, f"repo_path does not exist: {repo_path}") from None
    if not path.is_dir():
        raise MCPError(INVALID_PARAMS, f"repo_path is not a directory: {path}")

    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise MCPError(INVALID_PARAMS, f"repo_path is not inside a git repository: {path}")
    return path


async def _validate_repo_path(repo_path: str) -> Path:
    return await asyncio.to_thread(_resolve_repo_path, repo_path)


def _require_claude() -> None:
    if shutil.which(CLAUDE_BINARY) is None:
        raise MCPError(
            INVALID_PARAMS,
            f"the `{CLAUDE_BINARY}` CLI was not found on PATH; install Claude Code first",
        )


@mcp.tool()
async def start_claude_code_task(
    prompt: str,
    repo_path: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = None,
) -> dict[str, Any]:
    """Dispatch a coding task to a new headless Claude Code session and return immediately.

    Args:
        prompt: Instructions for the agent. Be specific about the desired end state.
        repo_path: Absolute path to a git repository; the agent's working directory.
        max_turns: Cap on agent turns before the run is stopped.
        model: Model alias or full name (e.g. "opus", "sonnet"). Defaults to Claude Code's own.

    Returns the new task_id and its starting state. The run continues in the background; poll
    get_task_status or call wait_for_task to follow it.
    """
    _require_claude()
    _check_max_turns(max_turns)
    if not prompt or not prompt.strip():
        raise MCPError(INVALID_PARAMS, "prompt must be a non-empty string")

    path = await _validate_repo_path(repo_path)
    task = await _reg().start(prompt, path, max_turns=max_turns, model=model)
    return task.brief()


@mcp.tool()
async def get_task_status(task_id: str) -> dict[str, Any]:
    """Report a dispatched task's current state without blocking.

    Args:
        task_id: Identifier returned by start_claude_code_task or resume_claude_code_task.

    Includes the agent's closing summary, cost, turn count and any permission_denials once the
    run has finished, plus the tail of its event stream while it is still going.

    Tasks started by an earlier bridge server process are still reported, read back from disk. Those
    carry `recovered: true` and a `note` explaining what is and is not known about them.
    """
    task = _reg().get(task_id)
    if task is not None:
        return task.snapshot()

    record = _reg().recover(task_id)
    if record is None:
        raise MCPError(
            INVALID_PARAMS,
            f"unknown task_id: {task_id}. No task with that id is running, and no record of one "
            f"exists under {_reg().log_dir}. Use list_tasks to see what is known.",
        )
    return store.snapshot(_reg().log_dir, record)


async def _await_with_progress(task: Task, timeout_seconds: int, ctx: Context | None) -> None:
    """Wait for `task`, emitting progress every few seconds until it finishes or time runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    steps = max(1, math.ceil(timeout_seconds / PROGRESS_INTERVAL_SECONDS))

    for step in range(1, steps + 1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(
                task.done.wait(), timeout=min(PROGRESS_INTERVAL_SECONDS, remaining)
            )
            return
        except asyncio.TimeoutError:
            pass

        if ctx is None:
            continue
        try:
            await ctx.report_progress(
                step,
                total=steps,
                message=f"{task.task_id} still running ({task.duration_seconds:.0f}s elapsed)",
            )
        except Exception:  # pragma: no cover - a client that ignores progress must not break us
            log.debug("task %s: client did not accept progress", task.task_id)


async def _poll_recovered(
    record: store.TaskRecord, timeout_seconds: int, ctx: Context | None
) -> store.TaskRecord:
    """Wait on a task from another server process by polling its liveness.

    There is no completion event to await here, so this checks the recorded process on the same
    cadence it reports progress, and re-reads the record in case that process's own server
    finishes it first.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    steps = max(1, math.ceil(timeout_seconds / PROGRESS_INTERVAL_SECONDS))

    for step in range(1, steps + 1):
        if loop.time() >= deadline:
            break
        if not store.process_alive(record.pid, record.session_id):
            break
        await asyncio.sleep(min(PROGRESS_INTERVAL_SECONDS, max(0.0, deadline - loop.time())))

        fresh = _reg().recover(record.task_id)
        if fresh is not None:
            record = fresh
        if record.status in TERMINAL_STATUSES:
            break

        if ctx is None:
            continue
        try:
            await ctx.report_progress(
                step, total=steps, message=f"{record.task_id} still running (recovered task)"
            )
        except Exception:  # pragma: no cover - a client that ignores progress must not break us
            log.debug("task %s: client did not accept progress", record.task_id)

    return record


@mcp.tool()
async def wait_for_task(
    task_id: str,
    timeout_seconds: int = DEFAULT_WAIT_SECONDS,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Wait for a task to finish, giving up after a timeout without disturbing the run.

    Args:
        task_id: Identifier of the task to await.
        timeout_seconds: How long to wait before returning early. Keep this modest — your MCP
            client applies its own request timeout (often 60s), and exceeding it fails the *call*
            with a timeout error even though the task keeps running.
        ctx: Injected by the server; not a caller argument.

    If the task is still going when the wait ends, the result comes back with status "running" and
    a `next_step` hint: just call this again, or poll get_task_status. The dispatched process is
    never signalled here, so waiting is always safe and repeating it costs nothing.

    A coding task can easily run for many minutes. Expect several calls rather than one long one.
    """
    if timeout_seconds <= 0:
        raise MCPError(INVALID_PARAMS, f"timeout_seconds must be > 0, got {timeout_seconds}")

    task = _reg().get(task_id)
    if task is not None:
        await _await_with_progress(task, timeout_seconds, ctx)
        snapshot = task.snapshot()
        finished = task.finished
    else:
        # A task from an earlier server process is waitable too; get_task_status reports it, so
        # refusing to wait on it here would just be an inconsistency the caller has to work around.
        record = _reg().recover(task_id)
        if record is None:
            raise MCPError(
                INVALID_PARAMS,
                f"unknown task_id: {task_id}. Use list_tasks to see what is known.",
            )
        record = await _poll_recovered(record, timeout_seconds, ctx)
        snapshot = store.snapshot(_reg().log_dir, record)
        finished = snapshot["status"] in TERMINAL_STATUSES

    if not finished:
        log.info("task %s still running after %ss wait", task_id, timeout_seconds)
        snapshot["next_step"] = (
            f"still running after {timeout_seconds}s and unaffected by this wait — "
            "call wait_for_task again, or poll get_task_status"
        )
    return snapshot


@mcp.tool()
async def resume_claude_code_task(
    task_id: str,
    followup_prompt: str,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> dict[str, Any]:
    """Continue a finished task's session with follow-up instructions.

    Args:
        task_id: A task that has finished; its session is the one resumed.
        followup_prompt: What the agent should do next, with the prior context intact.
        max_turns: Cap on agent turns for this continuation.

    Returns a new task_id sharing the original session_id, and returns immediately as with
    start_claude_code_task.
    """
    _require_claude()
    _check_max_turns(max_turns)
    if not followup_prompt or not followup_prompt.strip():
        raise MCPError(INVALID_PARAMS, "followup_prompt must be a non-empty string")

    parent = _reg().get(task_id)
    try:
        if parent is not None:
            # `finished`, not the status field: a cancellation in progress is not resumable yet.
            if not parent.finished:
                raise MCPError(
                    INVALID_PARAMS,
                    f"task {task_id} is still {parent.status}; wait for it or cancel it "
                    "before resuming",
                )
            task = await _reg().resume(parent, followup_prompt, max_turns=max_turns)
        else:
            record = _reg().recover(task_id)
            if record is None:
                raise MCPError(INVALID_PARAMS, f"unknown task_id: {task_id}")
            if store.process_alive(record.pid, record.session_id):
                raise MCPError(
                    INVALID_PARAMS,
                    f"task {task_id} is still running (started by an earlier bridge server "
                    "process); wait for it or cancel it before resuming",
                )
            task = await _reg().resume_record(record, followup_prompt, max_turns=max_turns)
    except (SessionBusyError, RepoUnavailableError) as exc:
        raise MCPError(INVALID_PARAMS, str(exc)) from None
    return task.brief()


@mcp.tool()
async def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
    """List dispatched tasks, oldest first.

    Args:
        status: Optional filter — one of running, completed, failed, timed_out, cancelled.

    Includes tasks from earlier bridge server processes, marked `recovered: true`.
    """
    if status is not None and status not in VALID_STATUSES:
        raise MCPError(
            INVALID_PARAMS,
            f"unknown status {status!r}; expected one of {sorted(VALID_STATUSES)}",
        )

    live = _reg().list()
    entries = [task.brief() for task in live]
    entries.extend(_reg().recovered_briefs(exclude={task.task_id for task in live}))
    entries.sort(key=lambda entry: entry["started_at"])

    if status is not None:
        entries = [entry for entry in entries if entry["status"] == status]
    return entries


@mcp.tool()
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Stop a running task, terminating the agent and any processes it spawned.

    Args:
        task_id: Identifier of the task to stop.

    Already-finished tasks are returned unchanged. Work the agent had already written to disk is
    left in place. Tasks started by an earlier bridge server process can be stopped too, via their
    recorded process group.
    """
    task = _reg().get(task_id)
    if task is not None:
        await _reg().cancel(task)
        return task.snapshot()

    record = _reg().recover(task_id)
    if record is None:
        raise MCPError(INVALID_PARAMS, f"unknown task_id: {task_id}")

    # Snapshot the record cancellation produced, not the one we started from, or the response
    # would report a status that later calls contradict.
    return store.snapshot(_reg().log_dir, await _reg().cancel_recovered(record))


def main() -> None:
    """Entry point for the `claude-code-bridge-server` console script."""
    logging.basicConfig(
        level=os.environ.get("CCB_LOG_LEVEL", "INFO").upper(),
        # stderr, never stdout: stdout is the MCP wire.
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if shutil.which(CLAUDE_BINARY) is None:
        log.warning(
            "`%s` is not on PATH; dispatch tools will fail until Claude Code is installed",
            CLAUDE_BINARY,
        )
    log.info("claude-code-bridge starting (denied tools: %s)", DISALLOWED_TOOLS)
    mcp.run()


if __name__ == "__main__":
    main()
