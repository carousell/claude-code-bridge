"""Stream parsing, using event shapes captured from a real `claude` run."""

from __future__ import annotations

import json

import pytest

from claude_code_bridge.stream import StreamState, parse_line

SESSION = "e89614aa-6c79-4f95-b6b4-74277357b37b"

INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "session_id": SESSION,
    "cwd": "/tmp/repo",
    "model": "claude-opus-5[1m]",
    "tools": ["Bash", "Edit", "Read"],
}

HOOK_EVENT = {
    "type": "system",
    "subtype": "hook_started",
    "session_id": SESSION,
    "hook_name": "PreToolUse",
}

RATE_LIMIT_EVENT = {"type": "rate_limit_event", "session_id": SESSION, "rate_limit_info": {}}

DENIAL = {
    "tool_name": "Bash",
    "tool_use_id": "toolu_0198veQY2SVZe4pSWhSN3zNu",
    "tool_input": {"command": "git add a.txt && git commit -m probe"},
}

RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "session_id": SESSION,
    "result": "Created hello.txt with the text 'hi'.",
    "is_error": False,
    "num_turns": 3,
    "total_cost_usd": 0.51478,
    "duration_ms": 4210,
    "stop_reason": "end_turn",
    "terminal_reason": "completed",
    "permission_denials": [DENIAL],
}


def ingest_all(*events: dict) -> StreamState:
    state = StreamState()
    for event in events:
        parsed = parse_line(json.dumps(event))
        assert parsed is not None
        state.ingest(parsed)
    return state


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n", id="whitespace"),
        pytest.param('{"type": "result", "partia', id="truncated-json"),
        pytest.param("Error: something went wrong", id="plain-text"),
        pytest.param("[1, 2, 3]", id="json-array"),
        pytest.param('"a string"', id="json-scalar"),
    ],
)
def test_unusable_lines_are_skipped_not_raised(line: str) -> None:
    assert parse_line(line) is None


def test_parses_a_well_formed_event() -> None:
    assert parse_line(json.dumps(INIT_EVENT)) == INIT_EVENT


def test_session_id_available_from_the_first_event() -> None:
    state = ingest_all(HOOK_EVENT)
    assert state.session_id == SESSION


def test_first_session_id_wins() -> None:
    state = ingest_all(INIT_EVENT, {"type": "assistant", "session_id": "later-id"})
    assert state.session_id == SESSION


def test_fields_are_none_until_the_result_event_arrives() -> None:
    state = ingest_all(INIT_EVENT, HOOK_EVENT, RATE_LIMIT_EVENT)
    assert state.result_event is None
    assert state.summary is None
    assert state.is_error is None
    assert state.total_cost_usd is None
    assert state.num_turns is None
    assert state.subtype is None
    assert state.permission_denials == []


def test_result_event_populates_every_reported_field() -> None:
    state = ingest_all(INIT_EVENT, RATE_LIMIT_EVENT, RESULT_EVENT)
    assert state.summary == "Created hello.txt with the text 'hi'."
    assert state.is_error is False
    assert state.total_cost_usd == pytest.approx(0.51478)
    assert state.num_turns == 3
    assert state.subtype == "success"
    assert state.permission_denials == [DENIAL]
    assert state.event_count == 3


def test_malformed_result_fields_do_not_crash_accessors() -> None:
    state = ingest_all({"type": "result", "result": None, "num_turns": "three", "total_cost_usd": "free"})
    assert state.summary is None
    assert state.num_turns is None
    assert state.total_cost_usd is None
    assert state.permission_denials == []
