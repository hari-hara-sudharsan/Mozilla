"""A stand-in Otari gateway for tests and the offline demo.

It speaks the same HTTP API with fictional models whose skill is fixed in a
table, so results from it say nothing about any real model. Every reply carries
an `Otari-Simulator` header. The client notices that header, the run records it,
and the report and policy are stamped SIMULATED. Nothing from here can pass as a
live run.

It never executes the code it is sent. The sandbox route just looks for a marker
that the simulated models write into their answers.
"""

from __future__ import annotations

import hashlib
import json
import re
import socketserver
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .graders import JUDGE_SYSTEM
from .workload import Case, prompt_digest

DEMO_INCUMBENT = "frontier-lab/flagship-sim"
DEMO_JUDGES = ["sim-judge-a/oss-judge", "sim-judge-b/oss-judge"]
DEMO_CANDIDATES = ["sim-tiny/oss-8b", "sim-mid/oss-32b", "sim-large/oss-120b"]

DEMO_MODELS: dict[str, dict] = {
    DEMO_INCUMBENT: {"rate": 6.0, "skill": {
        "classify": 0.97, "extract": 0.96, "math": 0.96, "code": 0.95, "rewrite": 0.9}},
    "sim-tiny/oss-8b": {"rate": 0.08, "skill": {
        "classify": 0.97, "extract": 0.72, "math": 0.45, "code": 0.3, "rewrite": 0.3}},
    "sim-mid/oss-32b": {"rate": 0.3, "flaky": 0.02, "skill": {
        "classify": 0.98, "extract": 0.96, "math": 0.82, "code": 0.7, "rewrite": 0.72}},
    "sim-large/oss-120b": {"rate": 0.9, "skill": {
        "classify": 0.98, "extract": 0.97, "math": 0.96, "code": 0.87, "rewrite": 0.86}},
    "sim-judge-a/oss-judge": {"rate": 0.2, "skill": {}},
    "sim-judge-b/oss-judge": {"rate": 0.2, "skill": {}},
}


