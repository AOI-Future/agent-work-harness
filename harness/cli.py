"""JSON CLI for trusted controllers; no model/provider or external writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import TaskSpec
from .runtime import Harness
from .workspace import HarnessError


def read_json(path: str) -> Any:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1048576:
        raise HarnessError("JSON input must be a bounded regular non-symlink file")
    return json.loads(source.read_text())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--runtime", type=Path, default=Path.cwd() / ".runtime")
    commands = root.add_subparsers(dest="operation", required=True)
    submit = commands.add_parser("submit", help="register an immutable local task")
    submit.add_argument("spec")
    for operation in ("inspect", "verify", "recover", "evidence"):
        commands.add_parser(operation).add_argument("task")
    claim = commands.add_parser("claim", help="controller assigns a dedicated workspace")
    claim.add_argument("task")
    claim.add_argument("--role", required=True, choices=["worker", "reviewer"])
    claim.add_argument("--actor", required=True)
    for operation in ("report", "review"):
        report = commands.add_parser(operation)
        report.add_argument("task")
        report.add_argument("report")
    for operation in ("events", "notifications"):
        events = commands.add_parser(operation)
        events.add_argument("task")
        events.add_argument("--after", type=int, default=0)
    intervention = commands.add_parser("intervention")
    intervention.add_argument("task")
    intervention.add_argument("--seconds", type=float, required=True)
    intervention.add_argument("--cost", type=float)
    commands.add_parser("list")
    return root


def main() -> int:
    args = parser().parse_args()
    harness: Harness | None = None
    try:
        harness = Harness(args.runtime.absolute())
        operation = args.operation
        if operation == "submit":
            result: Any = harness.submit(TaskSpec.model_validate(read_json(args.spec)))
        elif operation == "inspect":
            result = harness.inspect(args.task)
        elif operation == "list":
            result = [harness.inspect(task) for task in harness.store.ids()]
        elif operation in {"events", "notifications"}:
            result = harness.store.events(
                args.task, notifications=operation == "notifications", after=args.after
            )
        elif operation == "evidence":
            view = harness.inspect(args.task)
            if view["state"] != "completed":
                raise HarnessError("evidence is available only for a current completed revision")
            result = harness.store.get(args.task)["evidence"]
        else:
            command: dict[str, Any] = {"action": operation}
            if operation == "claim":
                command.update(role=args.role, actor=args.actor)
            elif operation in {"report", "review"}:
                command["report"] = read_json(args.report)
            elif operation == "intervention":
                command.update(seconds=args.seconds, cost=args.cost)
            result = harness.advance(args.task, command)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        print(json.dumps({"error": "invalid_input", "next": "check the strict input schema"}))
        return 2
    except HarnessError as exc:
        print(json.dumps({"error": "operation_rejected", "reason": str(exc)}))
        return 2
    except (OSError, ValueError):
        print(
            json.dumps({"error": "local_io_failure", "next": "inspect local paths and permissions"})
        )
        return 2
    finally:
        if harness is not None:
            harness.close()


if __name__ == "__main__":
    sys.exit(main())
