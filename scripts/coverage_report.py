#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Report test coverage of the Python package and the C sources as one table.

Reads coverage.py's JSON report and gcovr's JSON summary (``make
cov-report`` writes both), prints a Markdown report, and with ``--check``
fails when a figure has fallen below the committed baseline by more than
its tolerance. The tolerance absorbs the branches that timing decides
from one run to the next.

    --update-baseline    write the figures measured now as the baseline

Raise the baseline in the change that raises coverage, with the figures
CI measured: they differ a little between platforms.
"""

import argparse
import json
import sys
from pathlib import Path

MARKER = "<!-- coverage-report -->"

# What is held against the baseline, with the label it is reported under.
FIGURES = {
    "python": "Python package, lines and branches",
    "c_lines": "C sources, lines",
    "c_branches": "C sources, branches taken",
}


def percent(covered: int, total: int) -> float:
    return round(100.0 * covered / total, 2) if total else 100.0


def measure(python_report: dict, c_report: dict) -> tuple[dict, list[tuple[str, str]]]:
    """The figures, and a (file, coverage) row per source file."""
    figures = {
        "python": round(python_report["totals"]["percent_covered"], 2),
        "c_lines": percent(c_report["line_covered"], c_report["line_total"]),
        "c_branches": percent(c_report["branch_covered"], c_report["branch_total"]),
    }
    rows = [
        (name, f"{entry['summary']['percent_covered']:.1f}%")
        for name, entry in sorted(python_report["files"].items())
        if entry["summary"]["num_statements"]
    ]
    for entry in sorted(c_report["files"], key=lambda entry: entry["filename"]):
        lines = percent(entry["line_covered"], entry["line_total"])
        branches = percent(entry["branch_covered"], entry["branch_total"])
        rows.append((entry["filename"], f"{lines:.1f}% lines, {branches:.1f}% branches"))
    return figures, rows


def shortfalls(figures: dict, baseline: dict) -> list[str]:
    """One line per figure that fell below its baseline by more than its tolerance."""
    lines = []
    for key, label in FIGURES.items():
        held = baseline.get(key)
        if held is None:
            continue
        allowed = held["percent"] - held["tolerance"]
        if figures[key] < allowed:
            lines.append(
                f"{label}: {figures[key]:.2f}% is below the baseline {held['percent']:.2f}% "
                f"by more than {held['tolerance']:.2f}"
            )
    return lines


def render(figures: dict, rows: list[tuple[str, str]], baseline: dict, failed: list[str]) -> str:
    out = [MARKER, "## Test coverage (unit suite)", ""]
    out += ["| | Now | Baseline | Change |", "|---|---|---|---|"]
    for key, label in FIGURES.items():
        held = baseline.get(key)
        if held is None:
            out.append(f"| {label} | {figures[key]:.2f}% | none | |")
            continue
        change = figures[key] - held["percent"]
        out.append(f"| {label} | {figures[key]:.2f}% | {held['percent']:.2f}% | {change:+.2f} |")
    out.append("")
    if failed:
        out += ["**Coverage dropped:**", "", *(f"- {line}" for line in failed), ""]
    out += ["<details><summary>By file</summary>", "", "| File | Covered |", "|---|---|"]
    out += [f"| `{name}` | {covered} |" for name, covered in rows]
    out += ["", "</details>", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", required=True, type=Path, help="coverage.py's JSON report")
    parser.add_argument("--c", required=True, type=Path, help="gcovr's JSON summary")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="also write the report here")
    parser.add_argument("--check", action="store_true", help="fail when a figure dropped")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    figures, rows = measure(json.loads(args.python.read_text()), json.loads(args.c.read_text()))
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
    if args.update_baseline:
        for key in FIGURES:
            tolerance = baseline.get(key, {}).get("tolerance", 0.5)
            baseline[key] = {"percent": figures[key], "tolerance": tolerance}
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n")
    failed = shortfalls(figures, baseline)
    report = render(figures, rows, baseline, failed)
    print(report)
    if args.output:
        args.output.write_text(report)
    return 1 if args.check and failed else 0


if __name__ == "__main__":
    sys.exit(main())