def _unit(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def simulated_answer(case: Case, correct: bool) -> str:
    grader = case.grader
    kind = grader["type"]
    if kind == "python_tests":
        return f"```python\n# sim:{'pass' if correct else 'fail'}\n```"
    if kind == "number":
        value = grader["expected"]
        return f"The answer is {value if correct else value + 1}."
    if kind == "json_fields":
        fields = dict(grader["expected"])
        if not correct:
            fields[next(iter(fields))] = "wrong"
        return json.dumps(fields)
    if kind == "jury":
        reference = grader.get("reference", "A complete and careful answer to the request.")
        return reference if correct else reference[: max(len(reference) // 3, 1)]
    expected = grader.get("expected", "answer")
    if isinstance(expected, list):
        expected = " ".join(map(str, expected))
    return str(expected) if correct else "something else"


class Simulator(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cases: list[Case], models: dict | None = None,
                 cap_usd: float | None = None, sandbox: bool = True,
                 wallet_empty: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.oracle = {prompt_digest(case.messages): case for case in cases}
        self.models = DEMO_MODELS if models is None else models
        self.cap_usd = cap_usd
        self.sandbox = sandbox
        self.wallet_empty = wallet_empty
        self.spent = 0.0
        self.requests = 0
        self.users: set[str] = set()
        self.lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/api/v1"

    def server_bind(self) -> None:
        # HTTPServer's version does a reverse DNS lookup, which is slow on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def start(self) -> Simulator:
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: Simulator

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Otari-Simulator", "1")
        self.send_header("Otari-Request-ID", str(uuid.uuid4()))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _fail(self, status: int, code: str, message: str, headers: dict | None = None) -> None:
        self._send(status, {"error": {"code": code, "message": message}}, headers)

    def do_GET(self) -> None:
        sim = self.server
        if self.path.endswith("/models"):
            self._send(200, {"data": [
                {"id": name, "pricing": {"input_price_per_million": profile["rate"],
                                         "output_price_per_million": 3 * profile["rate"]}}
                for name, profile in sim.models.items()
            ]})
        elif self.path.endswith("/tools"):
            self._send(200, {"data": [{"id": "otari_code_execution",
                                       "available": sim.sandbox}]})
        elif self.path.endswith("/key/budget"):
            if sim.cap_usd is None:  # Hosted Otari answers 404 here.
                self._fail(404, "not_found", self.path)
                return
            self._send(200, {"budgets_enforced": True, "limits": [],
                             "remaining_usd": max(sim.cap_usd - sim.spent, 0.0)})
        else:
            self._fail(404, "not_found", self.path)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            self._chat(body)
        elif self.path.endswith("/code/execute"):
            self._execute(body)
        else:
            self._fail(404, "not_found", self.path)

    def _execute(self, body: dict) -> None:
        if not self.server.sandbox:
            self._fail(403, "code_execution_disabled", "no sandbox on this deployment")
            return
        code = body.get("code", "")
        nonce = re.search(r"print\('(offramp-ok-[0-9a-f]+)'\)", code)
        passed = "# sim:pass" in code and nonce is not None
        self._send(200, {
            "object": "code_execution.result",
            "stdout": f"{nonce.group(1)}\n" if passed else "",
            "stderr": "" if passed else "AssertionError",
            "return_code": 0 if passed else 1,
            "files": [],
        })

    def _chat(self, body: dict) -> None:
        sim = self.server
        if sim.wallet_empty:  # The shape hosted Otari really returns.
            self._send(402, {"detail": "Insufficient funds ($0.00) to serve this request.",
                             "code": "insufficient_funds"})
            return
        model = body.get("model", "")
        profile = sim.models.get(model)
        if profile is None:
            self._fail(404, "model_not_found", f"unknown model {model}")
            return
        with sim.lock:
            sim.requests += 1
            turn = sim.requests
            if sim.cap_usd is not None and sim.spent >= sim.cap_usd:
                self._fail(403, "budget_exceeded", "budget is exhausted")
                return
        if _unit(model, str(turn)) < profile.get("flaky", 0.0):
            self._fail(503, "upstream_error", "simulated outage", {"Retry-After": "0"})
            return

        if profile.get("no_temperature") and "temperature" in body:
            self._send(400, {"detail": "No model accepted the request (2 attempts).",
                             "code": "all_candidates_rejected"})
            return

        messages = body["messages"]
        if messages[0].get("content") == JUDGE_SYSTEM:
            text = self._judge(model, messages[1]["content"])
        else:
            case = sim.oracle.get(prompt_digest(messages))
            if case is None:
                text = "simulated reply"
            else:
                skill = profile["skill"].get(case.segment, 0.5)
                text = simulated_answer(case, _unit(model, case.id) < skill)
                text += profile.get("suffix", "")
        if body.get("user"):
            sim.users.add(body["user"])

        prompt_tokens = sum(len(str(m.get("content"))) for m in messages) // 4 + 1
        completion_tokens = len(text) // 4 + 1
        cost = (prompt_tokens + 3 * completion_tokens) * profile["rate"] / 1e6
        with sim.lock:
            sim.spent += cost
        self._send(200, {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": profile.get("finish_reason", "stop")}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "cost_usd": f"{cost:.6f}", "pricing_source": "simulator"},
        })

    @staticmethod
    def _judge(model: str, prompt: str) -> str:
        first = re.search(r"<answer_a>\n(.*)\n</answer_a>", prompt, re.DOTALL)
        second = re.search(r"<answer_b>\n(.*)\n</answer_b>", prompt, re.DOTALL)
        size_a = len(first.group(1)) if first else 0
        size_b = len(second.group(1)) if second else 0
        if _unit(model, prompt) < 0.05 or size_a == size_b:
            return '{"winner": "tie"}'
        return json.dumps({"winner": "A" if size_a > size_b else "B"})
