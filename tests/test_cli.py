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


def with_extra_option(argv: list[str], *tokens: str) -> list[str]:
    """Insert tokens into the option region, just before the `--` separator."""
    sep = argv.index("--")
    return argv[:sep] + list(tokens) + argv[sep:]


def replacing(argv: list[str], flag: str, *replacement: str) -> list[str]:
    """Replace a `flag value` pair in the option region with arbitrary tokens."""
    i = argv.index(flag)
    return argv[:i] + list(replacement) + argv[i + 2 :]


# ---- basic shape ----


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
    tampered = with_extra_option(argv, flag)

    with pytest.raises(UnsafeInvocationError, match="cut the dispatched agent off"):
        assert_safe(tampered)


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
    """Rewritten for the new layout: the prompt used to sit bare at argv[2]; it now sits alone,
    behind `--`, at the end of argv — `claude -p -- <prompt> <flags>` would make every later flag
    a positional, so the prompt had to move rather than merely be re-marked in place."""
    prompt = "do a thing"
    argv = build_claude_argv(prompt, max_turns=9, **INVOCATIONS[kind])
    assert argv[:2] == ["claude", "-p"]
    assert argv[-2:] == ["--", prompt]
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


# ---- the prompt is arbitrary caller text, never parsed as an option ----


@pytest.mark.parametrize(
    "prompt",
    [
        "--dangerously-skip-permissions",
        "--permission-mode=plan",
        "--disallowed-tools=",
    ],
)
def test_an_option_shaped_prompt_is_delivered_as_text_not_parsed(prompt: str) -> None:
    """The three shapes reproduced against the old argv layout (see the plan file), now landing
    after `--` as the sole positional.

    This proves the *construction*: the token sits alone after `--` and assert_safe accepts it as
    ordinary prompt text rather than treating it as part of the option region. It does not by
    itself prove live CLI delivery — that was measured separately against the real binary (see the
    module docstring) and is not re-asserted here.
    """
    argv = build_claude_argv(prompt, session_id=SESSION)
    assert argv[-2:] == ["--", prompt]
    assert_safe(argv)  # does not raise


# ---- the reproduced bypasses, now refused ----


def test_attached_permission_mode_is_refused() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = replacing(argv, "--permission-mode", "--permission-mode=bypassPermissions")
    with pytest.raises(UnsafeInvocationError):
        assert_safe(tampered)


def test_bare_dangerously_skip_permissions_is_refused() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--dangerously-skip-permissions")
    with pytest.raises(UnsafeInvocationError, match="dangerously-skip-permissions"):
        assert_safe(tampered)


def test_disallowed_tools_alias_is_refused() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = replacing(argv, "--disallowedTools", "--disallowed-tools=Bash(x)")
    with pytest.raises(UnsafeInvocationError):
        assert_safe(tampered)


# ---- strict canonical-option walker: attached forms, aliases, unknown tokens ----

ATTACHED_FORMS = [
    "--verbose=true",
    "--output-format=stream-json",
    "--permission-mode=bypassPermissions",
    f"--disallowedTools={DISALLOWED_TOOLS}",
    "--max-turns=5",
    "--model=opus",
    f"--session-id={SESSION}",
    f"--resume={SESSION}",
]


@pytest.mark.parametrize("token", ATTACHED_FORMS)
def test_attached_form_of_any_written_flag_is_refused(token: str) -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, token)
    with pytest.raises(UnsafeInvocationError, match="unrecognised option token"):
        assert_safe(tampered)


@pytest.mark.parametrize("alias", ["--allowed-tools", "--disallowed-tools", "--allowedTools"])
def test_documented_alias_is_refused(alias: str) -> None:
    """`--allowed-tools`/`--disallowed-tools` are documented claude aliases this module never
    writes; `--allowedTools` is the camelCase counterpart to the one it does. None may be admitted
    just because they resemble something legitimate."""
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, alias, "Bash(x)")
    with pytest.raises(UnsafeInvocationError, match="unrecognised option token"):
        assert_safe(tampered)


def test_unknown_token_is_refused() -> None:
    """`--effort` is a real claude option this module deliberately never writes — it must stay
    unknown and rejected rather than being copied in from a sibling package."""
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--effort", "high")
    with pytest.raises(UnsafeInvocationError, match="unrecognised option token"):
        assert_safe(tampered)


def test_assert_safe_rejects_a_stray_token_the_variadic_flag_would_swallow() -> None:
    """`--disallowedTools` is variadic at the real CLI, so a stray token right after its value
    would otherwise be silently absorbed as another pattern instead of being refused."""
    argv = build_claude_argv("x", session_id=SESSION)
    i = argv.index("--disallowedTools")
    tampered = argv[: i + 2] + ["Bash(extra:*)"] + argv[i + 2 :]
    with pytest.raises(UnsafeInvocationError, match="unrecognised option token"):
        assert_safe(tampered)


# ---- `--` separator and positional arity ----


def test_assert_safe_rejects_argv_with_no_separator_at_all() -> None:
    with pytest.raises(UnsafeInvocationError, match="without a `--` separator"):
        assert_safe(["claude", "-p"])


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_assert_safe_requires_the_separator(kind: str) -> None:
    argv = build_claude_argv("x", **INVOCATIONS[kind])
    tampered = [t for t in argv if t != "--"]
    with pytest.raises(UnsafeInvocationError, match="without a `--` separator"):
        assert_safe(tampered)


def test_assert_safe_rejects_extra_tokens_after_the_prompt() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = argv + ["extra"]
    with pytest.raises(UnsafeInvocationError, match="exactly one positional"):
        assert_safe(tampered)


