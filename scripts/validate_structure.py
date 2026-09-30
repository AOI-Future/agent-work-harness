"""Mechanical knowledge, dependency and vendored protocol checks."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate() -> None:
    instructions = (ROOT / "AGENTS.md").read_text()
    assert len(instructions.splitlines()) <= 100, "entry point exceeds 100 lines"
    for path in [ROOT / "AGENTS.md", ROOT / "README.md", *ROOT.glob("docs/*.md")]:
        for reference in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
            if "://" not in reference:
                assert (path.parent / reference).is_file(), (
                    f"broken documentation link: {path.name}"
                )
    # Inner modules cannot depend on the controller or CLI.
    forbidden = {
        "models.py": {"runtime", "store", "workspace", "checks", "cli", "protocol"},
        "workspace.py": {"runtime", "store", "cli", "protocol"},
        "store.py": {"runtime", "cli", "protocol", "checks"},
        "checks.py": {"runtime", "store", "cli", "protocol"},
        "protocol.py": {"runtime", "store", "cli"},
        "runtime.py": {"cli"},
    }
    for file, targets in forbidden.items():
        tree = ast.parse((ROOT / "harness" / file).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                assert node.module.split(".")[0] not in targets, f"dependency direction: {file}"
    vendor = ROOT / "harness/vendor"
    manifest = json.loads((vendor / "manifest.json").read_text())
    for file, expected in manifest["files"].items():
        assert hashlib.sha256((vendor / file).read_bytes()).hexdigest() == expected, (
            "vendored protocol changed without a reviewed pin update"
        )
    print("entry point, documentation links, dependency directions and vendor pin: PASS")


if __name__ == "__main__":
    validate()
