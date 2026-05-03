"""
실험 결과 인덱스 빌더.

outputs/experiments/*/report.json 을 스캔해서 outputs/experiments/index.html 을 생성한다.
각 run을 카드로 표시하고, 카드 hover 시 토폴로지별 KPI 요약을 펼친다.
다중 AGV count run은 1위 토폴로지의 completion_rate 스파크라인을 그린다.

호출 방식:
  - experiment_runner 종료 시 자동 (rebuild_index 호출)
  - 수동:  python -m src.tools.build_index
  -        python -m src.tools.build_index outputs/experiments
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ── 카드 데이터 모델 ────────────────────────────────────────────────
@dataclass
class RunCard:
    run_id: str
    timestamp: Optional[datetime]
    yaml_name: str                         # "fab_topology" 등 (없으면 run_id fallback)
    types: list[str]
    agv_counts: list[int]
    duration_s: float
    seeds: list[int]
    demand_mode: str
    top_winner: Optional[str]
    headline: str                          # overview.summary[0] (한 줄)
    winner_completion: Optional[float]
    winner_throughput: Optional[float]
    detail_rows: list[dict[str, Any]]      # per_topology aggregate (hover 패널)
    variants: list[dict[str, str]]         # {name, playback_link} 리스트 (드롭다운)
    sparkline_points: list[tuple[float, float]]  # [(agv, completion_mean), ...]
    has_report: bool
    has_ranking_csv: bool


# ── run_id → datetime 파싱 ─────────────────────────────────────────
def _parse_run_timestamp(run_id: str) -> Optional[datetime]:
    """20260501T230016 형태 → datetime. 실패 시 None."""
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(run_id, fmt)
        except ValueError:
            continue
    return None


# ── report.json → RunCard 변환 ─────────────────────────────────────
def _load_card(run_dir: Path) -> Optional[RunCard]:
    report_path = run_dir / "report.json"
    if not report_path.exists():
        return None
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    overview = report.get("overview", {}) or {}
    params = overview.get("input_parameters", {}) or {}
    per_topology = report.get("per_topology", []) or []
    chart_series = report.get("chart_series", {}) or {}

    run_id = run_dir.name
    yaml_name = overview.get("experiment_yaml") or run_id

    summary_lines = overview.get("summary") or []
    headline = summary_lines[0] if summary_lines else "(요약 없음)"

    # 1위 카드 메트릭
    top_winner = overview.get("top_winner")
    winner_completion: Optional[float] = None
    winner_throughput: Optional[float] = None
    for pt in per_topology:
        if pt.get("topology_variant") == top_winner:
            agg = pt.get("aggregate_metrics", {}) or {}
            winner_completion = agg.get("avg_completion_rate")
            winner_throughput = agg.get("avg_demand_throughput_per_hour")
            break

    # detail rows: per_topology 각각의 핵심 KPI
    detail_rows: list[dict[str, Any]] = []
    for pt in per_topology:
        agg = pt.get("aggregate_metrics", {}) or {}
        detail_rows.append({
            "variant": pt.get("topology_variant", "?"),
            "completion": agg.get("avg_completion_rate"),
            "throughput": agg.get("avg_demand_throughput_per_hour"),
            "wait": agg.get("avg_total_wait_time_s"),
            "headon": agg.get("avg_headon_total"),
            "followon": agg.get("avg_followon_total"),
            "section": agg.get("avg_section_conflict_total"),
            "rank": agg.get("avg_rank"),
        })

    # variants for playback dropdown
    variants: list[dict[str, str]] = []
    for pt in per_topology:
        link = pt.get("playback_link")
        name = pt.get("topology_variant", "?")
        if link:
            variants.append({"name": name, "link": str(link)})

    # sparkline: 1위 토폴로지의 completion_rate by AGV count
    sparkline_points: list[tuple[float, float]] = []
    completion_by_agv = chart_series.get("completion_by_agv", []) or []
    if top_winner:
        for series in completion_by_agv:
            if series.get("topology_variant") == top_winner:
                pts = series.get("points", []) or []
                sparkline_points = [
                    (float(p["agv_count"]), float(p["mean"]))
                    for p in pts
                    if p.get("agv_count") is not None and p.get("mean") is not None
                ]
                break

    return RunCard(
        run_id=run_id,
        timestamp=_parse_run_timestamp(run_id),
        yaml_name=str(yaml_name),
        types=list(params.get("types") or []),
        agv_counts=[int(x) for x in (params.get("agv_counts") or [])],
        duration_s=float(params.get("duration_s") or 0.0),
        seeds=[int(x) for x in (params.get("random_seeds") or [])],
        demand_mode=str(params.get("demand_mode") or ""),
        top_winner=top_winner,
        headline=str(headline),
        winner_completion=winner_completion,
        winner_throughput=winner_throughput,
        detail_rows=detail_rows,
        variants=variants,
        sparkline_points=sparkline_points,
        has_report=(run_dir / "report.html").exists(),
        has_ranking_csv=(run_dir / "ranking.csv").exists(),
    )


# ── SVG 스파크라인 ─────────────────────────────────────────────────
def _build_sparkline_svg(points: list[tuple[float, float]], width: int = 160, height: int = 36) -> str:
    if not points or len(points) < 2:
        # 단일 포인트 또는 빈 → 도트 표시
        if len(points) == 1:
            x, y = points[0]
            return (
                f'<svg class="spark" viewBox="0 0 {width} {height}" '
                f'xmlns="http://www.w3.org/2000/svg" aria-label="single point">'
                f'<circle cx="{width/2:.0f}" cy="{height/2:.0f}" r="3" fill="#1f6feb" />'
                f'<text x="{width/2 + 8:.0f}" y="{height/2 + 4:.0f}" '
                f'font-size="10" fill="#5a6877">AGV={int(x)} CR={y:.2f}</text>'
                f'</svg>'
            )
        return ""

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min:
        x_max = x_min + 1
    y_pad = max(0.02, (y_max - y_min) * 0.1)
    y_lo, y_hi = max(0.0, y_min - y_pad), min(1.0, y_max + y_pad)
    if y_hi == y_lo:
        y_hi = y_lo + 0.05

    pad_l, pad_r, pad_t, pad_b = 4, 4, 4, 4
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(x: float) -> float:
        return pad_l + (x - x_min) / (x_max - x_min) * plot_w

    def sy(y: float) -> float:
        return pad_t + (1 - (y - y_lo) / (y_hi - y_lo)) * plot_h

    poly = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
    last_x, last_y = points[-1]
    dot = f'<circle cx="{sx(last_x):.1f}" cy="{sy(last_y):.1f}" r="2.5" fill="#1f6feb" />'
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-label="completion sparkline">'
        f'<polyline points="{poly}" fill="none" stroke="#1f6feb" stroke-width="1.5" />'
        f'{dot}'
        f'</svg>'
    )


# ── 토폴로지 칩 (썸네일 대용 미니 마커) ─────────────────────────────
def _build_topology_chips(types: list[str], winner: Optional[str]) -> str:
    """Type 라벨 칩 — 1위는 강조."""
    chips = []
    for t in types:
        is_winner = bool(winner) and (winner == t or (winner.startswith(f"{t}/") if winner else False))
        cls = "chip chip-win" if is_winner else "chip"
        chips.append(f'<span class="{cls}">{html.escape(t)}</span>')
    return "".join(chips)


# ── 카드 HTML ──────────────────────────────────────────────────────
def _format_kpi(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    if digits == 0:
        return f"{value:.0f}{suffix}"
    return f"{value:{f'.{digits}f'}}{suffix}"


def _build_detail_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = (
        "<thead><tr>"
        "<th>variant</th><th>CR</th><th>thr/h</th><th>wait s</th>"
        "<th>head-on</th><th>follow-on</th><th>section</th>"
        "</tr></thead>"
    )
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(str(r['variant']))}</td>"
            f"<td>{_format_kpi(r.get('completion'), 3)}</td>"
            f"<td>{_format_kpi(r.get('throughput'), 1)}</td>"
            f"<td>{_format_kpi(r.get('wait'), 1)}</td>"
            f"<td>{_format_kpi(r.get('headon'), 0)}</td>"
            f"<td>{_format_kpi(r.get('followon'), 0)}</td>"
            f"<td>{_format_kpi(r.get('section'), 0)}</td>"
            "</tr>"
        )
    return f'<table class="detail">{head}<tbody>{"".join(body)}</tbody></table>'


def _build_variant_dropdown(run_id: str, variants: list[dict[str, str]]) -> str:
    if not variants:
        return ""
    items = []
    for v in variants:
        href = f"./{html.escape(run_id)}/{html.escape(v['link'].lstrip('./'))}"
        items.append(
            f'<a href="{href}" target="_blank" rel="noopener">'
            f'{html.escape(v["name"])}</a>'
        )
    return (
        '<details class="dd"><summary>playback ▾</summary>'
        f'<div class="dd-menu">{"".join(items)}</div></details>'
    )


def _build_card_html(card: RunCard) -> str:
    ts_label = (
        card.timestamp.strftime("%Y-%m-%d %H:%M")
        if card.timestamp else card.run_id
    )
    iso = card.timestamp.strftime("%Y-%m-%d") if card.timestamp else ""
    agv_label = (
        f"{min(card.agv_counts)}~{max(card.agv_counts)} AGV"
        if len(card.agv_counts) > 1
        else (f"{card.agv_counts[0]} AGV" if card.agv_counts else "AGV ?")
    )
    seed_label = ", ".join(str(s) for s in card.seeds) if card.seeds else "—"
    chips = _build_topology_chips(card.types, card.top_winner)
    sparkline = _build_sparkline_svg(card.sparkline_points)

    winner_block = ""
    if card.top_winner:
        winner_block = (
            f'<div class="winner">'
            f'  <div class="trophy">🏆</div>'
            f'  <div>'
            f'    <div class="winner-name">{html.escape(card.top_winner)}</div>'
            f'    <div class="winner-kpi">'
            f'      CR={_format_kpi(card.winner_completion, 3)} · '
            f'      thr={_format_kpi(card.winner_throughput, 1)}/h'
            f'    </div>'
            f'  </div>'
            f'  <div class="spark-wrap">{sparkline}</div>'
            f'</div>'
        )

    detail_table = _build_detail_table(card.detail_rows)
    variant_dd = _build_variant_dropdown(card.run_id, card.variants)

    links = []
    if card.has_report:
        links.append(
            f'<a href="./{html.escape(card.run_id)}/report.html" '
            f'target="_blank" rel="noopener">report</a>'
        )
    if card.has_ranking_csv:
        links.append(
            f'<a href="./{html.escape(card.run_id)}/ranking.csv" '
            f'target="_blank" rel="noopener">ranking csv</a>'
        )

    # data-* 속성으로 검색·필터 메타 부착
    return (
        f'<article class="card" '
        f'data-yaml="{html.escape(card.yaml_name)}" '
        f'data-runid="{html.escape(card.run_id)}" '
        f'data-date="{iso}">'
        f'  <header class="card-head">'
        f'    <div class="head-left">'
        f'      <div class="ts">{html.escape(ts_label)}</div>'
        f'      <div class="yaml">{html.escape(card.yaml_name)}</div>'
        f'    </div>'
        f'    <div class="chips">{chips}</div>'
        f'  </header>'
        f'  <div class="meta">{html.escape(agv_label)} · {card.duration_s:.0f}s · seed={html.escape(seed_label)} · {html.escape(card.demand_mode)}</div>'
        f'  <div class="headline">{html.escape(card.headline)}</div>'
        f'  {winner_block}'
        f'  <details class="more"><summary>토폴로지별 KPI ▾</summary>{detail_table}</details>'
        f'  <div class="links">{" · ".join(links)}{(" · " + variant_dd) if variant_dd else ""}</div>'
        f'</article>'
    )


# ── 인덱스 페이지 빌드 ─────────────────────────────────────────────
_PAGE_CSS = """
:root {
  --bg: #f4f6f8;
  --panel: #ffffff;
  --text: #18202a;
  --muted: #5a6877;
  --border: #d7dee7;
  --accent: #1f6feb;
  --good: #0f9d58;
  --chip-bg: #eef2f7;
  --chip-win-bg: #d8efe1;
  --chip-win-fg: #0f6f3c;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
}
.page { max-width: 1280px; margin: 0 auto; padding: 24px; }
header.top {
  display: flex; align-items: baseline; gap: 16px;
  flex-wrap: wrap; margin-bottom: 16px;
}
header.top h1 { margin: 0; font-size: 22px; }
header.top .count { color: var(--muted); font-size: 13px; }
.controls {
  display: flex; gap: 12px; flex-wrap: wrap;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 14px; margin-bottom: 18px;
}
.controls label {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--muted);
}
.controls input {
  border: 1px solid var(--border); border-radius: 4px;
  padding: 4px 8px; font-size: 13px;
}
.controls input[type=search] { min-width: 220px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 14px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex; flex-direction: column; gap: 10px;
  transition: transform 0.08s, box-shadow 0.08s;
}
.card:hover {
  box-shadow: 0 2px 10px rgba(0,0,0,0.05);
  transform: translateY(-1px);
}
.card.hidden { display: none; }
.card-head {
  display: flex; justify-content: space-between;
  align-items: flex-start; gap: 12px;
}
.ts { font-weight: 600; font-size: 14px; }
.yaml { font-size: 12px; color: var(--muted); margin-top: 2px; }
.chips { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.chip {
  padding: 2px 7px; border-radius: 10px;
  background: var(--chip-bg); color: var(--muted);
  font-size: 11px; font-weight: 500;
}
.chip-win { background: var(--chip-win-bg); color: var(--chip-win-fg); }
.meta { font-size: 12px; color: var(--muted); }
.headline { font-size: 13px; line-height: 1.4; color: var(--text); }
.winner {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px; align-items: center;
  background: #f8fafd;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}
.trophy { font-size: 20px; }
.winner-name { font-weight: 600; font-size: 14px; }
.winner-kpi { font-size: 12px; color: var(--muted); margin-top: 2px; }
.spark-wrap { display: flex; align-items: center; }
.spark { display: block; }
.more { font-size: 12px; }
.more summary {
  cursor: pointer; color: var(--accent);
  user-select: none; padding: 2px 0;
}
.detail {
  width: 100%; border-collapse: collapse; margin-top: 6px;
  font-size: 11px;
}
.detail th, .detail td {
  text-align: right; padding: 3px 6px;
  border-bottom: 1px solid var(--border);
}
.detail th { color: var(--muted); font-weight: 500; }
.detail th:first-child, .detail td:first-child { text-align: left; }
.links { font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
.links a { color: var(--accent); text-decoration: none; }
.links a:hover { text-decoration: underline; }
.dd { display: inline-block; position: relative; }
.dd summary {
  cursor: pointer; color: var(--accent);
  list-style: none; user-select: none;
}
.dd summary::-webkit-details-marker { display: none; }
.dd-menu {
  position: absolute; right: 0; top: 100%;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 4px; padding: 4px 0;
  min-width: 180px; z-index: 10;
  box-shadow: 0 2px 10px rgba(0,0,0,0.08);
}
.dd-menu a {
  display: block; padding: 6px 10px;
  text-decoration: none; color: var(--text); font-size: 12px;
}
.dd-menu a:hover { background: var(--chip-bg); }
.empty {
  text-align: center; color: var(--muted);
  padding: 60px 20px; font-size: 14px;
}
"""

_PAGE_JS = """
(function() {
  const search = document.getElementById('q');
  const dateFrom = document.getElementById('dfrom');
  const dateTo = document.getElementById('dto');
  const cards = Array.from(document.querySelectorAll('.card'));
  const counter = document.getElementById('count');
  const empty = document.getElementById('empty');

  function applyFilter() {
    const q = (search.value || '').trim().toLowerCase();
    const from = dateFrom.value || '';
    const to = dateTo.value || '';
    let visible = 0;
    for (const c of cards) {
      const yaml = (c.dataset.yaml || '').toLowerCase();
      const runid = (c.dataset.runid || '').toLowerCase();
      const date = c.dataset.date || '';
      let show = true;
      if (q && !yaml.includes(q) && !runid.includes(q)) show = false;
      if (from && date && date < from) show = false;
      if (to && date && date > to) show = false;
      c.classList.toggle('hidden', !show);
      if (show) visible++;
    }
    counter.textContent = `${visible} / ${cards.length} runs`;
    empty.style.display = visible === 0 ? '' : 'none';
  }

  search.addEventListener('input', applyFilter);
  dateFrom.addEventListener('change', applyFilter);
  dateTo.addEventListener('change', applyFilter);
  applyFilter();
})();
"""


def build_index_html(cards: list[RunCard], generated_at: datetime) -> str:
    cards_sorted = sorted(
        cards,
        key=lambda c: (c.timestamp or datetime.min),
        reverse=True,
    )
    cards_html = "\n".join(_build_card_html(c) for c in cards_sorted)
    if not cards_sorted:
        cards_html = '<div class="empty">실험 결과가 아직 없습니다.</div>'

    gen_label = generated_at.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FAB AMR Experiments — Index</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="page">
  <header class="top">
    <h1>FAB AMR Experiments</h1>
    <span class="count" id="count">{len(cards_sorted)} runs</span>
    <span class="count" style="margin-left:auto">갱신 {gen_label}</span>
  </header>
  <div class="controls">
    <label>검색
      <input id="q" type="search" placeholder="yaml 이름·run_id substring">
    </label>
    <label>날짜 from
      <input id="dfrom" type="date">
    </label>
    <label>to
      <input id="dto" type="date">
    </label>
  </div>
  <div class="grid">
{cards_html}
  </div>
  <div class="empty" id="empty" style="display:none">조건에 맞는 run이 없습니다.</div>
</div>
<script>{_PAGE_JS}</script>
</body>
</html>
"""


# ── 디렉토리 스캔 + 인덱스 재생성 ──────────────────────────────────
def rebuild_index(experiments_root: Path) -> Path:
    """outputs/experiments/ 디렉토리를 스캔해서 index.html 갱신.

    Returns:
        생성된 index.html 경로.
    """
    experiments_root = Path(experiments_root)
    experiments_root.mkdir(parents=True, exist_ok=True)
    cards: list[RunCard] = []
    for child in experiments_root.iterdir():
        if not child.is_dir():
            continue
        card = _load_card(child)
        if card is not None:
            cards.append(card)
    page = build_index_html(cards, datetime.now())
    out = experiments_root / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


# ── CLI ────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="실험 결과 index.html 생성"
    )
    parser.add_argument(
        "root", nargs="?", default="outputs/experiments",
        help="실험 결과 루트 (기본: outputs/experiments)",
    )
    args = parser.parse_args()
    out = rebuild_index(Path(args.root))
    print(f"index 생성: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
