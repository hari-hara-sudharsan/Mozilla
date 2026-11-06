"""A single self-contained HTML report. It has no scripts and no network calls."""

from __future__ import annotations

import math
from html import escape

from .analysis import policy_yaml
from .stats import early_stop_log_threshold

LOG10 = math.log(10)
SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"]

CSS = """
:root{--bg:#f7f5f0;--panel:#fffdf9;--ink:#191816;--muted:#6a665e;--line:#ddd8cd;
--ok:#1c7a4d;--ok-bg:#dcefe3;--bad:#b3341f;--bad-bg:#f7e0da;--wait:#8a6410;--wait-bg:#f5ead0;
--s1:#2456c9;--s2:#c2410c;--s3:#0f766e;--s4:#7c3aed;--s5:#a21caf;--s6:#4d7c0f}
@media (prefers-color-scheme:dark){:root{--bg:#141311;--panel:#1d1b19;--ink:#f1eee8;--muted:#a39e94;
--line:#37332d;--ok:#6fd3a0;--ok-bg:#16352a;--bad:#f49a86;--bad-bg:#42201a;--wait:#e6bf63;--wait-bg:#3a2e12;
--s1:#8db0ff;--s2:#fb9b63;--s3:#5fd4c6;--s4:#c4a5ff;--s5:#ee93f5;--s6:#b3dc66}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:40px 24px 80px}
code,pre,.num{font-family:ui-monospace,"Cascadia Code",Menlo,Consolas,monospace}
.num{font-variant-numeric:tabular-nums}
.eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
h1{font-size:clamp(34px,6vw,56px);line-height:1.06;letter-spacing:-.02em;margin:0 0 16px;max-width:17em}
h2{font-size:22px;letter-spacing:-.01em;margin:56px 0 6px}
h3{font-size:18px;margin:0}
.lede{color:var(--muted);max-width:44em;margin:0}
.sim{background:var(--wait-bg);color:var(--wait);border:1px solid currentColor;border-radius:8px;padding:12px 16px;margin:0 0 28px;font-weight:600}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-top:32px}
.stat{background:var(--panel);padding:18px 20px}
.stat b{display:block;font-size:24px;white-space:nowrap;line-height:1.15;letter-spacing:-.01em;font-weight:650}
.stat span{color:var(--muted);font-size:14px}
.segment{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:22px;margin-top:16px}
.head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px 20px;align-items:baseline;margin-bottom:12px}
.route{color:var(--muted);font-size:14px}
.body{display:grid;grid-template-columns:minmax(0,1fr);gap:14px}
svg{width:100%;height:auto;display:block}
svg text{font:11px ui-sans-serif,system-ui,sans-serif;fill:var(--muted)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{font-weight:500;color:var(--muted);text-align:right;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:0}
tr:last-child td{border-bottom:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:1px 8px;border-radius:99px}
.proven{background:var(--ok-bg);color:var(--ok)}
.rejected{background:var(--bad-bg);color:var(--bad)}
.undecided,.dominated,.unavailable{background:var(--wait-bg);color:var(--wait)}
pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;overflow-x:auto;font-size:13px;line-height:1.5;margin:12px 0 0}
dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px 20px;margin:12px 0 0;font-size:14px}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
p.note{color:var(--muted);font-size:14px;max-width:48em}
"""


def _money(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"${value:,.{digits}f}"


def _per_thousand(value: float | None) -> str:
    return "n/a" if value is None else f"${value * 1000:,.3f}"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 1000:,.0f} ms"


def _signed_evidence(point_for: float, point_against: float) -> float:
    """An e-value below 1 is no evidence at all, so it plots as zero."""
    value = max(point_for, 0.0) if point_for >= point_against else -max(point_against, 0.0)
    return value / LOG10


