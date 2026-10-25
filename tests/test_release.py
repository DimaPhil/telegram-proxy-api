from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def load_script(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("release.py")
installer = load_script("install-tg.py")


def test_version_selection() -> None:
    assert release.next_version([], None, "0.1.0") == "0.2.0"
    assert release.next_version(["v1.9.2", "v1.10.4", "junk", "v2.0.0-rc1"], None, "0.1.0") == "1.11.0"
    assert release.next_version(["v1.10.4"], "2.0.0", "0.1.0") == "2.0.0"
    for version in ("1.10.4", "0.9.0", "v2.0.0", "02.0.0", "2.0.0-rc1", "2.0.0\n"):
        with pytest.raises(ValueError):
            release.next_version(["v1.10.4"], version, "0.1.0")


def test_release_versions_both_packages_and_hashes(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
    release.set_version(project, "1.2.0")
    assert 'version = "1.2.0"' in project.read_text()
    release.checksums(tmp_path)
    assert (
        tmp_path / "SHA256SUMS"
    ).read_text() == f"{hashlib.sha256(project.read_bytes()).hexdigest()}  pyproject.toml\n"


def test_installer_verifies_wheel_before_mutating(tmp_path: Path) -> None:
    wheel = tmp_path / "telegram_proxy_cli-1.2.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    release.checksums(tmp_path)
    assert installer.verify(tmp_path, "1.2.0") == wheel
    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        installer.install(tmp_path, "1.2.0", tmp_path / "prefix", tmp_path / "bin")
    assert not (tmp_path / "prefix").exists()


def test_installer_preserves_unmanaged_command(tmp_path: Path) -> None:
    (tmp_path / "telegram_proxy_cli-1.2.0-py3-none-any.whl").write_bytes(b"wheel")
    release.checksums(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tg").write_text("user executable")
    with pytest.raises(ValueError, match="unmanaged"):
        installer.install(tmp_path, "1.2.0", tmp_path / "prefix", bin_dir)
    assert (bin_dir / "tg").read_text() == "user executable"


def test_install_switches_only_after_success_and_retains_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "telegram_proxy_cli-1.2.0-py3-none-any.whl").write_bytes(b"wheel")
    release.checksums(tmp_path)
    prefix, bin_dir = tmp_path / "prefix", tmp_path / "bin"
    previous = prefix / "releases" / "old" / "bin" / "tg"
    previous.parent.mkdir(parents=True)
    previous.touch()
    bin_dir.mkdir()
    (bin_dir / "tg").symlink_to(previous)

    def create_environment(path: Path) -> None:
        (path / "bin").mkdir(parents=True)
        (path / "bin/tg").touch()

    class Builder:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create(self, path: Path) -> None:
            create_environment(path)

    monkeypatch.setattr(installer.venv, "EnvBuilder", Builder)
    calls: list[list[str]] = []

    def fail_skill(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        if "skill" in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", fail_skill)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(tmp_path, "1.2.0", prefix, bin_dir)
    assert (bin_dir / "tg").resolve() == previous
    assert list((prefix / "releases").iterdir()) == [previous.parent.parent]
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kwargs: calls.append(command))
    installer.install(tmp_path, "1.2.0", prefix, bin_dir)
    assert (bin_dir / "tg").resolve() != previous
    assert previous.exists()
    assert calls[-2][-3:] == ["skill", "install", "--if-safe"]
    assert calls[-1][-3:] == ["completion", "install", "--if-safe"]


def test_existing_release_does_not_treat_network_errors_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "release not found"),
    )
    assert release.existing_release("v1.2.0") is None
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "network unavailable"),
    )
    with pytest.raises(RuntimeError):
        release.existing_release("v1.2.0")
