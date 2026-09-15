"""Construction of the `claude` command line.

Every headless invocation in this package is built here, by `build_claude_argv`. That is
deliberate: the git commit/push block is a safety boundary rather than a convention, so there
must be no second place where an argv can be assembled without it.

CLI facts established by measurement, not assumption. Established against `claude` 2.1.272 —
this repo's CLAUDE.md previously documented 2.1.220; that is a discrepancy worth noting, not a
value to silently overwrite, since a later CLI upgrade is exactly when these facts should be
re-verified rather than trusted:

* `-p`/`--print` is a **boolean** flag, not one that takes the prompt as its argument — the
  CLI's own usage line is `claude [options] [command] [prompt]`, with `prompt` a separate
  declared positional. So a prompt token that happens to exactly equal a real claude option name
  (e.g. `--dangerously-skip-permissions`) is parsed by claude as that option, not as text, unless
  something marks where options end. Measured with the old `["claude", "-p", <prompt>, ...flags]`
  shape and a prompt of `--disallowed-tools=`: claude initialised, ran SessionStart hooks, then
  exited 1 with `Input must be provided either through stdin or as a prompt argument when using
  --print`. That is the honest severity — the run fails outright, rather than the deny list being
  silently cleared and an agent turn proceeding with it empty. What is established is narrower but
  still worth fixing: arbitrary caller prompt text can alter claude's *startup* options, some of
  which (SessionStart hooks, in that run) took effect before the missing-input validation ran.
* The restructured shape — options first, then `--`, then the prompt alone — was measured working
  end to end with this package's exact flags:
  `claude -p --output-format stream-json --verbose --permission-mode bypassPermissions
  --disallowedTools "Bash(git commit:*),Bash(git push:*)" --max-turns 3 --session-id <uuid> --
  "--disallowed-tools= is just text here. Reply with exactly: PORTOK"` returned `result: PORTOK`,
  `is_error: false`, with the session id preserved — the option-shaped prompt was delivered as
  literal text, not parsed.
* The same held for this package's own `--resume` shape, separately measured (the polybridge
  measurement above used `--session-id` only): `--resume <id> -- "--disallowed-tools= is just
  text here in a resumed session too. Reply with exactly: RESUME-OK"` returned `result:
  RESUME-OK`, `is_error: false`, and the same `session_id` as the run it resumed.
* `--disallowedTools` is variadic (`<tools...>`), so its patterns must stay a single
  comma-separated argv value, or the option would swallow whatever followed it.
* `--verbose` is mandatory with `-p --output-format stream-json`. Without it the CLI exits
  immediately with `Error: When using --print, --output-format=stream-json requires --verbose`.
* `--max-turns` works but is absent from `--help`.
"""

from __future__ import annotations

CLAUDE_BINARY = "claude"

# Deny rules handed to `--disallowedTools`. Comma-separated in a single argv value rather than
# space-separated: the option is variadic (`<tools...>`), so separate values would greedily
# absorb whatever follows them on the command line.
DISALLOWED_TOOLS = "Bash(git commit:*),Bash(git push:*)"

# Flags that would cut a dispatched agent off from the user's own MCP servers, settings, hooks and
# CLAUDE.md. Inheriting all of that is a deliberate feature — an agent dispatched from here is meant
# to be as capable as the user's own sessions — so these are refused rather than merely unused.
# Removing them would silently break, for example, reaching owlex or argent from a dispatched task.
FORBIDDEN_FLAGS = ("--strict-mcp-config", "--setting-sources", "--safe-mode", "--bare")

