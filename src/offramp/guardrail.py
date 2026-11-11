"""Offramp's jury screen as an Otari guardrail service.

Otari can send a guardrail check to a service the request names (`url` on a
`guardrails` entry). It calls `POST <url>/validate` with the latest user message
and, in `block` mode, refuses the request with 403 `guardrail_violation` before
the model is ever called. This server answers that call with Offramp's
deterministic jury-steering screen, so Otari itself stops a candidate answer
that addresses its judges from ever reaching a juror.

    offramp guardrail-serve --port 8790

The contract is in Otari's docs, "What a guardrails service answers":
    POST /validate  {"profile": "...", "input_text": "...", "validate_kwargs": {...}}
    -> {"profile": "...", "result": {"valid": false, "explanation": "...", "score": 1.0}}
"""

from __future__ import annotations

import hmac
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import graders

PROFILE = "jury-steering"
OPEN, CLOSE = "<answer_a>\n", "\n</answer_b>"
SEPARATOR = "\n</answer_a>\n\n<answer_b>\n"


def answers_in(text: str) -> str:
    """The text of the two answers in a juror prompt, and nothing the user asked.

    The request is the caller's own task, so it is never screened. A candidate
    that injects its own answer tags keeps them in what is returned, and the
    screen flags them.
    """
    start, end = text.find(OPEN), text.rfind(CLOSE)
    if start < 0 or end <= start:
        return text
    return text[start + len(OPEN):end].replace(SEPARATOR, "\n", 1)


def verdict(input_text: str) -> dict:
    steering = graders.steers_jury(answers_in(input_text))
    if steering:
        return {"valid": False, "score": 1.0,
                "explanation": f"An answer addresses its judges: {steering!r}"}
    return {"valid": True, "score": 0.0, "explanation": None}


class GuardrailServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 8790, token: str | None = None) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.token = token
        self.lock = threading.Lock()
        self.checked = 0
        self.flagged = 0

    def server_bind(self) -> None:
        # HTTPServer's version does a reverse DNS lookup, which is slow on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(BaseHTTPRequestHandler):
    server: GuardrailServer

    def log_message(self, *args: object) -> None:
        pass

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        if not self.server.token:
            return True
        sent = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(sent.encode(), self.server.token.encode())

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/profiles"):
            self._reply(200, [{"name": PROFILE, "guardrail_name": "offramp_jury_screen"}])
        elif self.path.rstrip("/").endswith("/health"):
            with self.server.lock:
                self._reply(200, {"checked": self.server.checked, "flagged": self.server.flagged})
        else:
            self._reply(404, {"detail": "try POST /validate"})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/validate"):
            self._reply(404, {"detail": "try POST /validate"})
            return
        if not self._authorised():
            self._reply(401, {"detail": "missing or wrong bearer credential"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            self._reply(400, {"detail": "body is not JSON"})
            return
        result = verdict(str(body.get("input_text", "")))
        with self.server.lock:
            self.server.checked += 1
            self.server.flagged += result["valid"] is False
        self._reply(200, {"profile": body.get("profile", PROFILE), "result": result})
