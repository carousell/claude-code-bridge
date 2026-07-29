"""End-to-end dispatch against the real `claude` CLI.

Opt in with CCB_INTEGRATION=1. These spawn real agents, need working auth, and spend real money.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from claude_code_bridge import server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CCB_INTEGRATION"),
        reason="set CCB_INTEGRATION=1 to run tests that spawn real claude sessions",
    ),
    pytest.mark.skipif(shutil.which("claude") is None, reason="`claude` CLI not on PATH"),
]

POLL_TIMEOUT_SECONDS = 240
POLL_INTERVAL_SECONDS = 3

TERMINAL = {"completed", "failed", "timed_out", "cancelled"}


async def poll_until_finished(client: Client, task_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS
    while True:
        status = (await client.call_tool("get_task_status", {"task_id": task_id})).structured_content
        if status["status"] in TERMINAL:
            return status
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(
                f"task {task_id} still {status['status']} after {POLL_TIMEOUT_SECONDS}s; "
                f"tail={status['last_output_tail'][-3:]}"
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def test_dispatched_task_completes_and_writes_the_file(git_repo: Path) -> None:
    async with Client(server.mcp) as client:
        started = (
            await client.call_tool(
                "start_claude_code_task",
                {
                    "prompt": "Create a file called hello.txt with the text 'hi'. Nothing else.",
                    "repo_path": str(git_repo),
                    "max_turns": 8,
                },
            )
        ).structured_content

        # The point of the design: dispatch returns before the agent has done anything.
        assert started["status"] == "running"
        assert started["session_id"]

        status = await poll_until_finished(client, started["task_id"])

    assert status["status"] == "completed", status["summary"]
    assert status["is_error"] is False
    assert status["exit_code"] == 0
    assert status["num_turns"] >= 1
    assert Path(status["raw_stream_log"]).exists()

    hello = git_repo / "hello.txt"
    assert hello.exists(), f"agent did not create hello.txt; said: {status['summary']}"
    assert hello.read_text().strip() == "hi"


async def test_resume_continues_the_same_session(git_repo: Path) -> None:
    async with Client(server.mcp) as client:
        first = (
            await client.call_tool(
                "start_claude_code_task",
                {
                    "prompt": "Create a file called one.txt containing the text 'one'.",
                    "repo_path": str(git_repo),
                    "max_turns": 8,
                },
            )
        ).structured_content
        await poll_until_finished(client, first["task_id"])

        second = (
            await client.call_tool(
                "resume_claude_code_task",
                {
                    "task_id": first["task_id"],
                    "followup_prompt": "Now create two.txt containing the text 'two'.",
                    "max_turns": 8,
                },
            )
        ).structured_content

        assert second["task_id"] != first["task_id"]
        assert second["session_id"] == first["session_id"]
        assert second["parent_task_id"] == first["task_id"]

        status = await poll_until_finished(client, second["task_id"])

    assert status["status"] == "completed", status["summary"]
    assert (git_repo / "two.txt").exists()


async def test_wait_for_task_timeout_leaves_the_run_alive(git_repo: Path) -> None:
    async with Client(server.mcp) as client:
        started = (
            await client.call_tool(
                "start_claude_code_task",
                {
                    "prompt": "List every file in this repository, then describe each one.",
                    "repo_path": str(git_repo),
                    "max_turns": 20,
                },
            )
        ).structured_content

        waited = (
            await client.call_tool(
                "wait_for_task", {"task_id": started["task_id"], "timeout_seconds": 1}
            )
        ).structured_content
        assert waited["status"] == "running"

        cancelled = (
            await client.call_tool("cancel_task", {"task_id": started["task_id"]})
        ).structured_content
        assert cancelled["status"] == "cancelled"


async def test_commit_is_denied_in_a_dispatched_task(git_repo: Path) -> None:
    """The safety boundary, verified through the bridge rather than by hand."""
    (git_repo / "a.txt").write_text("one\n")

    async with Client(server.mcp) as client:
        started = (
            await client.call_tool(
                "start_claude_code_task",
                {
                    "prompt": (
                        "Run exactly this one command and report the outcome: "
                        "git add a.txt && git commit -m probe"
                    ),
                    "repo_path": str(git_repo),
                    "max_turns": 6,
                },
            )
        ).structured_content
        status = await poll_until_finished(client, started["task_id"])

    assert status["permission_denials"], f"expected a denial, got: {status['summary']}"
    commands = [
        denial.get("tool_input", {}).get("command", "") for denial in status["permission_denials"]
    ]
    assert any("git commit" in command for command in commands), commands

    log = subprocess.run(
        ["git", "-C", str(git_repo), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert log.stdout.strip() == "", f"a commit was created despite the deny rule: {log.stdout}"
