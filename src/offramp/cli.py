"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import analysis, graders, ledger, report, simulator, workload
from .capture import CaptureProxy
from .engine import Config, Engine
from .otari import DEFAULT_BASE_URL, Otari, OtariError, load_env

EXAMPLE_WORKLOAD = Path("examples/workload.jsonl")


def _client() -> Otari:
    load_env()
    return Otari(os.environ.get("OTARI_API_KEY", ""),
                 os.environ.get("OTARI_BASE_URL", DEFAULT_BASE_URL))


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def write_outputs(out: Path, head: str) -> dict:
    summary = analysis.summarize(out / "ledger.jsonl")
    (out / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "policy.yaml").write_text(analysis.policy_yaml(summary, head), encoding="utf-8")
    (out / "policy.json").write_text(json.dumps(analysis.policies(summary), indent=2),
                                     encoding="utf-8")
    (out / "report.html").write_text(report.render(summary, head), encoding="utf-8")
    return summary


def _print_summary(summary: dict, out: Path) -> None:
    print()
    for name, segment in summary["segments"].items():
        route = segment["route"][0] if segment["route"] else "stay on incumbent"
        verdicts = ", ".join(f"{arm['verdict']}@{arm['stopped_at']}"
                             for arm in segment["arms"].values())
        print(f"  {name:<16} {route:<36} [{verdicts}]")
    totals = summary["totals"]
    saving = totals["savings_pct"]
    print(f"\n  Traffic that can move: {totals['offramped_share'] * 100:.0f}%")
    print(f"  Projected cost change: {'unknown' if saving is None else f'-{saving:.0f}%'}")
    print(f"  Cost of this proof:    ${totals['spent_usd']:.4f}")
    if summary["simulated"]:
        print("\n  SIMULATED RUN: fictional models, so this says nothing about real ones.")
    print(f"\n  Report: {out / 'report.html'}")


def _run(client: Otari, cases: list, digest: str, config: Config, out: Path) -> int:
    if any(case.grader["type"] == "jury" for case in cases):
        if not config.judges and not config.decision_model:
            print("This workload has jury cases, so pass --judges or --decision-model.",
                  file=sys.stderr)
            return 2
    head = Engine(client, cases, digest, config, out).run()
    _print_summary(write_outputs(out, head), out)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cases, digest = workload.load(Path(args.workload))
    config = Config(
        incumbent=args.incumbent, candidates=_split(args.candidates), judges=_split(args.judges),
        decision_model=args.decision_model, budget_usd=args.budget,
        sandbox_cost_usd=args.sandbox_cost, margin=args.margin,
        alpha=args.alpha, seed=args.seed, batch=args.batch, workers=args.workers,
        store_text=args.store_text,
    )
    return _run(_client(), cases, digest, config, Path(args.out))


def cmd_demo(args: argparse.Namespace) -> int:
    cases, digest = workload.load(Path(args.workload))
    server = simulator.Simulator(cases).start()
    client = Otari("simulated-key", server.base_url, backoff=0.01)
    client.simulated = True
    config = Config(incumbent=simulator.DEMO_INCUMBENT, candidates=simulator.DEMO_CANDIDATES,
                    judges=simulator.DEMO_JUDGES, budget_usd=args.budget, seed=args.seed)
    out = Path(args.out or tempfile.mkdtemp(prefix="offramp-demo-"))
    print("Offline demo: fictional models on a local simulator. No key, no cost.\n")
    try:
        return _run(client, cases, digest, config, out)
    finally:
        server.stop()


def cmd_verify(args: argparse.Namespace) -> int:
    out = Path(args.run)
    ok, head, count = ledger.verify_chain(out / "ledger.jsonl")
    if not ok:
        print(f"FAILED: {head}")
        return 1
    print(f"Chain intact: {count} entries, head {head}")

    summary = analysis.summarize(out / "ledger.jsonl")
    checks = {
        "results.json": json.dumps(summary, indent=2),
        "policy.yaml": analysis.policy_yaml(summary, head),
        "policy.json": json.dumps(analysis.policies(summary), indent=2),
    }
    failed = False
    for name, expected in checks.items():
        matches = (out / name).read_text(encoding="utf-8") == expected
        failed |= not matches
        print(f"{'Recomputed and matches' if matches else 'DOES NOT MATCH'}: {name}")
    if args.head and args.head != head:
        print(f"FAILED: expected head {args.head}")
        return 1
    return 1 if failed else 0


