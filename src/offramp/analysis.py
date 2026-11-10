"""Turn a ledger into verdicts, savings and a routing policy.

This is a pure function of the ledger. The run and `offramp verify` both call it,
so verification really does recompute every conclusion from the raw evidence.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from pathlib import Path

from . import ledger as ledger_module
from .stats import Arm, e_bh


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize(path: Path) -> dict:
    start: dict = {}
    end: dict = {}
    arms: dict[tuple[str, str], Arm] = {}
    status: dict[tuple[str, str], dict] = {}
    q_candidate: dict[tuple[str, str], list[float]] = defaultdict(list)
    q_incumbent: dict[str, dict[str, float]] = defaultdict(dict)
    answer_cost: dict[tuple[str, str], list[float]] = defaultdict(list)
    grading_cost: dict[tuple[str, str], float] = defaultdict(float)
    latency: dict[tuple[str, str], list[float]] = defaultdict(list)
    incumbent_cost: dict[str, list[float]] = defaultdict(list)
    incumbent_latency: dict[str, list[float]] = defaultdict(list)
    by_role: dict[str, float] = defaultdict(float)
    unpriced = failures = drops = 0
    truncated: dict[str, int] = defaultdict(int)

    for entry in ledger_module.read(path):
        event, data = entry["event"], entry["data"]
        if event == "run_start":
            start = data
            margin = data["config"]["margin"]
            arms = {(s, c): Arm(margin) for s in data["segments"]
                    for c in data["config"]["candidates"]}
        elif event == "run_end":
            end = data
        elif event == "drop":
            drops += 1
        elif event == "arm_status":
            status[(data["segment"], data["candidate"])] = data
        elif event == "score":
            key = (data["segment"], data["candidate"])
            arms[key].update(data["d"])
            if data["q_candidate"] is not None:
                q_candidate[key].append(data["q_candidate"])
            if data["q_incumbent"] is not None:
                q_incumbent[data["segment"]][data["case"]] = data["q_incumbent"]
        elif event == "call":
            segment, candidate, role = data["segment"], data.get("candidate"), data["role"]
            if not data.get("ok"):
                failures += 1
                continue
            if data.get("finish_reason") == "length":
                truncated[data["model"]] += 1
            cost = data.get("cost_usd")
            if cost is None:
                unpriced += not data.get("recorded")
                cost = 0.0
            if not data.get("recorded"):
                by_role[role] += cost
            if role == "incumbent":
                incumbent_cost[segment].append(cost)
                if data.get("latency_s") is not None:
                    incumbent_latency[segment].append(data["latency_s"])
            elif role == "candidate":
                answer_cost[(segment, candidate)].append(cost)
                latency[(segment, candidate)].append(data["latency_s"])
            elif candidate:
                grading_cost[(segment, candidate)] += cost

    config = start["config"]
    keys = sorted(arms)
    alpha = config["alpha"]
    proven = dict(zip(keys, e_bh([arms[k].log_e_for for k in keys], alpha)))
    rejected = dict(zip(keys, e_bh([arms[k].log_e_against for k in keys], alpha)))

    def verdict(key: tuple[str, str]) -> str:
        raw = status.get(key, {}).get("status")
        if raw == "unavailable" and arms[key].n == 0:
            return "unavailable"
        if proven[key] and not rejected[key]:
            return "proven"
        if rejected[key] and not proven[key]:
            return "rejected"
        return "dominated" if raw == "dominated" else "undecided"

    segments: dict[str, dict] = {}
    total_weight = sum(meta["weight"] for meta in start["segments"].values())
    grid_cost = 0.0
    for name, meta in start["segments"].items():
        rows: dict[str, dict] = {}
        for candidate in config["candidates"]:
            key = (name, candidate)
            arm = arms[key]
            answers = answer_cost[key]
            per_case = (sum(answers) + grading_cost[key]) / arm.n if arm.n else 0.0
            grid_cost += per_case * meta["cases"]
            rows[candidate] = {
                "verdict": verdict(key),
                "status": status.get(key, {}).get("status"),
                "reason": status.get(key, {}).get("reason"),
                "stopped_at": arm.n,
                "mean_d": arm.mean_d,
                "quality": _mean(q_candidate[key]),
                "mean_cost": _mean(answers),
                "latency_p50": _percentile(latency[key], 0.5),
                "latency_p95": _percentile(latency[key], 0.95),
                "log_e_for": arm.log_e_for,
                "log_e_against": arm.log_e_against,
                "trail": [[n, round(a, 4), round(b, 4)] for n, a, b in arm.trail],
            }
        base = _mean(incumbent_cost[name])
        grid_cost += (base or 0.0) * meta["cases"]
        winners = sorted(
            (c for c, row in rows.items() if row["verdict"] == "proven"),
            key=lambda c: (rows[c]["mean_cost"] is None, rows[c]["mean_cost"] or 0.0),
        )
        segments[name] = {
            "cases": meta["cases"],
            "share": meta["weight"] / total_weight,
            "incumbent": {
                "mean_cost": base,
                "quality": _mean(list(q_incumbent[name].values())),
                "latency_p50": _percentile(incumbent_latency[name], 0.5),
                "latency_p95": _percentile(incumbent_latency[name], 0.95),
            },
            "arms": rows,
            "route": winners,
        }

    baseline = sum(s["share"] * (s["incumbent"]["mean_cost"] or 0.0) for s in segments.values())
    routed = sum(
        s["share"] * ((s["arms"][s["route"][0]]["mean_cost"] or 0.0) if s["route"]
                      else (s["incumbent"]["mean_cost"] or 0.0))
        for s in segments.values()
    )
    spent = sum(by_role.values())
    gateway_before = start.get("gateway_remaining_usd")
    gateway_after = end.get("gateway_remaining_usd")
    return {
        "run_id": start["run_id"],
        "simulated": start["simulated"],
        "base_url": start["base_url"],
        "workload_sha256": start["workload_sha256"],
        "config": config,
        "n_arms": len(keys),
        "stop_reason": end.get("stop_reason", "run did not finish"),
        "segments": segments,
        "totals": {
            "spent_usd": spent,
            "by_role": dict(by_role),
            "full_grid_usd": grid_cost,
            "saved_by_stopping_usd": max(grid_cost - spent, 0.0),
            "baseline_cost_per_request": baseline,
            "routed_cost_per_request": routed,
            "savings_pct": (1 - routed / baseline) * 100 if baseline else None,
            "offramped_share": sum(s["share"] for s in segments.values() if s["route"]),
            "comparisons": sum(arm.n for arm in arms.values()),
            "unpriced_calls": unpriced,
            "failed_calls": failures,
            "dropped": drops,
            "truncated_by_model": dict(truncated),
            "sandbox_runs": end.get("sandbox_runs"),
            "sandbox_estimate_usd": (
                end["sandbox_runs"] * config.get("sandbox_cost_usd", 0.010)
                if end.get("sandbox_runs") is not None else None
            ),
            "models_refusing_temperature": end.get("models_refusing_temperature", []),
            "gateway_measured_usd": (
                gateway_before - gateway_after
                if gateway_before is not None and gateway_after is not None else None
            ),
        },
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "default"


def policies(summary: dict) -> dict[str, dict]:
    """One Otari routing policy per segment. The incumbent is always the last
    resort, so a failing open-weight model falls back and never drops a request."""
    incumbent = summary["config"]["incumbent"]
    specs: dict[str, dict] = {}
    for name, segment in summary["segments"].items():
        route = segment["route"]
        spec: dict = {"select": [{"default": route[0] if route else incumbent}]}
        if route:
            spec["on_failure"] = [*route[1:], incumbent]
        specs[f"offramp-{_slug(name)}"] = spec
    return specs


def policy_yaml(summary: dict, head: str) -> str:
    lines = [
        "# Generated by Offramp. Callers send `model: <policy name>`.",
        f"# Evidence: run {summary['run_id']}, ledger head {head}",
        f"# Non-inferiority margin {summary['config']['margin']}, "
        f"false discovery rate {summary['config']['alpha']}",
    ]
    if summary["simulated"]:
        lines.append("# SIMULATED RUN: no real model was tested. Do not deploy this policy.")
    lines += ["routing:", "  policies:"]
    for name, spec in policies(summary).items():
        lines.append(f"    {name}:")
        lines.append("      select:")
        lines.append(f"        - default: {spec['select'][0]['default']}")
        if spec.get("on_failure"):
            lines.append("      on_failure:")
            lines += [f"        - {model}" for model in spec["on_failure"]]
    return "\n".join(lines) + "\n"
