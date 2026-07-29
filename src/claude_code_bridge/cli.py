"""Construction of the `claude` command line.

Every headless invocation in this package is built here, by `build_claude_argv`. That is
deliberate: the git commit/push block is a safety boundary rather than a convention, so there
must be no second place where an argv can be assembled without it.
"""

from __future__ import annotations

CLAUDE_BINARY = "claude"

# Deny rules handed to `--disallowedTools`. Comma-separated in a single argv value rather than
# space-separated: the option is variadic (`<tools...>`), so separate values would greedily
# absorb whatever follows them on the command line.
DISALLOWED_TOOLS = "Bash(git commit:*),Bash(git push:*)"

# Required, not stylistic: `claude -p --output-format stream-json` refuses to start without it.
_REQUIRED_FLAGS = ("--verbose", "--disallowedTools", "--permission-mode")

# Every argv this module builds starts ["claude", "-p", <prompt>]; options begin after the prompt.
_FLAGS_START = 3

DEFAULT_MAX_TURNS = 50


class UnsafeInvocationError(RuntimeError):
    """An argv was assembled without its safety flags intact."""


def build_claude_argv(
    prompt: str,
    *,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = None,
) -> list[str]:
    """Build the argv for one headless `claude` run.

    Pass `session_id` to start a fresh session under an id we choose, or `resume_session_id` to
    continue an existing one — exactly one of the two. They map to mutually exclusive CLI flags.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if (session_id is None) == (resume_session_id is None):
        raise ValueError("pass exactly one of session_id or resume_session_id")
    if max_turns < 1:
        raise ValueError(f"max_turns must be >= 1, got {max_turns}")

    argv = [
        CLAUDE_BINARY,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        DISALLOWED_TOOLS,
        "--max-turns",
        str(max_turns),
    ]
    if model:
        argv += ["--model", model]
    if resume_session_id is not None:
        argv += ["--resume", resume_session_id]
    else:
        argv += ["--session-id", str(session_id)]

    assert_safe(argv)
    return argv


def assert_safe(argv: list[str]) -> None:
    """Raise unless `argv` still carries the flags the safety model depends on.

    Only the option region is inspected. The prompt is arbitrary caller text that may itself look
    like a flag, and mistaking it for one would both miss the real option and reject a valid run.
    """
    # Checked rather than assumed: if the argv layout is ever reordered, this fails loudly instead
    # of quietly scanning the wrong slice and passing a weakened command line.
    if len(argv) <= _FLAGS_START or argv[0] != CLAUDE_BINARY or argv[1] != "-p":
        raise UnsafeInvocationError(f"unrecognised argv layout, cannot verify safety: {argv!r}")

    flags = argv[_FLAGS_START:]

    for flag in _REQUIRED_FLAGS:
        occurrences = flags.count(flag)
        if occurrences == 0:
            raise UnsafeInvocationError(f"refusing to run `claude` without {flag}: {argv!r}")
        if occurrences > 1:
            # A later duplicate can override the earlier one, so a single occurrence is required
            # rather than merely a correct first one.
            raise UnsafeInvocationError(f"{flag} appears {occurrences} times: {argv!r}")

    denied = flags[flags.index("--disallowedTools") + 1]
    if denied != DISALLOWED_TOOLS:
        raise UnsafeInvocationError(
            f"--disallowedTools was {denied!r}, expected {DISALLOWED_TOOLS!r}"
        )

    mode = flags[flags.index("--permission-mode") + 1]
    if mode != "bypassPermissions":
        raise UnsafeInvocationError(f"unexpected --permission-mode {mode!r}")