# Options this module ever writes — nothing more. `assert_safe` walks the option region against
# exactly these instead of searching/counting it, the way a search let non-canonical spellings
# through while claude still honoured them (attached `--flag=value` forms and documented long
# aliases such as `--disallowed-tools`/`--allowed-tools` are never emitted here, so admitting them
# would reopen the same hole under a different spelling). This module writes no `--allowedTools`
# and no `--effort`, so both stay unknown and refused rather than being copied in from elsewhere.
BOOLEAN_FLAGS = ("--verbose",)
VALUE_FLAGS = (
    "--output-format",
    "--permission-mode",
    "--disallowedTools",
    "--max-turns",
    "--model",
    "--session-id",
    "--resume",
)

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
    if model is not None and not model.strip():
        # Blank means "no model chosen", which is what it has always meant: the old builder tested
        # `if model:` and simply omitted the flag. Records written then persisted `""`, and those
        # are resumed by passing the stored value straight back here — so raising on blank would
        # make every such pre-existing session unresumable. Normalised rather than rejected.
        model = None
    if model is not None:
        if model.startswith("-"):
            # A caller-supplied injection vector independent of the prompt: unlike the prompt,
            # which is protected by landing after `--`, `model` rides inside the option region
            # itself, so a value shaped like a flag would become one.
            raise ValueError(f"model must not look like an option: {model!r}")

    argv = [
        CLAUDE_BINARY,
        "-p",
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
    if model is not None:
        argv += ["--model", model]
    if resume_session_id is not None:
        argv += ["--resume", resume_session_id]
    else:
        argv += ["--session-id", str(session_id)]
    # `--` then the prompt: last, and explicitly not parsed as an option however it looks. It
    # cannot simply be inserted at its old position (`claude -p -- <prompt> <flags>`) — that would
    # make every flag after it a positional instead. The prompt has to move to the end.
    argv += ["--", prompt]

    assert_safe(argv)
    return argv


def assert_safe(argv: list[str]) -> None:
    """Raise unless `argv` still carries the flags the safety model depends on.

    Only the option region — between `["claude", "-p"]` and the `--` separator — is inspected.
    Everything after `--` is the prompt: arbitrary caller text that may itself look like a flag,
    and mistaking it for one would both miss the real option and reject a valid run. See the
    module docstring for why `--` is load-bearing here rather than a fixed positional index.
    """
    if argv[:2] != [CLAUDE_BINARY, "-p"]:
        raise UnsafeInvocationError(f"unrecognised argv layout, cannot verify safety: {argv!r}")

    if "--" not in argv:
        raise UnsafeInvocationError(
            f"refusing to run claude without a `--` separator before the prompt, which stops "
            f"prompt text being parsed as options: {argv!r}"
        )
    options = argv[2 : argv.index("--")]

    # Positional arity, not just separator presence: claude declares exactly one positional (the
    # prompt), so anything other than exactly one token after `--` is not a shape this module ever
    # writes. This also catches an injected fake `--` earlier in the option region (e.g. a tampered
    # `--model --`): that would make `argv.index("--")` find the wrong separator, but the real `--`
    # and the prompt would then both land in `positionals`, so arity still fails.
    positionals = argv[argv.index("--") + 1 :]
    if len(positionals) != 1:
        raise UnsafeInvocationError(
            f"expected exactly one positional argument (the prompt) after `--`, found "
            f"{len(positionals)}: {argv!r}"
        )
    # Content as well as arity. `build_claude_argv` refuses a blank prompt, so one here means
    # something else assembled this argv — and the two layers must agree, since this is the check
    # re-run at spawn time and so cannot be the weaker of the pair.
    if not positionals[0].strip():
        raise UnsafeInvocationError(f"the prompt after `--` is blank: {argv!r}")

    # Kept ahead of the allowlist walk below for a clearer message even though it would refuse
    # this token too (as unrecognised) — this is the one form worth naming explicitly: a full
    # permission bypass, not merely a flag this module happens not to write.
    if "--dangerously-skip-permissions" in options:
        raise UnsafeInvocationError(
            f"--dangerously-skip-permissions discards the permission layer this module's safety "
            f"model depends on entirely: {argv!r}"
        )

    for flag in FORBIDDEN_FLAGS:
        if flag in options:
            raise UnsafeInvocationError(
                f"{flag} would cut the dispatched agent off from the user's MCP servers and "
                f"settings, which it is meant to inherit: {argv!r}"
            )

    seen = _parse_options(options, argv)

    _exactly_one(seen, "--verbose", argv)

    fmt = _exactly_one(seen, "--output-format", argv)
    if fmt != "stream-json":
        raise UnsafeInvocationError(
            f"--output-format was {fmt!r}, but only stream-json can be parsed into events: {argv!r}"
        )

    denied = _exactly_one(seen, "--disallowedTools", argv)
    if denied != DISALLOWED_TOOLS:
        raise UnsafeInvocationError(
            f"--disallowedTools was {denied!r}, expected {DISALLOWED_TOOLS!r}: {argv!r}"
        )

    mode = _exactly_one(seen, "--permission-mode", argv)
    if mode != "bypassPermissions":
        raise UnsafeInvocationError(f"unexpected --permission-mode {mode!r}: {argv!r}")

    turns_values = seen.get("--max-turns", [])
    if len(turns_values) != 1:
        raise UnsafeInvocationError(f"--max-turns appears {len(turns_values)} times: {argv!r}")
    turns = turns_values[0]
    if not turns.isdigit() or int(turns) < 1:
        raise UnsafeInvocationError(f"--max-turns was {turns!r}, expected a positive integer: {argv!r}")

    # Optional, unlike the flags above — but a second value would win silently at the CLI.
    model_values = seen.get("--model", [])
    if len(model_values) > 1:
        raise UnsafeInvocationError(f"--model appears {len(model_values)} times: {argv!r}")
    # The builder omits `--model` entirely rather than emitting a blank one, so a blank value here
    # means something else assembled this argv. An option-shaped value is already refused by the
    # walker, which rejects any flag value starting with `-`.
    if model_values and not model_values[0].strip():
        raise UnsafeInvocationError(f"--model names no model: {argv!r}")

    # Exactly one session flag, counted across both spellings: never both --session-id and
    # --resume, never neither, and it must actually name a session — a resume that silently
    # continued the wrong conversation is worse than one that fails outright.
    session_values = seen.get("--session-id", []) + seen.get("--resume", [])
    if len(session_values) != 1:
        raise UnsafeInvocationError(
            f"expected exactly one of --session-id/--resume, found {len(session_values)}: {argv!r}"
        )
    if not session_values[0].strip():
        raise UnsafeInvocationError(f"session flag with no session id: {argv!r}")


def _parse_options(options: list[str], argv: list[str]) -> dict[str, list[str]]:
    """Walk the option region strictly, refusing any token this module would not have written.

    Searching `flag in flags` or counting `flags.count(flag)` is not enough, because claude also
    honours `--flag=value` attached forms and documented long aliases (`--disallowed-tools`,
    `--allowed-tools`) this module never emits — a search sees the canonical form it wrote and
    passes while claude applies the non-canonical one that rode along. Measured evading the old
    search/count check: the attached `--permission-mode=bypassPermissions`, the bare
    `--dangerously-skip-permissions` (caught separately, above, for a clearer message), and the
    attached alias form `--disallowed-tools=...`. So every token not in canonical space-separated
    form is refused rather than skipped over.
    """
    seen: dict[str, list[str]] = {}
    index = 0
    while index < len(options):
        token = options[index]
        if token in BOOLEAN_FLAGS:
            seen.setdefault(token, []).append("")
            index += 1
        elif token in VALUE_FLAGS:
            if index + 1 >= len(options):
                raise UnsafeInvocationError(f"{token} has no value: {argv!r}")
            value = options[index + 1]
            # A value that looks like an option is not a value: claude's parser would read it as
            # the next flag. `model` is caller-supplied and lands here, so a model named
            # `--dangerously-skip-permissions` would otherwise smuggle an option into the region
            # this function exists to police.
            if value.startswith("-"):
                raise UnsafeInvocationError(
                    f"{token} was given {value!r}, which claude would parse as an option rather "
                    f"than a value: {argv!r}"
                )
            seen.setdefault(token, []).append(value)
            index += 2
        else:
            raise UnsafeInvocationError(
                f"unrecognised option token {token!r}: this module writes only canonical "
                f"space-separated options, and `--flag=value` or an alias spelling would apply "
                f"unnoticed: {argv!r}"
            )
    return seen


def _exactly_one(seen: dict[str, list[str]], flag: str, argv: list[str]) -> str:
    values = seen.get(flag, [])
    if len(values) != 1:
        raise UnsafeInvocationError(f"{flag} appears {len(values)} times: {argv!r}")
    return values[0]