def _chart(segment: dict, candidates: list[str], threshold: float) -> str:
    width, height, left, right, top, bottom = 940, 200, 20, 12, 14, 24
    limit = threshold / LOG10
    y_max = limit * 1.3
    x_max = max([len(segment["arms"][c]["trail"]) for c in candidates] + [10])

    def x(n: float) -> float:
        return left + (width - left - right) * n / x_max

    def y(value: float) -> float:
        clipped = max(-y_max, min(y_max, value))
        return top + (height - top - bottom) * (y_max - clipped) / (2 * y_max)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             'aria-label="Evidence for each candidate as cases accumulate">']
    for value, label in ((limit, "proven"), (-limit, "rejected")):
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(value):.1f}" '
                     f'y2="{y(value):.1f}" stroke="var(--muted)" stroke-dasharray="4 4"/>')
        offset = -5 if value > 0 else 13
        parts.append(f'<text x="{left}" y="{y(value) + offset:.1f}">{label}</text>')
    parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}" '
                 'stroke="var(--line)"/>')
    parts.append(f'<text x="{left - 6}" y="{y(0) + 4:.1f}" text-anchor="end">0</text>')
    parts.append(f'<text x="{left}" y="{height - 6}">0</text>')
    parts.append(f'<text x="{width - right}" y="{height - 6}" text-anchor="end">'
                 f'{x_max} cases</text>')

    for index, candidate in enumerate(candidates):
        trail = segment["arms"][candidate]["trail"]
        if not trail:
            continue
        color = SERIES[index % len(SERIES)]
        points = [f"{x(0):.1f},{y(0):.1f}"]
        points += [f"{x(n):.1f},{y(_signed_evidence(a, b)):.1f}" for n, a, b in trail]
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
                     'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        last = trail[-1]
        parts.append(f'<circle cx="{x(last[0]):.1f}" cy="{y(_signed_evidence(last[1], last[2])):.1f}" '
                     f'r="3.5" fill="{color}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _segment(name: str, segment: dict, candidates: list[str], incumbent: str,
             threshold: float) -> str:
    route = segment["route"]
    target = f"Route to <code>{escape(route[0])}</code>" if route else (
        f"Stay on <code>{escape(incumbent)}</code>")
    base = segment["incumbent"]
    rows = [f'<tr><td>{escape(incumbent)} <span class="route">incumbent</span></td><td></td>'
            f'<td class="num">{_percent(base["quality"])}</td><td class="num">baseline</td><td></td>'
            f'<td class="num">{_per_thousand(base["mean_cost"])}</td>'
            f'<td class="num">{_ms(base["latency_p95"])}</td></tr>']
    for index, candidate in enumerate(candidates):
        arm = segment["arms"][candidate]
        color = SERIES[index % len(SERIES)]
        rows.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>{escape(candidate)}</td>'
            f'<td><span class="tag {arm["verdict"]}" title="{escape(arm["reason"] or "")}">'
            f'{arm["verdict"]}</span></td>'
            f'<td class="num">{_percent(arm["quality"])}</td>'
            f'<td class="num">{arm["mean_d"] * 100:+.1f} pts</td>'
            f'<td class="num">{arm["stopped_at"]}</td>'
            f'<td class="num">{_per_thousand(arm["mean_cost"])}</td>'
            f'<td class="num">{_ms(arm["latency_p95"])}</td></tr>'
        )
    return f"""
<section class="segment">
  <div class="head"><h3>{escape(name)}</h3>
  <span class="route">{segment["share"] * 100:.0f}% of traffic · {target}</span></div>
  <div class="body">
    <div>{_chart(segment, candidates, threshold)}</div>
    <div class="scroll"><table>
      <tr><th>Model</th><th>Verdict</th><th>Quality</th><th>vs incumbent</th><th>Cases</th><th>Per 1k req</th><th>p95</th></tr>
      {"".join(rows)}
    </table></div>
  </div>
</section>"""


