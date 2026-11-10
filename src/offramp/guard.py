"""Offramp Guard: keep proving a route in production, and roll it back if it slips.

Run the guard as a local proxy in front of your app. Requests for an
`offramp-<segment>` policy go to Otari unchanged. A sample of them is also sent
to the incumbent in the background, and independent jurors compare the two
answers, so the user never waits for any of this.

Each comparison feeds the same betting e-process as the offline test, run in the
other direction. Its null is that the route is still within the margin. The
process is a supermartingale under that null, so Ville's inequality bounds the
chance of ever raising a false alarm by alpha, however long the guard runs. When
the evidence crosses 1/alpha, the guard sends that segment back to the incumbent
and records why in a hash-chained ledger.
"""

from __future__ import annotations

import json
import math
import random
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import graders
from .ledger import Ledger, sha256
from .otari import Otari, OtariError
from .stats import Arm

PREFIX = "offramp-"


class Guard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, client: Otari, incumbent: str, judges: list[str], out: Path,
                 port: int = 8788, sample: float = 0.2, margin: float = 0.10,
                 alpha: float = 0.05, seed: int = 7, quiet: bool = False) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        if not judges:
            raise ValueError("the guard needs at least one juror (--judges)")
        if not 0 < sample <= 1:
            raise ValueError("--sample must be between 0 and 1")
        self.client = client
        self.incumbent = incumbent
        self.judges = judges
        self.sample = sample
        self.margin = margin
        self.alpha = alpha
        self.threshold = math.log(1 / alpha)
        self.quiet = quiet
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.arms: dict[str, Arm] = {}
        self.served: dict[str, int] = {}
        self.tripped: dict[str, dict] = {}
        self.shadows = ThreadPoolExecutor(max_workers=4)
        self.closed = False
        self.ledger = Ledger(out / "guard.jsonl")
        self.ledger.append("guard_start", incumbent=incumbent, judges=judges, sample=sample,
                           margin=margin, alpha=alpha, seed=seed, base_url=client.base_url,
                           simulated=client.simulated)

    def server_bind(self) -> None:
        # HTTPServer's version does a reverse DNS lookup, which is slow on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def say(self, message: str) -> None:
        if not self.quiet:
            print(message, flush=True)

    def status(self) -> dict:
        with self.lock:
            return {
                "incumbent": self.incumbent, "margin": self.margin, "alpha": self.alpha,
                "alarm_at_evidence": round(math.exp(self.threshold), 2),
                "segments": {
                    name: {
                        "requests": self.served.get(name, 0),
                        "shadow_comparisons": arm.n,
                        "evidence_of_regression": round(math.exp(arm.log_e_against), 4),
                        "unanimous_incumbent_preference": round(-arm.mean_d, 4),
                        "rolled_back": name in self.tripped,
                    }
                    for name, arm in sorted(self.arms.items())
                },
                "ledger_head": self.ledger.head,
            }

    def route(self, model: str) -> tuple[str | None, str]:
        """Return the segment and the model to actually request."""
        if not model.startswith(PREFIX):
            return None, model
        segment = model[len(PREFIX):]
        with self.lock:
            self.served[segment] = self.served.get(segment, 0) + 1
            self.arms.setdefault(segment, Arm(self.margin))
            if segment in self.tripped:
                return segment, self.incumbent
        return segment, model

    def should_shadow(self) -> bool:
        with self.lock:
            return self.rng.random() < self.sample

    def shadow(self, segment: str, body: dict, answer: str, served_model: str) -> None:
        """Compare one routed answer with the incumbent's, off the request path."""
        try:
            params = {k: v for k, v in body.items() if k not in ("model", "messages", "stream")}
            params.pop("metadata", None)
            incumbent = self.client.chat(self.incumbent, body["messages"], params,
                                         {"offramp_role": "guard_shadow",
                                          "offramp_segment": segment[:64]})
            request = "\n\n".join(str(m.get("content", "")) for m in body["messages"])
            judges = graders.independent_judges(self.judges, self.incumbent, served_model)
            votes = []
            key = sha256(request)[:16]
            for index, judge in enumerate(judges):
                first_is_candidate = graders.candidate_goes_first(key, served_model, index)
                pair = (answer, incumbent.text)
                first, second = pair if first_is_candidate else pair[::-1]
                reply = self.client.chat(judge, graders.judge_prompt(request, first, second),
                                         {"temperature": 0, "max_tokens": 1024},
                                         {"offramp_role": "guard_juror"})
                verdict = graders.parse_verdict(reply.text)
                if verdict:
                    votes.append(graders.vote_value(verdict, first_is_candidate))
        except OtariError as error:
            with self.lock:
                self.ledger.append("shadow_failed", segment=segment, error=str(error)[:300])
            return
        if not votes:
            return
        d = graders.jury_score(votes)
        with self.lock:
            if segment in self.tripped:
                return
            arm = self.arms[segment]
            arm.update(d)
            self.ledger.append("shadow", segment=segment, served_model=served_model, d=d,
                               votes=votes, prompt_sha256=sha256(request),
                               log_evidence=arm.log_e_against)
            evidence = math.exp(arm.log_e_against)
            if arm.log_e_against >= self.threshold:
                self.tripped[segment] = {"after": arm.n, "evidence": evidence}
                self.ledger.append("rollback", segment=segment, after_comparisons=arm.n,
                                   evidence=evidence, to=self.incumbent)
                self.say(f"ROLLBACK {PREFIX}{segment}: evidence {evidence:.1f} crossed "
                         f"{1 / self.alpha:.0f} after {arm.n} comparisons. "
                         f"Now serving {self.incumbent}.")
            else:
                self.say(f"  {PREFIX}{segment}: comparison {arm.n}, vote {d:+.0f}, "
                         f"evidence of regression {evidence:.2f} (alarm at {1 / self.alpha:.0f})")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.shadows.shutdown(wait=True)
        with self.lock:
            self.ledger.append("guard_end", status=self.status_unlocked())
            self.ledger.close()
        self.server_close()

    def status_unlocked(self) -> dict:
        return {name: {"comparisons": arm.n, "log_evidence": arm.log_e_against,
                       "rolled_back": name in self.tripped}
                for name, arm in self.arms.items()}


class _Handler(BaseHTTPRequestHandler):
    server: Guard

    def log_message(self, *args: object) -> None:
        pass

    def _reply(self, status: int, body: dict, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/offramp/status"):
            self._reply(200, self.server.status())
        else:
            self._reply(404, {"error": {"message": "try GET /offramp/status"}})

    def do_POST(self) -> None:
        guard = self.server
        if not self.path.endswith("/chat/completions"):
            self._reply(404, {"error": {"message": "only chat completions pass through the guard"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("stream"):
            self._reply(400, {"error": {"message": "the guard does not support stream: true"}})
            return
        segment, model = guard.route(str(body.get("model", "")))
        rolled_back = segment is not None and model == guard.incumbent
        forwarded = {**body, "model": model}
        try:
            reply, headers = guard.client.request("POST", "/chat/completions", forwarded)
        except OtariError as error:
            self._reply(error.status or 502, {"error": {"code": error.code,
                                                        "message": error.message}})
            return
        state = "rolled-back" if rolled_back else ("watching" if segment else "pass-through")
        self._reply(200, reply, {"X-Offramp-Guard": state})

        choices = reply.get("choices") or []
        if segment and not rolled_back and choices and guard.should_shadow():
            answer = (choices[0].get("message") or {}).get("content") or ""
            served = headers.get("otari-model") or model
            guard.shadows.submit(guard.shadow, segment, body, answer, served)
