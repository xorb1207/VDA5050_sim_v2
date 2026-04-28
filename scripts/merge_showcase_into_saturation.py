"""Copy showcase variant directories into a saturation run dir, then regenerate
report.html so its 시뮬레이션 재생 buttons resolve to the showcase playbacks.

Usage:
    python scripts/merge_showcase_into_saturation.py SAT_DIR SHOWCASE_DIR
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Local import path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.application.usecases.experiment_runner import (  # noqa: E402
    _build_per_topology,
    _build_report_html,
)


def main(sat_dir: Path, showcase_dir: Path) -> None:
    if not sat_dir.is_dir() or not showcase_dir.is_dir():
        sys.exit(f"Both arguments must be existing dirs. sat={sat_dir} showcase={showcase_dir}")
    report_path = sat_dir / "report.json"
    if not report_path.exists():
        sys.exit(f"Saturation report.json missing at {report_path}")

    # Copy showcase playback files into saturation. Saturation usually already has
    # a same-named variant dir (same seed), so we only top up the playback files.
    copied: list[str] = []
    for child in sorted(showcase_dir.iterdir()):
        if not child.is_dir():
            continue
        src_html = child / "playback.html"
        src_trace = child / "playback_trace.json"
        if not src_html.exists():
            continue
        target = sat_dir / child.name
        target.mkdir(exist_ok=True)
        shutil.copy2(src_html, target / "playback.html")
        if src_trace.exists():
            shutil.copy2(src_trace, target / "playback_trace.json")
        copied.append(child.name)

    print(f"Copied {len(copied)} showcase variants into {sat_dir.name}:")
    for name in copied:
        print(f"  + {name}")

    # Regenerate report.html with playback links pointing at the freshly merged dirs.
    report = json.loads(report_path.read_text())
    aggregate_path = sat_dir / "ranking_aggregate.json"
    if aggregate_path.exists():
        aggregate = json.loads(aggregate_path.read_text())
    else:
        # Fallback: derive aggregate from per_topology entries already in the report.
        aggregate = []
        for entry in report.get("per_topology", []):
            aggregate.append({
                "topology_type": entry["topology_type"],
                "topology_variant": entry["topology_variant"],
                "siding_placement": entry.get("siding_placement", "base"),
                "siding_policy": entry.get("siding_policy", "reachable"),
                **entry.get("aggregate_metrics", {}),
                "first_place_wins": entry.get("aggregate_metrics", {}).get("wins", 0),
                "evaluated_groups": entry.get("aggregate_metrics", {}).get("evaluated_groups", 0),
                "avg_rank": entry.get("aggregate_metrics", {}).get("avg_rank", 0),
            })

    summary_path = sat_dir / "summary.json"
    rows = json.loads(summary_path.read_text()) if summary_path.exists() else []
    locale = report.get("overview", {}).get("locale") or "ko"

    report["per_topology"] = _build_per_topology(rows, aggregate, locale, sat_dir)

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (sat_dir / "report.html").write_text(_build_report_html(report), encoding="utf-8")
    print(f"Rebuilt report.html at {sat_dir / 'report.html'}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: merge_showcase_into_saturation.py SAT_DIR SHOWCASE_DIR")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