def test_assert_safe_rejects_no_positional_after_separator() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = argv[:-1]  # drops the prompt, leaves the trailing `--`
    with pytest.raises(UnsafeInvocationError, match="exactly one positional"):
        assert_safe(tampered)


def test_assert_safe_refuses_a_blank_prompt_even_though_arity_is_right() -> None:
    """Arity is not enough — content matters too.

    `build_claude_argv` refuses a blank prompt, so a blank one here means something else assembled
    this argv. The two layers have to agree: this is the check re-run at spawn time, so it cannot be
    the weaker of the pair.
    """
    argv = build_claude_argv("x", session_id=SESSION)

    for blank in ("", "   ", "\n"):
        tampered = argv[:-1] + [blank]
        with pytest.raises(UnsafeInvocationError, match="blank"):
            assert_safe(tampered)


# ---- caller-supplied --model: a second injection vector, independent of the prompt ----


@pytest.mark.parametrize("model", ["", "   "], ids=["empty", "whitespace-only"])
def test_a_blank_model_is_omitted_rather_than_rejected(model: str) -> None:
    """Blank has always meant "no model chosen", and rejecting it breaks old sessions.

    The previous builder tested `if model:` and simply left the flag out, so records written then
    persisted `""` — and a resume passes that stored value straight back in. Raising on it would
    make every such pre-existing session unresumable, which is a regression rather than a fix.
    """
    argv = build_claude_argv("x", session_id=SESSION, model=model)

    assert "--model" not in argv
    assert_safe(argv)


def test_assert_safe_refuses_a_blank_model_even_though_construction_normalises_it() -> None:
    """The two layers must agree: `assert_safe` is the spawn-time re-check, so it cannot be the
    weaker of the two. The builder never emits a blank `--model`, so one here means something else
    assembled this argv."""
    argv = build_claude_argv("x", session_id=SESSION, model=None)
    cut = argv.index("--")

    for blank in ("", "   "):
        with pytest.raises(UnsafeInvocationError, match="names no model"):
            assert_safe(argv[:cut] + ["--model", blank] + argv[cut:])


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("--dangerously-skip-permissions", id="named-dangerous-flag"),
        pytest.param("--", id="fake-separator"),
    ],
)
def test_model_injection_is_rejected_at_construction(model: str) -> None:
    with pytest.raises(ValueError):
        build_claude_argv("x", session_id=SESSION, model=model)


def test_assert_safe_independently_rejects_a_flag_shaped_model() -> None:
    """Defense in depth: even an argv that bypassed build_claude_argv's own ValueError is refused
    by assert_safe's generic value-flag check, not just the named `--dangerously-skip-permissions`
    special case."""
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--model", "--not-a-real-flag")
    with pytest.raises(UnsafeInvocationError, match="rather than a value"):
        assert_safe(tampered)


def test_assert_safe_rejects_a_fake_separator_as_a_model_value() -> None:
    """A model value of literally `--` cannot smuggle a fake separator past the real one: whatever
    mechanism catches it, the argv must be refused."""
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--model", "--")
    with pytest.raises(UnsafeInvocationError):
        assert_safe(tampered)


# ---- `-p` layout admits only the exact boolean flag ----


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["claude"], id="too-short"),
        pytest.param(["claude", "--print", "--", "x"], id="long-print-flag"),
        pytest.param(["claude", "-print", "--", "x"], id="malformed-p-prefix"),
        pytest.param(["not-claude", "-p", "--", "x"], id="wrong-binary"),
    ],
)
def test_assert_safe_refuses_an_argv_layout_it_cannot_reason_about(argv: list[str]) -> None:
    """Only exactly `["claude", "-p"]` is admitted as the required prefix."""
    with pytest.raises(UnsafeInvocationError, match="unrecognised argv layout"):
        assert_safe(argv)


# ---- duplicates and altered values ----


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


def test_assert_safe_rejects_an_altered_output_format() -> None:
    """Nothing currently protects stream parsing from an altered --output-format value."""
    argv = build_claude_argv("x", session_id=SESSION)
    argv[argv.index("--output-format") + 1] = "json"
    with pytest.raises(UnsafeInvocationError, match="--output-format"):
        assert_safe(argv)


def test_assert_safe_rejects_a_weaker_duplicate_of_the_deny_list() -> None:
    """A later duplicate can win at the CLI, so a correct first occurrence is not enough."""
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--disallowedTools", "Bash(true:*)")
    with pytest.raises(UnsafeInvocationError, match="appears 2 times"):
        assert_safe(tampered)


def test_assert_safe_rejects_a_duplicate_model() -> None:
    argv = build_claude_argv("x", session_id=SESSION, model="opus")
    tampered = with_extra_option(argv, "--model", "haiku")
    with pytest.raises(UnsafeInvocationError, match=r"--model appears 2 times"):
        assert_safe(tampered)


def test_assert_safe_rejects_a_duplicate_max_turns() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--max-turns", "1")
    with pytest.raises(UnsafeInvocationError, match=r"--max-turns appears 2 times"):
        assert_safe(tampered)


def test_assert_safe_rejects_both_session_flags_together() -> None:
    argv = build_claude_argv("x", session_id=SESSION)
    tampered = with_extra_option(argv, "--resume", SESSION)
    with pytest.raises(UnsafeInvocationError, match="session-id/--resume"):
        assert_safe(tampered)


# ---- resume gets the same treatment as start ----


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_resume_argv_is_as_strictly_checked_as_start(kind: str) -> None:
    argv = build_claude_argv("x", **INVOCATIONS[kind])
    tampered = with_extra_option(argv, "--dangerously-skip-permissions")
    with pytest.raises(UnsafeInvocationError):
        assert_safe(tampered)
