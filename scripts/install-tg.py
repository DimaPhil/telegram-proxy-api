#!/usr/bin/env python3
"""Install a verified tg release into an isolated environment and install its agent skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import venv
from pathlib import Path

REPOSITORY = "DimaPhil/telegram-proxy-api"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def download(url: str, destination: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        destination.open("wb") as target,
    ):
        shutil.copyfileobj(response, target)


def verify(assets: Path, version: str) -> Path:
    name = f"telegram_proxy_cli-{version}-py3-none-any.whl"
    entries: dict[str, str] = {}
    for line in (assets / "SHA256SUMS").read_text().splitlines():
        digest, filename = line.split("  ", 1)
        if filename in entries or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Malformed or duplicate release checksum")
        entries[filename] = digest
    wheel = assets / name
    if name not in entries or hashlib.sha256(wheel.read_bytes()).hexdigest() != entries[name]:
        raise ValueError("CLI wheel checksum does not match the release")
    return wheel


def install(assets: Path, version: str, prefix: Path, bin_dir: Path) -> Path:
    wheel = verify(assets, version)
    prefix = prefix.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    launcher = bin_dir / "tg"
    if (launcher.exists() or launcher.is_symlink()) and (
        not launcher.is_symlink() or not launcher.resolve().is_relative_to(prefix / "releases")
    ):
        raise ValueError(f"Refusing to replace unmanaged executable {launcher}")
    environment = prefix / "releases" / f"{version}-{uuid.uuid4().hex[:12]}"
    try:
        # Symlinks also preserve relocatable/uv-managed interpreters' library lookup.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
        python = environment / "bin" / "python"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
        )
        executable = environment / "bin" / "tg"
        subprocess.run([str(executable), "--version"], check=True)
        subprocess.run([str(executable), "skill", "install", "--if-safe"], check=True)
        subprocess.run([str(executable), "completion", "install", "--if-safe"], check=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        temporary = bin_dir / f".tg-{uuid.uuid4().hex}"
        try:
            temporary.symlink_to(executable)
            os.replace(temporary, launcher)
        finally:
            temporary.unlink(missing_ok=True)
    except BaseException:
        shutil.rmtree(environment, ignore_errors=True)
        raise
    return launcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Exact X.Y.Z release (default: latest)")
    parser.add_argument(
        "--assets",
        type=Path,
        help="Use locally downloaded release assets; requires --version",
    )
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/tg")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    if sys.version_info < (3, 12) or os.name != "posix":
        parser.error("tg release installation requires Python 3.12+ on macOS or Linux")
    if args.version and not VERSION.fullmatch(args.version):
        parser.error("--version must be X.Y.Z")
    if args.assets and not args.version:
        parser.error("--assets requires --version")
    with tempfile.TemporaryDirectory(prefix="tg-install-") as temporary:
        assets = args.assets or Path(temporary)
        version = args.version
        if not args.assets:
            if not version:
                metadata = assets / "github-release.json"
                download(
                    f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                    metadata,
                )
                tag = json.loads(metadata.read_text())["tag_name"]
                if not isinstance(tag, str) or not tag.startswith("v") or not VERSION.fullmatch(tag[1:]):
                    raise ValueError("Latest release does not have a supported version")
                version = tag[1:]
            base = f"https://github.com/{REPOSITORY}/releases/download/v{version}"
            for name in (
                "SHA256SUMS",
                f"telegram_proxy_cli-{version}-py3-none-any.whl",
            ):
                download(f"{base}/{name}", assets / name)
        assert version is not None
        launcher = install(assets, version, args.prefix, args.bin_dir)
    print(f"Installed tg {version}: {launcher}")
    print(f"Ensure {launcher.parent} is on PATH, then run: tg login --help")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        sys.exit(1)
