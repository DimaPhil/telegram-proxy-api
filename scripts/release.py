"""Build and publish one API/CLI release; invoked only by the release workflow."""

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
import tomllib
from pathlib import Path

VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION.fullmatch(value):
        raise ValueError("Version must be X.Y.Z without a prefix or prerelease suffix")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def next_version(tags: list[str], requested: str | None, baseline: str) -> str:
    versions = [version_tuple(tag[1:]) for tag in tags if tag.startswith("v") and VERSION.fullmatch(tag[1:])]
    latest = max(versions, default=version_tuple(baseline))
    if requested:
        if version_tuple(requested) <= latest:
            raise ValueError("Manual version must be greater than every existing release tag")
        return requested
    return f"{latest[0]}.{latest[1] + 1}.0"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def existing_release(tag: str) -> dict[str, object] | None:
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "isDraft"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        payload: dict[str, object] = json.loads(result.stdout)
        return payload
    if "release not found" in result.stderr.lower():
        return None
    raise RuntimeError("Cannot inspect existing release; refusing to publish")


def set_version(path: Path, version: str) -> None:
    source = path.read_text()
    current = tomllib.loads(source)["project"]["version"]
    # Both projects deliberately use one literal project version. Fail on ambiguity.
    updated, count = re.subn(
        r'^version\s*=\s*"' + re.escape(current) + r'"\s*$',
        f'version = "{version}"',
        source,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError(f"Expected one version declaration in {path}")
    path.write_text(updated)


def checksums(directory: Path) -> None:
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    (directory / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files)
    )


def smoke_cli(directory: Path, version: str) -> None:
    """Exercise the actual release installer without touching the runner's user files."""
    with tempfile.TemporaryDirectory(prefix="tg-release-smoke-") as temporary:
        sandbox = Path(temporary).resolve()
        environment = dict(os.environ, HOME=str(sandbox))
        subprocess.run(
            [sys.executable, str(directory / "install-tg.py"), "--assets", str(directory), "--version", version],
            env=environment,
            check=True,
        )
        executable = sandbox / ".local/bin/tg"
        actual_version = subprocess.check_output([str(executable), "--version"], text=True).strip()
        if actual_version != f"tg {version}":
            raise ValueError("Installed CLI version does not match the release")
        subprocess.run([str(executable), "operations", "list"], env=environment, check=True, stdout=subprocess.DEVNULL)
        for name in (
            ".agents/skills/tg/SKILL.md",
            ".local/share/bash-completion/completions/tg",
            ".local/share/zsh/site-functions/_tg",
            ".config/fish/completions/tg.fish",
        ):
            if not (sandbox / name).is_file():
                raise ValueError(f"Release installer did not install {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--publish", action="store_true", help="Create or finish the GitHub release")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sha = run("git", "rev-parse", "HEAD")
    tags = run("git", "tag", "--list").splitlines()
    # A successful rerun must not increment the version again for the same commit.
    matching = run("git", "tag", "--points-at", sha).splitlines()
    matching = [tag for tag in matching if tag.startswith("v") and VERSION.fullmatch(tag[1:])]
    if matching and not args.version:
        tag = max(matching, key=lambda value: version_tuple(value[1:]))
        release = existing_release(tag)
        if release is not None and not release["isDraft"]:
            print(f"{tag} already published for this commit; nothing to do")
            return
        version = tag[1:]
    else:
        baseline = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        # A failed manual run may have created its tag and draft already.
        if args.version and f"v{args.version}" in matching:
            release = existing_release(f"v{args.version}")
            if release is not None and not release["isDraft"]:
                print(f"v{args.version} already published for this commit; nothing to do")
                return
            version = args.version
        else:
            version = next_version(tags, args.version, baseline)
    tag = f"v{version}"
    if args.publish:
        stable_tags = [value for value in tags if value.startswith("v") and VERSION.fullmatch(value[1:])]
        if stable_tags:
            latest_tag = max(stable_tags, key=lambda value: version_tuple(value[1:]))
            if version_tuple(version) < version_tuple(latest_tag[1:]):
                raise ValueError("A higher version already exists; refusing to publish an older draft")
            ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", latest_tag, sha], check=False)
            if ancestor.returncode == 1:
                raise ValueError("A newer main commit has already been released; refusing a stale release")
            if ancestor.returncode != 0:
                raise RuntimeError("Cannot verify release ancestry")
    output = root / "dist-release"
    output.mkdir(exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("dist-release must be empty; refusing to mix release artifacts")
    for project in (root, root / "cli"):
        set_version(project / "pyproject.toml", version)
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(output), str(project)],
            check=True,
        )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 2 or len(list(output.glob("*.tar.gz"))) != 2:
        raise ValueError("Expected exactly one wheel and sdist per project")
    shutil.copyfile(root / "scripts" / "install-tg.py", output / "install-tg.py")
    (output / "release.json").write_text(json.dumps({"version": version, "commit": sha}, indent=2) + "\n")
    checksums(output)
    smoke_cli(output, version)
    if not args.publish:
        print(f"Built {tag} in {output}")
        return
    if tag not in tags:
        # Reserve the tag before creating a draft so retries recover even if publication fails.
        repository = os.environ["GITHUB_REPOSITORY"]
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/git/refs",
                "--method",
                "POST",
                "-f",
                f"ref=refs/tags/{tag}",
                "-f",
                f"sha={sha}",
                "--silent",
            ],
            check=True,
        )
    if existing_release(tag) is None:
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--verify-tag",
                "--draft",
                "--title",
                tag,
                "--generate-notes",
            ],
            check=True,
        )
    subprocess.run(
        [
            "gh",
            "release",
            "upload",
            tag,
            *[str(path) for path in sorted(output.iterdir())],
            "--clobber",
        ],
        check=True,
    )
    subprocess.run(["gh", "release", "edit", tag, "--draft=false", "--latest"], check=True)


if __name__ == "__main__":
    main()
