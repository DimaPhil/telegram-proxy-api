from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from .errors import CliError

MARKER = "# Managed by telegram-proxy-cli (https://github.com/DimaPhil/telegram-proxy-api)"
BEGIN = "# >>> telegram-proxy-cli completion >>>"
END = "# <<< telegram-proxy-cli completion <<<"


def candidates(parser: argparse.ArgumentParser, words: list[str], operation_ids: list[str]) -> list[str]:
    """Complete the parser itself; no second list of commands or option names."""
    prefix = words[-1] if words else ""
    pending: argparse.Action | None = None
    positional_index = 0
    for word in words[:-1]:
        if pending is not None:
            pending = None
            continue
        options = {option: action for action in parser._actions for option in action.option_strings}
        if word.split("=", 1)[0] in options:
            action = options[word.split("=", 1)[0]]
            if "=" not in word and action.nargs != 0:
                pending = action
            continue
        subcommands = next(
            (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None
        )
        if subcommands is not None and word in subcommands.choices:
            parser = subcommands.choices[word]
            positional_index = 0
            continue
        positional_index += 1
    if pending is not None:
        if pending.choices is not None:
            values = [str(value) for value in pending.choices]
        elif getattr(pending.type, "__name__", "") == "boolean":
            values = ["true", "false"]
        else:
            values = []
        return [value for value in values if value.startswith(prefix)]
    values = []
    positionals = [action for action in parser._actions if not action.option_strings]
    for action in parser._actions:
        values.extend(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            values.extend(str(choice) for choice in action.choices if choice != "candidates")
    if positional_index < len(positionals) and positionals[positional_index].dest == "operation_id":
        values.extend(operation_ids)
    return sorted(value for value in set(values) if value.startswith(prefix))


def script(shell: str) -> str:
    if shell == "bash":
        body = """_tg_complete() {
    local line
    COMPREPLY=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && COMPREPLY+=("$line")
    done < <(tg completion candidates -- "${COMP_WORDS[@]:1:$COMP_CWORD}" 2>/dev/null)
}
complete -o default -F _tg_complete tg
"""
    elif shell == "zsh":
        body = """#compdef tg
_tg() {
    local -a suggestions
    suggestions=("${(@f)$(tg completion candidates -- "${words[@]:1:$((CURRENT-1))}" 2>/dev/null)}")
    if (( ${#suggestions[@]} )); then
        _describe 'tg' suggestions
    else
        _files
    fi
}
_tg "$@"
"""
    elif shell == "fish":
        body = """function __tg_candidates
    set -l tokens (commandline -opc)
    tg completion candidates -- $tokens[2..-1] (commandline -ct) 2>/dev/null
end
complete -c tg -a '(__tg_candidates)'
"""
    else:
        raise CliError("Supported shells: bash, zsh, fish.")
    # zsh requires #compdef to be the first line for compinit discovery.
    if shell == "zsh":
        return body.replace("#compdef tg\n", f"#compdef tg\n{MARKER}\n", 1)
    return MARKER + "\n" + body


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tg-completion-", dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        else:
            os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _with_block(text: str, block: str) -> str:
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) >= text.index(END):
            raise CliError("Malformed tg completion markers in shell config; repair them before reinstalling.")
        start = text.index(BEGIN)
        stop = text.index(END) + len(END)
        return text[:start] + block + text[stop:]
    return text + ("\n" if text and not text.endswith("\n") else "") + block + "\n"


def install_completion(*, home: Path | None = None, shell_init: bool = True) -> dict[str, list[str]]:
    home = home or Path.home()
    destinations = {
        "bash": home / ".local/share/bash-completion/completions/tg",
        "zsh": home / ".local/share/zsh/site-functions/_tg",
        "fish": home / ".config/fish/completions/tg.fish",
    }
    writes: dict[Path, str] = {}
    for shell, path in destinations.items():
        if path.is_symlink() or (path.exists() and MARKER not in path.read_text().splitlines()[:2]):
            raise CliError(f"Unmanaged completion preserved at {path}. Move it aside before reinstalling.")
        writes[path] = script(shell)
    if shell_init:
        blocks = {
            ".bashrc": '[ -r "$HOME/.local/share/bash-completion/completions/tg" ] && . "$HOME/.local/share/bash-completion/completions/tg"',
            ".zshrc": 'fpath=("$HOME/.local/share/zsh/site-functions" $fpath)\nautoload -Uz compinit\n(( $+functions[compdef] )) || compinit\n# Register directly when an existing compinit cache predates this installation.\nautoload -Uz _tg\ncompdef _tg tg',
        }
        for name, integration in blocks.items():
            path = home / name
            if path.is_symlink():
                raise CliError(
                    f"Shell config {path} is a symlink; use --no-shell-init and configure completion in its managed source."
                )
            existing = path.read_text() if path.exists() else ""
            writes[path] = _with_block(existing, f"{BEGIN}\n{integration}\n{END}")
    # Validate every destination before writing any file.
    for path, content in writes.items():
        _atomic_write(path, content)
    return {
        "installed": [str(path) for path in destinations.values()],
        "shell_config": [str(path) for path in writes if path not in destinations.values()],
    }
