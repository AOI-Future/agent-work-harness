"""Machine profile detection for controller-side check scheduling.

Reports-only: nothing here widens authority. On Apple Silicon the
performance-core count caps how many independent checks the controller may
run concurrently; other platforms fall back to a conservative core count.
"""

from __future__ import annotations

import os
import platform
import subprocess
from functools import lru_cache
from typing import Any


def _sysctl_int(name: str) -> int | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    text = result.stdout.decode().strip()
    if result.returncode != 0 or not text.isdigit():
        return None
    return int(text)


@lru_cache(maxsize=1)
def profile() -> dict[str, Any]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    info: dict[str, Any] = {
        "platform": system,
        "arch": machine,
        "apple_silicon": False,
        "model": None,
        "perf_cores": None,
        "eff_cores": None,
    }
    if system == "darwin" and machine == "arm64":
        info["apple_silicon"] = True
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.model"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                info["model"] = result.stdout.decode().strip() or None
        except OSError:
            pass
        info["perf_cores"] = _sysctl_int("hw.perflevel0.physicalcpu")
        info["eff_cores"] = _sysctl_int("hw.perflevel1.physicalcpu")
    total = os.cpu_count() or 1
    if info["perf_cores"]:
        info["check_concurrency"] = max(1, min(8, info["perf_cores"]))
    else:
        info["check_concurrency"] = max(1, min(8, total))
    return info
