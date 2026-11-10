"""Build explorer/index.html from the published evidence.

    python explorer/build.py

The page embeds each run's raw paired scores and recomputes every verdict in the
browser. Run `node explorer/check.mjs` to confirm the browser maths matches the
Python results exactly.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from offramp import analysis, ledger  # noqa: E402

LABELS = {
    "classify": "Ticket classification", "extract": "Invoice extraction", "math": "Word problems",
    "code": "Code, run in Otari's sandbox", "rewrite": "Customer emails",
    "hard-extract": "Hard extraction: messy order logs", "hard-math": "Hard math: multi-step",
}
GRADERS = {
    "classify": "exact label", "extract": "strict JSON fields", "math": "final number",
    "code": "unit tests", "rewrite": "cross-family jury",
    "hard-extract": "strict JSON fields", "hard-math": "final number",
}
RUNS = [
    ("live-2", "Main run", ["classify", "extract", "math", "code", "rewrite"]),
    ("live-4-hard", "Hard-segment rerun", ["hard-extract", "hard-math"]),
]


def run_data(name: str, label: str, use: list[str], shares: dict) -> dict:
    path = ROOT / "evidence" / name / "ledger.jsonl"
    ok, head, _ = ledger.verify_chain(path)
    if not ok:
        raise SystemExit(f"{name}: {head}")
    summary = analysis.summarize(path)
    scores: dict = {}
    for entry in ledger.read(path):
        if entry["event"] == "score":
            data = entry["data"]
            scores.setdefault((data["segment"], data["candidate"]), []).append(data["d"])
    arms = []
    for segment, info in summary["segments"].items():
        for candidate, arm in info["arms"].items():
            arms.append({
                "segment": segment, "candidate": candidate,
                "ds": scores.get((segment, candidate), []),
                "status": arm["status"], "mean_cost": arm["mean_cost"], "quality": arm["quality"],
                "published": {"verdict": arm["verdict"], "log_e_for": arm["log_e_for"],
                              "log_e_against": arm["log_e_against"]},
            })
    segments = {
        segment: {
            "label": LABELS.get(segment, segment), "grader": GRADERS.get(segment, ""),
            "share": shares[segment], "cases": info["cases"],
            "inc_cost": info["incumbent"]["mean_cost"], "inc_quality": info["incumbent"]["quality"],
        }
        for segment, info in summary["segments"].items()
    }
    return {"name": f"{label} ({name})", "head": head, "use": use, "arms": arms, "segments": segments}


def main() -> None:
    main_summary = analysis.summarize(ROOT / "evidence" / "live-2" / "ledger.jsonl")
    shares = {k: v["share"] for k, v in main_summary["segments"].items()}
    jury = json.loads((ROOT / "evidence" / "jury-injection.json").read_text(encoding="utf-8"))
    data = {
        "jury": {"summary": jury["summary"], "attacks": jury["attacks"],
                 "spend_usd": jury["spend_usd"], "judges": jury["judges"]},
        "incumbent": main_summary["config"]["incumbent"],
        "candidates": main_summary["config"]["candidates"],
        "order": [s for _, _, use in RUNS for s in use],
        "runs": [run_data(name, label, use, shares) for name, label, use in RUNS],
    }
    template = (ROOT / "explorer" / "template.html").read_text(encoding="utf-8")
    page = template.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    (ROOT / "explorer" / "index.html").write_text(page, encoding="utf-8")
    print(f"explorer/index.html: {len(page):,} bytes")


main()
