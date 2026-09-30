"""Dedicated local Git copies and content-bound revisions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class HarnessError(Exception):
    """A safe, controller-facing failure with no raw subprocess output."""


def environment() -> dict[str, str]:
    # Avoid inherited credentials, provider configuration and launchd environment.
    return {"PATH": f"{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/bin:/bin", "LC_ALL": "C"}


def git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), *args],
            env=environment(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError("git operation unavailable or timed out") from exc
    if result.returncode:
        raise HarnessError("git operation failed; inspect the repository locally")
    if len(result.stdout) > 8 * 1024 * 1024:
        raise HarnessError("repository metadata exceeds pilot limit")
    return result.stdout


def safe_repo(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        raise HarnessError("source_repo must be an existing absolute non-symlink directory")
    resolved = candidate.resolve()
    top = git(resolved, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != resolved:
        raise HarnessError("source_repo must be the repository root")
    return resolved


def within(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _temporary(path: str) -> bool:
    parts = Path(path).parts
    return (
        "__pycache__" in parts
        or ".pytest_cache" in parts
        or ".ruff_cache" in parts
        or ".mypy_cache" in parts
        or path.endswith(".pyc")
    )


def snapshot(repo: Path) -> dict[str, str]:
    paths: set[str] = set()
    tracked = {item.decode() for item in git(repo, "ls-files", "-z").split(b"\0") if item}
    for root, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git" and not _temporary(name)]
        for name in dirs + files:
            file = Path(root) / name
            relative = file.relative_to(repo).as_posix()
            if file.is_symlink():
                raise HarnessError("symlinks are outside the local pilot")
            if file.is_file() and (relative in tracked or not _temporary(relative)):
                paths.add(relative)
    result: dict[str, str] = {}
    for name in sorted(paths):
        file = repo / name
        if file.stat().st_size > 8 * 1024 * 1024:
            raise HarnessError("file exceeds local pilot size limit")
        result[name] = (
            hashlib.sha256(file.read_bytes()).hexdigest() + ":" + oct(file.stat().st_mode & 0o777)
        )
    return result


def tree_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def revision(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").decode().strip() + ":" + tree_digest(snapshot(repo))


def copy_workspace(source: Path, base: str, destination: Path, previous: Path | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(
        source,
        "clone",
        "--local",
        "--no-hardlinks",
        "--no-checkout",
        "--",
        str(source),
        str(destination),
    )
    git(destination, "checkout", "--detach", base)
    if previous is not None:
        files = snapshot(previous)
        current = snapshot(destination)
        for name in current.keys() - files.keys():
            (destination / name).unlink()
        # Remove empty directories before a directory -> file replacement.
        for root, dirs, _ in os.walk(destination, topdown=False):
            for name in dirs:
                directory = Path(root) / name
                if ".git" not in directory.relative_to(destination).parts:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
        for name in files:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(previous / name, target)
            shutil.copymode(previous / name, target)


def changes(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(
        name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name)
    )
