from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from telegram_proxy_cli.catalog import operations
from telegram_proxy_cli.completion import BEGIN, candidates, install_completion, script
from telegram_proxy_cli.errors import CliError
from telegram_proxy_cli.main import build_parser, main


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ([""], "chats"),
        (["messages", ""], "context"),
        (["messages", "get", "--"], "--message-id"),
        (["media", "list", "--include-album", ""], "false"),
        (["operations", "describe", "messages_"], "messages_get"),
        (["operations", "call", "messages_get", ""], "--params"),
        (["--api-url", "https://api.example", "chats", ""], "list"),
        (["completion", "install", ""], "--no-shell-init"),
    ],
)
def test_candidates_follow_parser(words: list[str], expected: str) -> None:
    catalog = operations()
    result = candidates(build_parser(catalog), words, [operation.id for operation in catalog])
    assert expected in result


def test_candidates_do_not_suggest_flags_for_token_value() -> None:
    catalog = operations()
    assert candidates(build_parser(catalog), ["login", "--url", ""], []) == []


def test_install_is_idempotent_and_preserves_shell_configuration(tmp_path: Path) -> None:
    config = tmp_path / ".zshrc"
    config.write_text("# existing user configuration\nexport EDITOR=vim\n")
    result = install_completion(home=tmp_path)
    snapshot = config.read_text()
    assert snapshot.startswith("# existing user configuration\nexport EDITOR=vim\n")
    assert snapshot.count(BEGIN) == 1
    assert "compdef _tg tg" in snapshot
    assert len(result["installed"]) == 3
    install_completion(home=tmp_path)
    assert config.read_text() == snapshot


def test_unmanaged_completion_conflict_causes_no_partial_writes(tmp_path: Path) -> None:
    target = tmp_path / ".config/fish/completions/tg.fish"
    target.parent.mkdir(parents=True)
    target.write_text("user completion")
    with pytest.raises(CliError, match="Unmanaged completion"):
        install_completion(home=tmp_path)
    assert target.read_text() == "user completion"
    assert not (tmp_path / ".local").exists()


def test_no_shell_init_supports_managed_dotfiles(tmp_path: Path) -> None:
    source = tmp_path / "source-zshrc"
    source.write_text("# managed elsewhere")
    (tmp_path / ".zshrc").symlink_to(source)
    with pytest.raises(CliError, match="symlink"):
        install_completion(home=tmp_path)
    install_completion(home=tmp_path, shell_init=False)
    assert source.read_text() == "# managed elsewhere"
    assert (tmp_path / ".local/share/zsh/site-functions/_tg").is_file()


def test_completion_cli_plain_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion", "candidates", "--", "chats", ""]) == 0
    assert "list" in capsys.readouterr().out.splitlines()
    assert main(["completion", "zsh"]) == 0
    assert capsys.readouterr().out.startswith("#compdef tg\n")


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_script_syntax(shell: str, tmp_path: Path) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    path = tmp_path / f"tg.{shell}"
    path.write_text(script(shell))
    result = subprocess.run([executable, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_adapter_executes_real_candidate_command(shell: str, tmp_path: Path) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    path = tmp_path / f"tg.{shell}"
    path.write_text(script(shell))
    if shell == "bash":
        command = f'source {shlex.quote(str(path))}; COMP_WORDS=(tg messages ""); COMP_CWORD=2; _tg_complete; printf "%s\\n" "${{COMPREPLY[@]}}"'
    else:
        command = f'_describe() {{ print -rl -- "${{suggestions[@]}}"; }}; words=(tg messages ""); CURRENT=3; source {shlex.quote(str(path))}'
    environment = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    result = subprocess.run([executable, "-c", command], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "context" in result.stdout.splitlines()
    assert "search" in result.stdout.splitlines()
