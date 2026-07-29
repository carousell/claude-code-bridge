"""Argument validation, exercised through an in-memory MCP client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client, MCPError

from claude_code_bridge import server
from claude_code_bridge.tasks import Task


async def call(tool: str, **arguments):
    """Call one tool on an in-memory client.

    An MCPError is captured and re-raised after the client has closed: letting it escape the
    `async with` gets it wrapped in an anyio ExceptionGroup, which pytest.raises would not match.
    """
    error: MCPError | None = None
    async with Client(server.mcp) as client:
        try:
            return await client.call_tool(tool, arguments)
        except MCPError as exc:
            error = exc
    raise error


async def test_rejects_a_directory_that_is_not_a_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(MCPError, match="not inside a git repository"):
        await call("start_claude_code_task", prompt="do a thing", repo_path=str(plain))


async def test_rejects_a_path_that_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(MCPError, match="does not exist"):
        await call("start_claude_code_task", prompt="x", repo_path=str(tmp_path / "nope"))


async def test_rejects_a_file(git_repo: Path) -> None:
    target = git_repo / "a.txt"
    target.write_text("one\n")
    with pytest.raises(MCPError, match="not a directory"):
        await call("start_claude_code_task", prompt="x", repo_path=str(target))


async def test_rejects_an_empty_repo_path() -> None:
    with pytest.raises(MCPError, match="non-empty"):
        await call("start_claude_code_task", prompt="x", repo_path="  ")


def test_a_git_repo_and_its_subdirectories_validate(git_repo: Path) -> None:
    nested = git_repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert server._resolve_repo_path(str(git_repo)) == git_repo.resolve()
    assert server._resolve_repo_path(str(nested)) == nested.resolve()


async def test_rejects_an_empty_prompt(git_repo: Path) -> None:
    with pytest.raises(MCPError, match="non-empty"):
        await call("start_claude_code_task", prompt="   ", repo_path=str(git_repo))


async def test_rejects_a_non_positive_max_turns(git_repo: Path) -> None:
    with pytest.raises(MCPError, match="max_turns"):
        await call("start_claude_code_task", prompt="x", repo_path=str(git_repo), max_turns=0)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_task_status", {}),
        ("wait_for_task", {"timeout_seconds": 1}),
        ("cancel_task", {}),
        ("resume_claude_code_task", {"followup_prompt": "more"}),
    ],
)
async def test_unknown_task_id_is_rejected(tool: str, arguments: dict) -> None:
    with pytest.raises(MCPError, match="unknown task_id"):
        await call(tool, task_id="no-such-task", **arguments)


async def test_rejects_an_unknown_status_filter() -> None:
    with pytest.raises(MCPError, match="unknown status"):
        await call("list_tasks", status="sleeping")


async def test_rejects_a_non_positive_wait_timeout(fake_running_task: Task) -> None:
    with pytest.raises(MCPError, match="timeout_seconds"):
        await call("wait_for_task", task_id=fake_running_task.task_id, timeout_seconds=0)


async def test_cannot_resume_a_task_that_is_still_running(fake_running_task: Task) -> None:
    with pytest.raises(MCPError, match="still running"):
        await call(
            "resume_claude_code_task",
            task_id=fake_running_task.task_id,
            followup_prompt="carry on",
        )


async def test_listing_reports_registered_tasks(fake_running_task: Task) -> None:
    result = await call("list_tasks")
    listed = result.structured_content["result"]
    assert [entry["task_id"] for entry in listed] == [fake_running_task.task_id]
    assert listed[0]["status"] == "running"

    empty = await call("list_tasks", status="completed")
    assert empty.structured_content["result"] == []


class RecordingContext:
    """Stands in for the injected MCP Context, capturing progress reports."""

    def __init__(self, fail: bool = False) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []
        self.fail = fail

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        if self.fail:
            raise RuntimeError("client does not accept progress")
        self.reports.append((progress, total, message))


def test_default_wait_stays_under_a_typical_client_request_timeout() -> None:
    """MCP clients time out requests around 60s, reporting -32001 while the task runs on."""
    assert server.DEFAULT_WAIT_SECONDS < 60


async def test_wait_emits_strictly_increasing_progress(
    fake_running_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL_SECONDS", 0.01)
    ctx = RecordingContext()

    await server._await_with_progress(fake_running_task, 1, ctx)

    values = [report[0] for report in ctx.reports]
    assert values, "expected progress while the task was still running"
    assert values == sorted(values)
    assert len(set(values)) == len(values)


async def test_wait_returns_immediately_once_finished(
    fake_running_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL_SECONDS", 0.01)
    fake_running_task.done.set()
    ctx = RecordingContext()

    await asyncio.wait_for(server._await_with_progress(fake_running_task, 30, ctx), timeout=1)

    assert ctx.reports == []


async def test_a_client_that_rejects_progress_does_not_break_the_wait(
    fake_running_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL_SECONDS", 0.01)

    await asyncio.wait_for(
        server._await_with_progress(fake_running_task, 1, RecordingContext(fail=True)), timeout=5
    )


async def test_wait_tells_the_caller_what_to_do_when_still_running(
    fake_running_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL_SECONDS", 0.01)

    result = await call("wait_for_task", task_id=fake_running_task.task_id, timeout_seconds=1)

    snapshot = result.structured_content
    assert snapshot["status"] == "running"
    assert "get_task_status" in snapshot["next_step"]


async def test_no_next_step_hint_once_the_task_has_finished(fake_running_task: Task) -> None:
    fake_running_task.status = "completed"
    fake_running_task.done.set()

    result = await call("wait_for_task", task_id=fake_running_task.task_id, timeout_seconds=1)

    assert "next_step" not in result.structured_content


@pytest.fixture
def fake_running_task(tmp_path: Path) -> Task:
    """A registry entry without a real subprocess, for guards that never reach one."""
    task = Task(
        task_id="task-1",
        session_id="session-1",
        repo_path=tmp_path,
        prompt="x",
        max_turns=5,
        log_path=tmp_path / "task-1.jsonl",
        started_at=datetime.now(timezone.utc),
    )
    server._reg()._tasks[task.task_id] = task
    return task
