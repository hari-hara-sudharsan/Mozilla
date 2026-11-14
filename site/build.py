"""Build site/public/index.html, the Offramp website, from the published evidence.

    python site/build.py
    node site/check.mjs

It reuses the Evidence Explorer's data, so every number on the site comes from the
same hash-chained ledgers, and adds the guard rollback replay and the live proofs.
"""

import importlib.util
import json
import math
import shutil
import sys
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from offramp import ledger, simulator, workload  # noqa: E402
from offramp.guard import Guard  # noqa: E402
from offramp.otari import Otari  # noqa: E402

spec = importlib.util.spec_from_file_location("explorer_build", ROOT / "explorer" / "build.py")
explorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(explorer)

DEGRADE_AT = 31


def guard_replay() -> dict:
    """Run the offline guard demo and keep its evidence trail. It is a simulation."""
    cases = [c for c in workload.load(ROOT / "examples" / "workload.jsonl")[0]
             if c.grader["type"] == "jury"]
    route = {"rate": 0.1, "skill": {c.segment: 0.97 for c in cases}}
    models = {
        simulator.DEMO_INCUMBENT: {"rate": 6.0, "skill": {c.segment: 0.97 for c in cases}},
        "offramp-support": route,
        **{judge: {"rate": 0.2, "skill": {}} for judge in simulator.DEMO_JUDGES},
    }
    server = simulator.Simulator(cases, models).start()
    client = Otari("simulated-key", server.base_url, backoff=0.01)
    out = Path(tempfile.mkdtemp(prefix="offramp-site-guard-"))
    guard = Guard(client, simulator.DEMO_INCUMBENT, simulator.DEMO_JUDGES, out, port=0,
                  sample=1.0, quiet=True)
    guard.shadows.shutdown()
    guard.shadows = ThreadPoolExecutor(max_workers=1)
    threading.Thread(target=guard.serve_forever, args=(0.01,), daemon=True).start()
    url = f"http://127.0.0.1:{guard.server_address[1]}/api/v1/chat/completions"
    try:
        for number, case in enumerate(cases[:90], start=1):
            if number == DEGRADE_AT:
                route["skill"] = {c.segment: 0.35 for c in cases}
            request = urllib.request.Request(
                url, data=json.dumps({"model": "offramp-support",
                                      "messages": case.messages}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request).read()
            guard.shadows.submit(lambda: None).result()
    finally:
        guard.shutdown()
        guard.close()
        server.stop()
    points, rollback = [], None
    for entry in ledger.read(out / "guard.jsonl"):
        if entry["event"] == "shadow":
            points.append(round(math.exp(entry["data"]["log_evidence"]), 4))
        elif entry["event"] == "rollback":
            rollback = entry["data"]["after_comparisons"]
    shutil.rmtree(out, ignore_errors=True)
    return {"evidence": points, "degrade_at": DEGRADE_AT, "rollback_at": rollback,
            "alarm": 20, "simulated": True}


def enduser_proof() -> dict:
    path = ROOT / "evidence" / "enduser-budget" / "ledger.jsonl"
    ok, head, _ = ledger.verify_chain(path)
    if not ok:
        raise SystemExit(f"enduser-budget: {head}")
    entries = list(ledger.read(path))
    calls = [e["data"] for e in entries if e["event"] == "call"]
    end = entries[-1]["data"]
    return {"served": sum(bool(c.get("ok")) for c in calls),
            "spend": round(sum(c.get("cost_usd") or 0 for c in calls), 4),
            "refusal": end["stop_reason"].split("budget_exceeded: ", 1)[-1].split(", less than")[0],
            "head": head}


def main() -> None:
    data = explorer.build_data()
    data["guard"] = guard_replay()
    data["enduser"] = enduser_proof()
    template = (ROOT / "site" / "template.html").read_text(encoding="utf-8")
    page = template.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    out = ROOT / "site" / "public"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"site/public/index.html: {len(page):,} bytes, guard rollback at comparison "
          f"{data['guard']['rollback_at']}, end user served {data['enduser']['served']} calls")


if __name__ == "__main__":
    main()