def render(summary: dict, head: str) -> str:
    totals = summary["totals"]
    config = summary["config"]
    candidates = config["candidates"]
    threshold = early_stop_log_threshold(config["alpha"], summary["n_arms"])
    share = totals["offramped_share"]
    savings = totals["savings_pct"]

    if share > 0:
        headline = f"{share * 100:.0f}% of this workload can leave {escape(config['incumbent'])}."
    else:
        headline = f"Nothing has earned its way off {escape(config['incumbent'])} yet."
    lede = (f"Offramp replayed this workload through Otari and tested each open-weight "
            f"candidate for being no more than {config['margin'] * 100:.0f} points worse than "
            f"the incumbent, at a {config['alpha'] * 100:.0f}% false discovery rate. "
            "Segments without proof stay where they are.")
    banner = ""
    if summary["simulated"]:
        banner = ('<p class="sim">SIMULATED RUN. These are fictional models on a local stand-in '
                  'gateway. Nothing here says anything about a real model.</p>')

    gateway = totals["gateway_measured_usd"]
    gateway_line = (
        f"<dt>Gateway-measured spend</dt><dd class='num'>{_money(gateway, 4)}</dd>"
        if gateway is not None else "")
    sections = "".join(
        _segment(name, segment, candidates, config["incumbent"], threshold)
        for name, segment in summary["segments"].items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offramp Report</title><style>{CSS}</style></head><body><main>
{banner}
<p class="eyebrow">Offramp evidence report · run {escape(summary["run_id"])}</p>
<h1>{headline}</h1>
<p class="lede">{lede}</p>
<div class="stats">
  <div class="stat"><b class="num">{"n/a" if savings is None else f"{savings:.0f}%"}</b><span>projected cost reduction</span></div>
  <div class="stat"><b class="num">{_per_thousand(totals["baseline_cost_per_request"])} → {_per_thousand(totals["routed_cost_per_request"])}</b><span>per 1,000 requests</span></div>
  <div class="stat"><b class="num">{_money(totals["spent_usd"], 3)}</b><span>cost of this proof</span></div>
  <div class="stat"><b class="num">{_money(totals["saved_by_stopping_usd"], 3)}</b><span>not spent, by stopping early</span></div>
</div>

<h2>Evidence by segment</h2>
<p class="note">Each line is one candidate's evidence as cases accumulate, in log10 of the e-value.
Crossing a dashed line settles the arm, and the run stops paying for it. Looking after every batch
does not weaken the result. Final verdicts apply e-BH across all arms, so a line can end just short of
the dashed line and still be proven.</p>
{sections}

<h2>Routing policy</h2>
<p class="note">Callers send <code>model: offramp-&lt;segment&gt;</code>. The incumbent stays as the last
fallback, so an open-weight failure degrades to today's behaviour and does not drop the request.</p>
<pre>{escape(policy_yaml(summary, head))}</pre>

<h2>Receipts</h2>
<dl>
  <dt>Ledger head</dt><dd><code>{head}</code></dd>
  <dt>Workload SHA-256</dt><dd><code>{summary["workload_sha256"]}</code></dd>
  <dt>Gateway</dt><dd><code>{escape(summary["base_url"])}</code></dd>
  <dt>Comparisons</dt><dd class="num">{totals["comparisons"]:,}</dd>
  <dt>Run ended</dt><dd>{escape(summary["stop_reason"])}</dd>
  <dt>Failed calls / dropped</dt><dd class="num">{totals["failed_calls"]} / {totals["dropped"]}</dd>
  <dt>Calls with no price</dt><dd class="num">{totals["unpriced_calls"]}</dd>
  <dt>Ledger-reported spend</dt><dd class="num">{_money(totals["spent_usd"], 4)}</dd>
  {gateway_line}
</dl>
<p class="note">Run <code>offramp verify</code> on this folder to recheck the hash chain and recompute every
verdict from the raw scores. The guarantee covers traffic like this sample. It does not cover later
drift, so run it again when your workload changes.</p>
</main></body></html>
"""
