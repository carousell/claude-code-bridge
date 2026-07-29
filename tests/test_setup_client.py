"""Config wiring. Every test runs against tmp_path — never a real Claude config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_code_bridge import setup_client
from claude_code_bridge.setup_client import (
    SERVER_KEY,
    SetupError,
    build_path_env,
    load_config,
    merge_entry,
    setup,
)

EXISTING = {
    "mcpServers": {
        "some-other-server": {"command": "/usr/local/bin/other"},
    },
    "preferences": {"sidebarMode": "chat", "nested": {"keep": True}},
    "coworkUserFilesPath": "/Users/someone/Claude",
}


@pytest.fixture(autouse=True)
def fake_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stand in for the installed server and the claude CLI, wherever they'd really be."""
    bin_dir = tmp_path / "bin"
    claude_dir = tmp_path / "elsewhere"
    bin_dir.mkdir()
    claude_dir.mkdir()
    server = bin_dir / "claude-code-bridge-server"
    claude = claude_dir / "claude"
    server.touch(mode=0o755)
    claude.touch(mode=0o755)

    paths = {"claude-code-bridge-server": str(server), "claude": str(claude)}
    monkeypatch.setattr(setup_client.shutil, "which", lambda name: paths.get(name))
    return paths


def write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_merge_preserves_other_servers_and_unrelated_keys(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.json", EXISTING)

    result = setup(config_path)

    on_disk = json.loads(config_path.read_text())
    assert on_disk == result
    assert on_disk["mcpServers"]["some-other-server"] == {"command": "/usr/local/bin/other"}
    assert on_disk["preferences"] == EXISTING["preferences"]
    assert on_disk["coworkUserFilesPath"] == EXISTING["coworkUserFilesPath"]
    assert SERVER_KEY in on_disk["mcpServers"]


def test_entry_uses_absolute_paths_the_gui_can_resolve(
    tmp_path: Path, fake_binaries: dict[str, str]
) -> None:
    """A GUI-launched app has no useful PATH, so both the command and claude must be absolute."""
    config_path = tmp_path / "config.json"

    entry = setup(config_path)["mcpServers"][SERVER_KEY]

    assert entry["command"] == fake_binaries["claude-code-bridge-server"]
    path_dirs = entry["env"]["PATH"].split(":")
    assert str(Path(fake_binaries["claude"]).parent) in path_dirs
    assert str(Path(fake_binaries["claude-code-bridge-server"]).parent) in path_dirs


def test_creates_the_config_when_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "config.json"

    setup(config_path)

    assert SERVER_KEY in json.loads(config_path.read_text())["mcpServers"]


def test_backs_up_the_previous_config(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.json", EXISTING)

    setup(config_path)

    backups = list(tmp_path.glob("config.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == EXISTING


def test_is_idempotent(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.json", EXISTING)

    first = setup(config_path)
    second = setup(config_path)

    assert first == second
    assert len(json.loads(config_path.read_text())["mcpServers"]) == 2


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.json", EXISTING)

    result = setup(config_path, dry_run=True)

    assert SERVER_KEY in result["mcpServers"]
    assert json.loads(config_path.read_text()) == EXISTING
    assert not list(tmp_path.glob("config.json.bak-*"))


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("{not json at all", id="malformed"),
        pytest.param("[1, 2, 3]", id="json-but-not-an-object"),
    ],
)
def test_refuses_to_overwrite_a_config_it_cannot_understand(
    tmp_path: Path, contents: str
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(SetupError, match="Refusing to overwrite"):
        setup(config_path)

    assert config_path.read_text() == contents


def test_refuses_when_mcp_servers_is_the_wrong_shape() -> None:
    with pytest.raises(SetupError, match="Refusing to overwrite"):
        merge_entry({"mcpServers": "nonsense"}, {"command": "x"})


def test_an_empty_file_is_treated_as_a_fresh_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("   \n", encoding="utf-8")

    assert load_config(config_path) == {}


def test_missing_claude_is_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_binaries: dict[str, str]
) -> None:
    """Setup must still work so someone can install Claude Code afterwards."""
    monkeypatch.setattr(
        setup_client.shutil,
        "which",
        lambda name: fake_binaries["claude-code-bridge-server"]
        if name == "claude-code-bridge-server"
        else None,
    )
    config_path = tmp_path / "config.json"

    entry = setup(config_path)["mcpServers"][SERVER_KEY]

    assert entry["command"] == fake_binaries["claude-code-bridge-server"]


def test_fails_clearly_when_the_server_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_client.shutil, "which", lambda name: None)

    with pytest.raises(SetupError, match="uv tool install"):
        setup(tmp_path / "config.json")


def test_path_env_keeps_the_symlink_dir_not_the_versioned_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code installs ~/.local/bin/claude as a symlink into a versioned directory.

    Resolving it would pin PATH to today's version, so dispatch would break the next time Claude
    Code updates itself.
    """
    stable_bin = tmp_path / "local" / "bin"
    versioned = tmp_path / "share" / "claude" / "versions" / "2.1.220"
    stable_bin.mkdir(parents=True)
    versioned.mkdir(parents=True)
    real_claude = versioned / "claude"
    real_claude.touch(mode=0o755)
    claude_link = stable_bin / "claude"
    claude_link.symlink_to(real_claude)
    server = stable_bin / "claude-code-bridge-server"
    server.touch(mode=0o755)

    monkeypatch.setattr(
        setup_client.shutil,
        "which",
        lambda name: {"claude": str(claude_link), "claude-code-bridge-server": str(server)}.get(name),
    )

    path_dirs = setup(tmp_path / "config.json")["mcpServers"][SERVER_KEY]["env"]["PATH"].split(":")

    assert str(stable_bin) in path_dirs
    assert str(versioned) not in path_dirs


@pytest.mark.parametrize(
    ("command", "ephemeral"),
    [
        ("/Users/x/proj/.venv/bin/claude-code-bridge-server", True),
        ("/Users/x/.local/bin/claude-code-bridge-server", False),
    ],
)
def test_detects_a_virtualenv_install(command: str, ephemeral: bool) -> None:
    assert setup_client.is_ephemeral_install(command) is ephemeral


def test_path_env_is_deduplicated(fake_binaries: dict[str, str]) -> None:
    path_env = build_path_env(
        fake_binaries["claude-code-bridge-server"],
        fake_binaries["claude"],
        fake_binaries["claude"],
    )
    dirs = path_env.split(":")

    assert len(set(dirs)) == len(dirs)


def test_path_env_includes_git_so_repo_validation_works(fake_binaries: dict[str, str]) -> None:
    """The server shells out to `git`; a git outside the standard dirs must still be reachable."""
    path_env = build_path_env(
        fake_binaries["claude-code-bridge-server"], None, "/opt/custom/bin/git"
    )

    assert "/opt/custom/bin" in path_env.split(":")


def test_path_env_keeps_standard_dirs_even_if_absent() -> None:
    """A missing PATH entry is harmless at exec time, and keeps working if a tool lands there later."""
    path_env = build_path_env("/nowhere/bin/server", None)

    assert "/opt/homebrew/bin" in path_env.split(":")
    assert "/usr/bin" in path_env.split(":")


def test_backups_never_overwrite_each_other(tmp_path: Path) -> None:
    """Two runs in the same second must not destroy the first backup."""
    config_path = write(tmp_path / "config.json", EXISTING)
    setup(config_path)
    second_state = json.loads(config_path.read_text())

    setup(config_path)

    backups = sorted(tmp_path.glob("config.json.bak-*"))
    assert len(backups) == 2
    contents = [json.loads(b.read_text()) for b in backups]
    assert EXISTING in contents
    assert second_state in contents


def test_preserves_file_permissions(tmp_path: Path) -> None:
    """NamedTemporaryFile is 0600; replacing must not silently tighten the user's config."""
    config_path = write(tmp_path / "config.json", EXISTING)
    config_path.chmod(0o644)

    setup(config_path)

    assert config_path.stat().st_mode & 0o777 == 0o644


def test_rewrites_a_symlinked_config_through_the_link(tmp_path: Path) -> None:
    real = write(tmp_path / "real.json", EXISTING)
    link = tmp_path / "config.json"
    link.symlink_to(real)

    setup(link)

    assert link.is_symlink()
    assert SERVER_KEY in json.loads(real.read_text())["mcpServers"]


def test_refuses_to_write_if_the_config_changed_underneath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop app owns this file; a concurrent write must not be silently discarded."""
    config_path = write(tmp_path / "config.json", EXISTING)
    intruder = {"mcpServers": {}, "preferences": {"changed": True}}

    original_merge = setup_client.merge_entry

    def merge_while_someone_else_writes(config, entry):
        # Lands inside the window between reading the config and replacing it.
        write(config_path, intruder)
        return original_merge(config, entry)

    monkeypatch.setattr(setup_client, "merge_entry", merge_while_someone_else_writes)

    with pytest.raises(SetupError, match="changed while it was being edited"):
        setup(config_path)

    # The intruder's write survives untouched.
    assert json.loads(config_path.read_text()) == intruder
    assert not list(tmp_path.glob("config.json.bak-*"))


def test_cli_dry_run_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = write(tmp_path / "config.json", EXISTING)

    code = setup_client.main(["--config", str(config_path), "--dry-run"])

    assert code == 0
    assert SERVER_KEY in capsys.readouterr().out
    assert json.loads(config_path.read_text()) == EXISTING


def test_cli_reports_an_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup_client.shutil, "which", lambda name: None)

    code = setup_client.main(["--config", str(tmp_path / "config.json")])

    assert code == 1
    assert "error:" in capsys.readouterr().err
