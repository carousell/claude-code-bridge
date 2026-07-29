"""The safety flags must survive every code path that builds a `claude` argv."""

from __future__ import annotations

import pytest

from claude_code_bridge.cli import (
    DISALLOWED_TOOLS,
    FORBIDDEN_FLAGS,
    UnsafeInvocationError,
    assert_safe,
    build_claude_argv,
)

SESSION = "11111111-1111-1111-1111-111111111111"

# Both entry points the server exposes: a fresh session and a resumed one.
INVOCATIONS = {
    "start": {"session_id": SESSION},
    "resume": {"resume_session_id": SESSION},
}


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_commit_and_push_are_denied(kind: str) -> None:
    argv = build_claude_argv("do a thing", **INVOCATIONS[kind])
    denied = flag_value(argv, "--disallowedTools")
    assert denied == DISALLOWED_TOOLS
    assert "Bash(git commit:*)" in denied
    assert "Bash(git push:*)" in denied


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_dispatched_agents_inherit_the_users_mcp_servers(kind: str) -> None:
    """Inheriting the user's MCP config is a feature, so nothing may isolate the child.

    Without this, an agent dispatched from here could not reach owlex, argent, or any other server
    the user has configured — and the flag that breaks it is a one-word addition.
    """
    argv = build_claude_argv("do a thing", **INVOCATIONS[kind])

    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv


@pytest.mark.parametrize("flag", FORBIDDEN_FLAGS)
def test_assert_safe_rejects_flags_that_isolate_the_agent(flag: str) -> None:
    argv = build_claude_argv("do a thing", session_id=SESSION)
    argv.append(flag)

    with pytest.raises(UnsafeInvocationError, match="cut the dispatched agent off"):
        assert_safe(argv)


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_deny_list_is_a_single_argv_value(kind: str) -> None:
    """`--disallowedTools` is variadic, so the patterns must not be separate arguments."""
    argv = build_claude_argv("do a thing", **INVOCATIONS[kind])
    assert argv.count("--disallowedTools") == 1
    following = argv[argv.index("--disallowedTools") + 1 :]
    assert following[0] == DISALLOWED_TOOLS
    assert not any(item.startswith("Bash(") for item in following[1:])


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_required_cli_shape(kind: str) -> None:
    argv = build_claude_argv("do a thing", max_turns=9, **INVOCATIONS[kind])
    assert argv[:3] == ["claude", "-p", "do a thing"]
    assert flag_value(argv, "--output-format") == "stream-json"
    assert flag_value(argv, "--permission-mode") == "bypassPermissions"
    assert flag_value(argv, "--max-turns") == "9"
    # `claude -p --output-format stream-json` refuses to start without this.
    assert "--verbose" in argv


def test_start_uses_session_id_and_resume_uses_resume() -> None:
    start = build_claude_argv("x", session_id=SESSION)
    assert flag_value(start, "--session-id") == SESSION
    assert "--resume" not in start

    resume = build_claude_argv("x", resume_session_id=SESSION)
    assert flag_value(resume, "--resume") == SESSION
    # The two flags conflict, so a resumed run must not carry both.
    assert "--session-id" not in resume


def test_model_is_optional() -> None:
    assert "--model" not in build_claude_argv("x", session_id=SESSION)
    assert flag_value(build_claude_argv("x", session_id=SESSION, model="opus"), "--model") == "opus"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="neither-session"),
        pytest.param({"session_id": SESSION, "resume_session_id": SESSION}, id="both-sessions"),
    ],
)
def test_session_arguments_are_mutually_exclusive(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_claude_argv("x", **kwargs)


@pytest.mark.parametrize("prompt", ["", "   "])
def test_empty_prompt_rejected(prompt: str) -> None:
    with pytest.raises(ValueError):
        build_claude_argv(prompt, session_id=SESSION)


def test_zero_max_turns_rejected() -> None:
    with pytest.raises(ValueError):
        build_claude_argv("x", session_id=SESSION, max_turns=0)


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda a: [x for x in a if x != "--verbose"], id="drops-verbose"),
        pytest.param(
            lambda a: [x for x in a if x not in ("--disallowedTools", DISALLOWED_TOOLS)],
            id="drops-deny-flag",
        ),
    ],
)
def test_assert_safe_rejects_a_stripped_argv(tamper) -> None:
    argv = tamper(build_claude_argv("x", session_id=SESSION))
    with pytest.raises(UnsafeInvocationError):
        assert_safe(argv)


def test_assert_safe_rejects_a_weakened_deny_list() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    argv[argv.index("--disallowedTools") + 1] = "Bash(git push:*)"
    with pytest.raises(UnsafeInvocationError):
        assert_safe(argv)


def test_assert_safe_rejects_a_changed_permission_mode() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    argv[argv.index("--permission-mode") + 1] = "acceptEdits"
    with pytest.raises(UnsafeInvocationError):
        assert_safe(argv)


@pytest.mark.parametrize("flag", ["--disallowedTools", "--permission-mode", "--verbose"])
def test_a_prompt_that_looks_like_a_flag_is_not_mistaken_for_one(flag: str) -> None:
    """The prompt is arbitrary caller text and must not be scanned as part of the option region."""
    argv = build_claude_argv(flag, session_id=SESSION)
    assert argv[2] == flag
    assert_safe(argv)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["claude", "-p"], id="no-options"),
        pytest.param(["claude", "--print", "x", "--verbose"], id="long-print-flag"),
        pytest.param(["not-claude", "-p", "x", "--verbose"], id="wrong-binary"),
    ],
)
def test_assert_safe_refuses_an_argv_layout_it_cannot_reason_about(argv: list[str]) -> None:
    """The prompt-at-index-2 assumption is verified, so a reorder fails loudly."""
    with pytest.raises(UnsafeInvocationError, match="unrecognised argv layout"):
        assert_safe(argv)


def test_assert_safe_rejects_a_weaker_duplicate_of_the_deny_list() -> None:
    """A later duplicate can win at the CLI, so a correct first occurrence is not enough."""
    argv = build_claude_argv("x", session_id=SESSION)
    argv += ["--disallowedTools", "Bash(true:*)"]
    with pytest.raises(UnsafeInvocationError, match="appears 2 times"):
        assert_safe(argv)