def cmd_gate(args: argparse.Namespace) -> int:
    """For CI: fail unless enough traffic has proof behind it."""
    summary = analysis.summarize(Path(args.run) / "ledger.jsonl")
    share = summary["totals"]["offramped_share"]
    print(f"Proven share of traffic: {share * 100:.1f}% (required {args.min_share * 100:.1f}%)")
    if summary["simulated"] and not args.allow_simulated:
        print("Refusing a simulated run.")
        return 1
    return 0 if share >= args.min_share else 1


def cmd_apply(args: argparse.Namespace) -> int:
    out = Path(args.run)
    ok, head, _ = ledger.verify_chain(out / "ledger.jsonl")
    if not ok:
        print(f"Refusing to apply: {head}", file=sys.stderr)
        return 1
    summary = analysis.summarize(out / "ledger.jsonl")
    if summary["simulated"]:
        print("Refusing to apply a simulated run.", file=sys.stderr)
        return 1
    client = _client()
    for name, spec in analysis.policies(summary).items():
        body = {"name": name, "spec": spec, "workspace_id": args.workspace_id}
        try:
            client.request("POST", "/organizations/me/routing-policies", body)
        except OtariError as error:
            print(f"{name}: {error}", file=sys.stderr)
            print("Only org owners and admins can write policies, and the dashboard may be "
                  "needed. policy.yaml has the same content.", file=sys.stderr)
            return 1
        print(f"Applied {name}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    client = _client()
    print(f"Gateway: {client.base_url}")
    try:
        print(f"Models visible: {len(client.models())}")
    except OtariError as error:
        print(f"Cannot list models: {error}")
        return 1
    for tool in client.tools().get("data", []):
        state = "available" if tool.get("available") else f"off ({tool.get('reason')})"
        print(f"Tool {tool.get('id')}: {state}")
    remaining = client.remaining_usd()
    print("Gateway budget readout: " + (
        f"${remaining:.2f} remaining" if remaining is not None
        else "not available, so Offramp's own ledger enforces --budget"))
    return 0


def _tokens(text: str) -> int:
    return len(text) // 4 + 1


def _call_cost(pricing: dict, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * float(pricing.get("input_price_per_million") or 0)
            + tokens_out * float(pricing.get("output_price_per_million") or 0)) / 1e6


def cmd_plan(args: argparse.Namespace) -> int:
    """Check a run against the live catalog and price it, without spending."""
    cases, _ = workload.load(Path(args.workload))
    candidates, judges = _split(args.candidates), _split(args.judges)
    client = _client()
    catalog = {model.get("id"): model for model in client.models()}

    problems = [f"{model} is not in this key's catalog"
                for model in [args.incumbent, *candidates, *judges] if model not in catalog]
    has_jury = any(case.grader["type"] == "jury" for case in cases)
    if any(case.grader["type"] == "python_tests" for case in cases):
        tools = {tool.get("id"): tool for tool in client.tools().get("data", [])}
        if not tools.get("otari_code_execution", {}).get("available"):
            problems.append("code cases need otari_code_execution, which is off for this key")
    if has_jury:
        problems += [f"no judge is independent of {candidate} and {args.incumbent}"
                     for candidate in candidates
                     if not graders.independent_judges(judges, args.incumbent, candidate)]
    if problems:
        print("\n".join(f"Problem: {problem}" for problem in problems))
        return 1

    out_tokens = args.output_tokens
    tokens_in = [sum(_tokens(str(m.get("content"))) for m in case.messages) for case in cases]
    worst = 0.0
    print(f"{len(cases)} cases, assuming {out_tokens} output tokens per answer.")
    print("Cost if nothing stops early:")
    for role, model in [("incumbent", args.incumbent),
                        *[("candidate", candidate) for candidate in candidates]]:
        pricing = catalog[model].get("pricing") or {}
        cost = sum(_call_cost(pricing, tokens, out_tokens) for tokens in tokens_in)
        worst += cost
        print(f"  ${cost:8.4f}  {role:<10} {model}")
    if has_jury:
        jury_in = [tokens + 2 * out_tokens + 150 for tokens, case in zip(tokens_in, cases)
                   if case.grader["type"] == "jury"]
        for judge in judges:
            pricing = catalog[judge].get("pricing") or {}
            cost = len(candidates) * sum(_call_cost(pricing, tokens, 20) for tokens in jury_in)
            worst += cost
            print(f"  ${cost:8.4f}  {'judge':<10} {judge}")
    code_cases = sum(case.grader["type"] == "python_tests" for case in cases)
    sandbox = code_cases * (len(candidates) + 1) * args.sandbox_cost
    worst += sandbox
    print(f"  ${sandbox:8.4f}  sandbox   {code_cases * (len(candidates) + 1)} runs, estimated "
          f"at ${args.sandbox_cost} each")
    print(f"  ${worst:8.4f}  total")
    print("Reasoning models can use far more output tokens than this. --budget stays a "
          "hard stop whichever way it goes.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    for model in _client().models():
        name = model.get("id", "") if isinstance(model, dict) else str(model)
        if args.grep.casefold() in name.casefold():
            print(name)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    proxy = CaptureProxy(_client(), Path(args.out), args.port)
    print(f"Recording to {args.out}. Point your client at "
          f"http://127.0.0.1:{args.port}/api/v1 and press Ctrl+C to stop.")
    print("This file will hold full prompts and answers, so treat it as sensitive.")
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        print(f"\nRecorded {proxy.count} requests.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offramp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="test candidates against the incumbent")
    run.add_argument("--workload", required=True)
    run.add_argument("--incumbent", required=True)
    run.add_argument("--candidates", required=True, help="comma-separated model ids")
    run.add_argument("--judges", help="comma-separated jury models from other families")
    run.add_argument("--decision-model", help="add a juror through Otari's Decisions API")
    run.add_argument("--budget", type=float, default=2.0, help="USD ceiling for this run")
    run.add_argument("--sandbox-cost", type=float, default=0.011,
                     help="estimated USD per sandbox run, counted against --budget")
    run.add_argument("--margin", type=float, default=0.10, help="largest acceptable quality loss")
    run.add_argument("--alpha", type=float, default=0.05, help="false discovery rate")
    run.add_argument("--seed", type=int, default=7)
    run.add_argument("--batch", type=int, default=4, help="cases per segment between looks")
    run.add_argument("--workers", type=int, default=8)
    run.add_argument("--store-text", action="store_true", help="keep answers in the ledger")
    run.add_argument("--out", required=True)
    run.set_defaults(func=cmd_run)

    demo = commands.add_parser("demo", help="run offline against the simulator")
    demo.add_argument("--workload", default=str(EXAMPLE_WORKLOAD))
    demo.add_argument("--budget", type=float, default=5.0)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--out")
    demo.set_defaults(func=cmd_demo)

    verify = commands.add_parser("verify", help="recheck a run from its raw evidence")
    verify.add_argument("run")
    verify.add_argument("--head", help="the published ledger head to pin against")
    verify.set_defaults(func=cmd_verify)

    gate = commands.add_parser("gate", help="CI check on the proven share of traffic")
    gate.add_argument("run")
    gate.add_argument("--min-share", type=float, default=0.5)
    gate.add_argument("--allow-simulated", action="store_true")
    gate.set_defaults(func=cmd_gate)

    apply_ = commands.add_parser("apply", help="write the policies to Otari")
    apply_.add_argument("run")
    apply_.add_argument("--workspace-id", required=True)
    apply_.set_defaults(func=cmd_apply)

    commands.add_parser("doctor", help="check key, models, tools and budget"
                        ).set_defaults(func=cmd_doctor)

    plan = commands.add_parser("plan", help="validate and price a run, spending nothing")
    plan.add_argument("--workload", required=True)
    plan.add_argument("--incumbent", required=True)
    plan.add_argument("--candidates", required=True)
    plan.add_argument("--judges")
    plan.add_argument("--output-tokens", type=int, default=300)
    plan.add_argument("--sandbox-cost", type=float, default=0.011)
    plan.set_defaults(func=cmd_plan)

    models = commands.add_parser("models", help="list models this key can use")
    models.add_argument("--grep", default="")
    models.set_defaults(func=cmd_models)

    capture = commands.add_parser("capture", help="record real traffic through a local proxy")
    capture.add_argument("--out", default="capture.jsonl")
    capture.add_argument("--port", type=int, default=8787)
    capture.set_defaults(func=cmd_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, FileExistsError, OtariError) as error:
        print(f"offramp: {error}", file=sys.stderr)
        return 1
