"""
run_imported_cases.py — 한 임포트 맵에서 여러 case 를 비교 실행.

YAML 스키마:
    source_map: maps/plant.json
    agv_count: 12
    duration_s: 600
    task_interval_s: 5.0
    random_seeds: [42, 43, 44]
    variants:
      - {label: "v0_baseline"}                                  # edit_file 없음 = 자동 추론 그대로
      - {label: "v1_chargers_8", edit_file: maps/v1.edit.json}
      - {label: "v2_bidir_south", edit_file: maps/v2.edit.json}

각 case × seed 별 KPI 결과를 outputs/imported_cases/<timestamp>/{ranking.csv,summary.json}
로 저장 + 간단한 HTML 비교 표 생성.

사용:
    PYTHONPATH=. python scripts/run_imported_cases.py experiments/plant_what_if.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# 프로젝트 루트를 sys.path 에 추가 — PYTHONPATH 설정 없이 동작 (Windows 친화)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml

from src.adapters.bus.adapters import LocalMemoryBus
from src.analytics.kpi import KPICalculator
from src.application.engine.simulation_engine import SimulationEngine
from src.application.scenario.task_generator import TaskGenerator
from src.domain.agv.agv import AGV
from src.domain.map.external_importer import apply_edits, build_map_graph, import_map_json
from src.domain.map.graph import MapGraph, NodeRole
from src.domain.reservation.scheduler import TimeWindowScheduler


@dataclass
class CaseResult:
    label: str
    seed: int
    n_agv: int
    duration_s: float
    task_interval_s: float
    edit_file: str = ""
    # KPI
    sim_time_s: float = 0.0
    completion_rate: float = 0.0
    task_acceptance_rate: float = 0.0
    demand_throughput_per_hour: float = 0.0
    total_wait_time_s: float = 0.0
    avg_wait_time_s: float = 0.0
    headon_total: int = 0
    retry_total: int = 0
    deadlock_count: int = 0
    error: str = ""


async def run_one_case(
    label: str,
    imported_graph: MapGraph,
    n_agv: int,
    duration_s: float,
    task_interval_s: float,
    seed: int,
    edit_file: str = "",
) -> CaseResult:
    result = CaseResult(
        label=label, seed=seed, n_agv=n_agv,
        duration_s=duration_s, task_interval_s=task_interval_s,
        edit_file=edit_file,
    )
    try:
        random.seed(seed)
        bus = LocalMemoryBus()
        scheduler = TimeWindowScheduler()
        task_gen = TaskGenerator(imported_graph, bus, task_interval_s=task_interval_s)
        engine = SimulationEngine(imported_graph, scheduler, task_generator=task_gen)

        chargers = [n.node_id for n in imported_graph.nodes.values() if n.is_charger]
        if not chargers:
            raise RuntimeError("no chargers in graph — Stamp 도구로 charger 마킹 필요")
        for i in range(n_agv):
            agv = AGV(f"AGV_{i+1:03d}", bus, imported_graph, scheduler)
            agv.current_node_id = chargers[i % len(chargers)]
            agv.physics.x = imported_graph.nodes[agv.current_node_id].x
            agv.physics.y = imported_graph.nodes[agv.current_node_id].y
            engine.register_agv(agv)

        kpis = await engine.run(duration_s=duration_s)
        result.sim_time_s = kpis.get("sim_time_s", duration_s)
        result.completion_rate = kpis.get("completion_rate", 0.0)
        result.task_acceptance_rate = kpis.get("task_acceptance_rate", 0.0)
        result.demand_throughput_per_hour = kpis.get("demand_throughput_per_hour", 0.0)
        result.total_wait_time_s = kpis.get("total_wait_time_s", 0.0)
        result.avg_wait_time_s = kpis.get("avg_wait_per_agv_s", kpis.get("avg_wait_time_s", 0.0))
        result.headon_total = int(kpis.get("headon_total", 0))
        result.retry_total = int(kpis.get("retry_total", 0))
        result.deadlock_count = int(kpis.get("deadlock_or_stall_count", 0))
    except Exception as exc:
        result.error = str(exc)
    return result


def build_html_report(results: list[CaseResult], out_path: Path, meta: dict) -> None:
    # case 별 seed 평균 집계
    by_label: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        if not r.error:
            by_label[r.label].append(r)

    aggregated = []
    for label, rs in by_label.items():
        n = len(rs)
        if n == 0:
            continue
        aggregated.append({
            "label": label,
            "n_runs": n,
            "completion_rate": sum(r.completion_rate for r in rs) / n,
            "throughput": sum(r.demand_throughput_per_hour for r in rs) / n,
            "avg_wait": sum(r.avg_wait_time_s for r in rs) / n,
            "headon": sum(r.headon_total for r in rs) / n,
            "retry": sum(r.retry_total for r in rs) / n,
            "deadlock": sum(r.deadlock_count for r in rs) / n,
            "edit_file": rs[0].edit_file or "(baseline)",
        })
    # 정렬: completion_rate desc → throughput desc → avg_wait asc → headon asc
    aggregated.sort(key=lambda r: (-r["completion_rate"], -r["throughput"], r["avg_wait"], r["headon"]))

    rows_html = "\n".join([f"""
      <tr>
        <td><strong>{i+1}</strong></td>
        <td>{r['label']}</td>
        <td class=mono>{r['edit_file']}</td>
        <td>{r['n_runs']}</td>
        <td class=num>{r['completion_rate']*100:.1f}%</td>
        <td class=num>{r['throughput']:.1f}</td>
        <td class=num>{r['avg_wait']:.2f}s</td>
        <td class=num>{r['headon']:.0f}</td>
        <td class=num>{r['retry']:.0f}</td>
        <td class=num>{r['deadlock']:.1f}</td>
      </tr>""" for i, r in enumerate(aggregated)])

    error_runs = [r for r in results if r.error]
    error_block = ""
    if error_runs:
        items = "".join(f"<li><strong>{r.label}</strong> (seed {r.seed}): {r.error}</li>"
                        for r in error_runs)
        error_block = f"<section class=err><h2>Errors</h2><ul>{items}</ul></section>"

    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Imported Map Case Comparison</title>
<style>
:root {{ --bg:#f4f6f9; --surf:#fff; --ink:#0f172a; --muted:#64748b; --border:#e3e8ef;
  --accent:#2563eb; --success:#0f9d58; --warn:#c77700; --danger:#c0392b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Pretendard",sans-serif; font-size:13px; }}
h1 {{ font-size:22px; letter-spacing:-0.015em; margin:0 0 4px; }}
.meta {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}
.meta span {{ margin-right:14px; }}
section {{ background:var(--surf); border:1px solid var(--border); border-radius:10px;
  padding:16px 18px; margin-bottom:14px; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
h2 {{ font-size:13px; font-weight:700; letter-spacing:0.05em; text-transform:uppercase;
  color:var(--muted); margin:0 0 12px; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid var(--border); text-align:left; }}
th {{ font-weight:600; color:var(--muted); font-size:11.5px; letter-spacing:0.03em; }}
tr:first-child td {{ background:#eef5ff; }}
tr:hover td {{ background:#f7faff; }}
td.num {{ font-family:ui-monospace,"JetBrains Mono",Menlo,monospace; text-align:right; }}
td.mono {{ font-family:ui-monospace,Menlo,monospace; font-size:11.5px; color:var(--muted); }}
.err {{ border-color:#fdb8b4; }}
.err h2 {{ color:var(--danger); }}
.err li {{ margin-bottom:6px; }}
</style></head><body>
<h1>Imported Map Case Comparison</h1>
<div class=meta>
  <span>📄 source: <strong>{meta.get("source_map", "")}</strong></span>
  <span>🤖 AGV: <strong>{meta.get("n_agv", "")}</strong></span>
  <span>⏱ duration: <strong>{meta.get("duration_s", "")}s</strong></span>
  <span>📦 task interval: <strong>{meta.get("task_interval_s", "")}s</strong></span>
  <span>🎲 seeds: <strong>{",".join(str(s) for s in meta.get("seeds", []))}</strong></span>
  <span>📅 {meta.get("timestamp", "")}</span>
</div>

<section>
  <h2>Ranking ({len(aggregated)} cases × {len(meta.get("seeds", []))} seeds)</h2>
  <table>
    <thead>
      <tr><th>#</th><th>Case</th><th>Edit</th><th>n</th><th>완료율</th><th>처리량 /h</th>
          <th>평균 대기</th><th>head-on</th><th>retry</th><th>deadlock</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</section>
{error_block}
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


async def main_async(yaml_path: Path) -> None:
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    source_map = cfg["source_map"]
    n_agv = int(cfg.get("agv_count", 12))
    duration_s = float(cfg.get("duration_s", 600))
    task_interval_s = float(cfg.get("task_interval_s", 5.0))
    seeds = [int(s) for s in cfg.get("random_seeds", [42])]
    variants = cfg["variants"]

    # 원본 맵 한 번만 임포트
    print(f"━━━ Importing {source_map} ━━━")
    base_imp = import_map_json(source_map)
    print(f"  nodes={len(base_imp.nodes)}, edges={len(base_imp.edges)}, "
          f"chargers={base_imp.report.inferred_chargers}, "
          f"stations={base_imp.report.inferred_stations}")

    results: list[CaseResult] = []
    total = len(variants) * len(seeds)
    idx = 0
    t_start = time.time()
    for v in variants:
        label = v["label"]
        edit_file = v.get("edit_file", "")
        imp = base_imp
        if edit_file:
            edits_path = Path(edit_file)
            if not edits_path.exists():
                print(f"  ✗ {label}: edit_file 없음 ({edits_path}) — skip")
                continue
            imp = apply_edits(base_imp, edits_path)
        graph = build_map_graph(imp)

        for seed in seeds:
            idx += 1
            elapsed = time.time() - t_start
            print(f"[{idx}/{total}] {label} seed={seed} (elapsed {elapsed:.1f}s)", flush=True)
            r = await run_one_case(label, graph, n_agv, duration_s, task_interval_s, seed, edit_file)
            results.append(r)
            if r.error:
                print(f"    ✗ {r.error}")
            else:
                print(f"    완료율 {r.completion_rate*100:.1f}% / 처리량 {r.demand_throughput_per_hour:.1f}/h / "
                      f"대기 {r.avg_wait_time_s:.2f}s / head-on {r.headon_total}")

    # 결과 저장
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = Path("outputs/imported_cases") / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "source_map": source_map,
        "n_agv": n_agv,
        "duration_s": duration_s,
        "task_interval_s": task_interval_s,
        "seeds": seeds,
        "timestamp": timestamp,
        "yaml": str(yaml_path),
        "results": [asdict(r) for r in results],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")

    # CSV
    import csv
    with (out_dir / "ranking.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["label","seed","n_agv","duration_s","completion_rate",
                    "throughput_per_h","avg_wait_s","headon","retry","deadlock","error"])
        for r in results:
            w.writerow([r.label, r.seed, r.n_agv, r.duration_s,
                        round(r.completion_rate, 4), round(r.demand_throughput_per_hour, 2),
                        round(r.avg_wait_time_s, 3), r.headon_total, r.retry_total,
                        r.deadlock_count, r.error])

    # HTML 비교 표
    build_html_report(results, out_dir / "report.html", summary)

    print()
    print(f"━━━ 완료 — {out_dir} ━━━")
    print(f"  open {out_dir}/report.html")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("yaml_path", help="imported case YAML")
    parser.add_argument("--open", action="store_true", help="auto-open report")
    args = parser.parse_args()

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        print(f"✗ not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main_async(yaml_path))

    if args.open:
        import glob, subprocess
        latest = sorted(glob.glob("outputs/imported_cases/*/report.html"))[-1]
        subprocess.run(["open", latest], check=False)


if __name__ == "__main__":
    main()
