from __future__ import annotations

import json
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from .errors import CliError

SOURCE = {
    "schemaVersion": 1,
    "source": "https://github.com/DimaPhil/telegram-proxy-api",
    "package": "telegram-proxy-cli",
}
MARKER = "source.json"


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def managed(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir() or (path / MARKER).is_symlink():
        return False
    try:
        return bool(json.loads((path / MARKER).read_text()) == SOURCE)
    except (OSError, ValueError):
        return False


def install_skill(*, home: Path | None = None) -> dict[str, str]:
    home = home or Path.home()
    target = home / ".agents" / "skills" / "tg"
    adapter = home / ".claude" / "skills" / "tg"
    link = "../../.agents/skills/tg"
    if exists(target) and not managed(target):
        raise CliError(f"Unmanaged skill preserved at {target}. Move it aside, then run tg skill install.")
    if exists(adapter) and not (adapter.is_symlink() and adapter.resolve() == target.resolve()):
        raise CliError(f"Unmanaged Claude skill preserved at {adapter}. Move it aside, then run tg skill install.")
    target.parent.mkdir(parents=True, exist_ok=True)
    adapter.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tg-skill-", dir=target.parent))
    backup = stage.with_name(stage.name + "-backup")
    try:
        source = files("telegram_proxy_cli").joinpath("skills", "tg")
        for name in ("SKILL.md", MARKER):
            (stage / name).write_bytes(source.joinpath(name).read_bytes())
        if exists(target):
            target.rename(backup)
        try:
            stage.rename(target)
        except OSError as replacement_error:
            if exists(backup):
                try:
                    backup.rename(target)
                except OSError:
                    raise CliError(
                        f"Skill update and rollback failed. Recovery copy remains at {backup}."
                    ) from replacement_error
            raise
        if not exists(adapter):
            adapter.symlink_to(link, target_is_directory=True)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"skill": str(target), "claude_adapter": str(adapter)}
