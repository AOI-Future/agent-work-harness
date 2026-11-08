"""Bounded, fixed-argv execution of controller-approved local acceptance commands."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import Check
from .workspace import environment


def run_check(check: Check, workspace: Path, capture_tail: int = 0) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"id": check.id, "passed": False, "reason": "execution_failed"}
    tail = bytearray()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="harness-clean-cache-") as cache,
            subprocess.Popen(
                check.argv,
                cwd=workspace,
                env=environment() | {"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            ) as child,
        ):
            assert child.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            size = 0
            reason = ""
            try:
                while selector.get_map():
                    elapsed = time.monotonic() - started
                    if elapsed >= check.timeout_seconds:
                        reason = "timeout"
                        break
                    for key, _ in selector.select(min(0.05, check.timeout_seconds - elapsed)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                        size += len(data)
                        if capture_tail:
                            tail += data
                            if len(tail) > capture_tail:
                                del tail[: len(tail) - capture_tail]
                        if size > 1024 * 1024:
                            reason = "output_limit"
                            break
                    if reason:
                        break
                # A process can close stdout and still run forever.
                if not reason:
                    try:
                        child.wait(
                            timeout=max(0.01, check.timeout_seconds - (time.monotonic() - started))
                        )
                    except subprocess.TimeoutExpired:
                        reason = "timeout"
                # Also stop descendants after an apparently successful parent exits.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                result.update(
                    passed=not reason and child.returncode == 0,
                    reason=reason or ("passed" if child.returncode == 0 else "exit_nonzero"),
                    returncode=child.returncode,
                )
            finally:
                selector.close()
    except OSError:
        result["reason"] = "command_unavailable"
    result["seconds"] = round(time.monotonic() - started, 4)
    if capture_tail:
        result["tail"] = tail.decode("utf-8", "replace")
    return result
