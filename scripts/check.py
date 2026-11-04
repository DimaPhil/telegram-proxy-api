"""The shared local, CI and release validation gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = [
        ["-m", "ruff", "check", "src", "tests", "scripts", "cli/src", "cli/tests"],
        ["-m", "ruff", "format", "--check", "src", "tests", "scripts", "cli/src", "cli/tests"],
        ["-m", "mypy"],
        ["scripts/export_openapi.py", "--check"],
        ["-m", "pytest", "-m", "not e2e"],
        ["-m", "pytest", "-c", "cli/pyproject.toml", "cli/tests"],
    ]
    for command in commands:
        subprocess.run([sys.executable, *command], cwd=root, check=True)


if __name__ == "__main__":
    main()
