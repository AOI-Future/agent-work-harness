"""Read-only evaluation report over a harness runtime ledger.

Aggregates process metrics per task from the tasks table and the append-only
events journal, grouped by spec complexity (the evaluation tier). The database
is opened strictly read-only; this script writes nothing, ever.

Usage:
    python3 scripts/eval_report.py --runtime /absolute/path/.runtime [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def collect(runtime: Path) -> list[dict[str, Any]]:
    database = runtime / "state.sqlite"
    if not database.is_file():
        raise SystemExit(f"no ledger at {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    metrics: list[dict[str, Any]] = []
    try:
        for row in connection.execute("SELECT data FROM tasks ORDER BY id"):
            task = json.loads(row["data"])
            spec = task["spec"]
            task_id = spec["id"]
            kinds: dict[str, int] = {}
            preflight: Any = None
            last_event_at = 0.0
            for event in connection.execute(
                "SELECT kind, data, at FROM events WHERE task_id=? ORDER BY seq", (task_id,)
            ):
                kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
                last_event_at = max(last_event_at, float(event["at"]))
                if preflight is None:
                    payload = json.loads(event["data"])
                    if "preflight" in payload:
                        preflight = payload["preflight"]
            implementation_reports = kinds.get("implementation_reported", 0)
            created_at = float(task["created_at"])
            metrics.append(
                {
                    "task_id": task_id,
                    "tier": spec.get("complexity", "unspecified"),
                    "state": task["state"],
                    "attempts": task["attempts"],
                    "review_attempts": task["review_attempts"],
                    "fix_rounds": max(implementation_reports - 1, 0),
                    "escalated": kinds.get("escalated", 0) > 0,
                    "closed_by_human": kinds.get("closed", 0) > 0,
                    "evidence_passed": bool(task.get("evidence")),
                    "human_interventions": task["human_count"],
                    "human_seconds": round(task["human_seconds"], 3),
                    "reported_cost": task["reported_cost"],
                    "elapsed_seconds": round(
                        (task.get("completed_at") or last_event_at or created_at) - created_at,
                        3,
                    ),
                    "preflight": preflight,
                    "integrated_at": task.get("integrated_at"),
                }
            )
    finally:
        connection.close()
    return metrics


def _mean(entries: list[dict[str, Any]], key: str) -> float:
    return round(sum(float(entry[key]) for entry in entries) / len(entries), 2)


def aggregate(metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tiers: dict[str, list[dict[str, Any]]] = {}
    for entry in metrics:
        tiers.setdefault(entry["tier"], []).append(entry)
    summary: dict[str, dict[str, Any]] = {}
    for tier, entries in tiers.items():
        summary[tier] = {
            "tasks": len(entries),
            "mean_attempts": _mean(entries, "attempts"),
            "mean_review_attempts": _mean(entries, "review_attempts"),
            "mean_fix_rounds": _mean(entries, "fix_rounds"),
            "tasks_needing_fixes": sum(1 for entry in entries if entry["fix_rounds"] > 0),
            "escalations": sum(1 for entry in entries if entry["escalated"]),
            "completed_verified": sum(1 for entry in entries if entry["evidence_passed"]),
            "integrated": sum(1 for entry in entries if entry["integrated_at"]),
            "total_human_seconds": round(sum(float(e["human_seconds"]) for e in entries), 2),
            "mean_elapsed_seconds": _mean(entries, "elapsed_seconds"),
            "reported_costs": sum(
                float(entry["reported_cost"]) for entry in entries if entry["reported_cost"]
            ),
        }
    return summary


def to_markdown(metrics: list[dict[str, Any]]) -> str:
    lines = [
        "| task | tier | state | att | rev | fix | esc | evid | human_s | elapsed_s | cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in metrics:
        lines.append(
            f"| {entry['task_id']} | {entry['tier']} | {entry['state']} "
            f"| {entry['attempts']} | {entry['review_attempts']} | {entry['fix_rounds']} "
            f"| {'yes' if entry['escalated'] else 'no'} "
            f"| {'pass' if entry['evidence_passed'] else '—'} "
            f"| {entry['human_seconds']} | {entry['elapsed_seconds']} "
            f"| {entry['reported_cost'] if entry['reported_cost'] is not None else '—'} |"
        )
    lines.append("")
    lines.append("## By tier")
    lines.append("")
    lines.append("| tier | tasks | attμ | revμ | fixμ | fixed | esc | verified | integ | human_s |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for tier, stats in aggregate(metrics).items():
        lines.append(
            f"| {tier} | {stats['tasks']} | {stats['mean_attempts']} "
            f"| {stats['mean_review_attempts']} | {stats['mean_fix_rounds']} "
            f"| {stats['tasks_needing_fixes']} | {stats['escalations']} "
            f"| {stats['completed_verified']} | {stats['integrated']} "
            f"| {stats['total_human_seconds']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = parser.parse_args(argv)
    metrics = collect(args.runtime.absolute())
    if args.json:
        print(json.dumps({"tasks": metrics, "tiers": aggregate(metrics)}, ensure_ascii=False))
    else:
        print(to_markdown(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
