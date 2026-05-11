"""
import_map_demo.py — 외부 맵 JSON 임포트 PoC CLI.

사용:
    PYTHONPATH=. python scripts/import_map_demo.py maps/mock_plant_a.json
    PYTHONPATH=. python scripts/import_map_demo.py maps/mock_plant_a.json --open

흐름:
  1. JSON 임포트 → ImportedMap (자동 추론 결과 포함)
  2. 검증 리포트 콘솔 출력
  3. ImportedMap → MapGraph 변환
  4. playback HTML 로 시각화 (snapshot 없는 정적 맵 뷰)
  5. --open 옵션이면 브라우저에서 열기

PoC 단계에선 편집 UI 가 없으므로 자동 추론된 결과를 "있는 그대로" 보여줌.
검토/수정은 다음 단계 (편집 UI) 에서.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.analytics.playback_trace import PlaybackTraceRecorder, build_playback_html
from src.domain.map.external_importer import build_map_graph, import_map_json
from src.interfaces.map_editor import build_editor_html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", help="external map JSON path")
    parser.add_argument("--out", default=None, help="output HTML path (default: <json>.preview.html)")
    parser.add_argument("--open", action="store_true", help="auto-open in browser")
    parser.add_argument("--edit", action="store_true",
                        help="open in Map Editor (편집 UI) instead of read-only preview")
    args = parser.parse_args()

    json_path = Path(args.json_path)
    if not json_path.exists():
        print(f"✗ not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    # 1단계: import + infer
    print(f"━━━ Importing {json_path} ━━━")
    imp = import_map_json(json_path)
    r = imp.report
    print(f"Nodes: {r.node_count}")
    print(f"Edges (raw → merged): {r.edge_count_raw} → {r.edge_count_after_merge}")
    print(f"  bidirectional: {r.bidirectional_count}")
    print(f"Connected components: {r.connected_components}")
    print(f"Isolated nodes: {len(r.isolated_nodes)} | Dead-end nodes: {len(r.dead_end_nodes)}")
    print(f"Corridor distribution: {r.corridor_stats}")
    print(f"Inferred — chargers: {r.inferred_chargers}, stations: {r.inferred_stations}, holding: {r.inferred_holding}")
    print()
    if r.warnings:
        print("━━━ Warnings ━━━")
        for w in r.warnings:
            sample = (" e.g. " + ", ".join(w.nodes[:3])) if w.nodes else ""
            print(f"  [{w.severity:>5}] {w.code}: {w.message}{sample}")
        print()

    # 2단계: 시각화 (편집 모드 vs 정적 미리보기)
    if args.edit:
        # Map Editor — 인터랙티브 편집 페이지
        html = build_editor_html(imp, title=f"Map Editor — {json_path.stem}")
        out_path = Path(args.out) if args.out else json_path.with_suffix(".editor.html")
        out_path.write_text(html, encoding="utf-8")
        print(f"✓ Map Editor written: {out_path}")
    else:
        # 정적 미리보기 (playback HTML)
        graph = build_map_graph(imp)
        recorder = PlaybackTraceRecorder(graph, sample_interval_s=0.5)
        trace = {
            "meta": {
                "duration_s": 0.0,
                "sample_interval_s": 0.5,
                "topology_type": "imported",
                "topology_variant": json_path.stem,
                "description": {
                    "headline": f"외부 맵 임포트: {json_path.name} ({r.node_count} nodes / {r.edge_count_after_merge} edges)",
                },
            },
            "map": recorder._serialize_map(),
            "snapshots": [],
            "events": [],
        }
        out_path = Path(args.out) if args.out else json_path.with_suffix(".preview.html")
        html = build_playback_html(trace)
        out_path.write_text(html, encoding="utf-8")
        print(f"✓ Preview written: {out_path}")

    if args.open:
        try:
            subprocess.run(["open", str(out_path)], check=False)
        except FileNotFoundError:
            try:
                subprocess.run(["xdg-open", str(out_path)], check=False)
            except FileNotFoundError:
                print(f"  (open command not found; visit manually: file://{out_path.resolve()})")


if __name__ == "__main__":
    main()
