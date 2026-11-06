"""A local recording proxy, so the evidence comes from your real traffic.

Point an OpenAI client at it. Each request goes to Otari unchanged and the
response comes straight back. The pair is appended to a workload file, which
later replays at no cost for the incumbent side. The file holds full prompts and
answers, so treat it as sensitive.
"""

from __future__ import annotations

import json
import socketserver
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .otari import Otari, OtariError

PARAMS_KEPT = ("temperature", "max_tokens", "max_completion_tokens", "top_p", "response_format")


class CaptureProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, client: Otari, out: Path, port: int = 8787) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.client = client
        self.out = out
        self.lock = threading.Lock()
        self.count = 0

    def server_bind(self) -> None:
        # HTTPServer's version does a reverse DNS lookup, which is slow on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def record(self, row: dict) -> None:
        with self.lock, self.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.count += 1


class _Handler(BaseHTTPRequestHandler):
    server: CaptureProxy

    def log_message(self, *args: object) -> None:
        pass

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        if not self.path.endswith("/chat/completions"):
            self._reply(404, {"error": {"message": "only chat completions are captured"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("stream"):
            self._reply(400, {"error": {"message": "capture does not support stream: true"}})
            return
        try:
            reply, _ = self.server.client.request("POST", "/chat/completions", body)
        except OtariError as error:
            self._reply(error.status or 502, {"error": {"code": error.code,
                                                        "message": error.message}})
            return

        choices = reply.get("choices") or []
        if not choices:
            self._reply(200, reply)
            return
        usage = reply.get("usage") or {}
        metadata = body.get("metadata") or {}
        self.server.record({
            "id": f"cap-{uuid.uuid4().hex[:12]}",
            "segment": self.headers.get("X-Offramp-Segment") or metadata.get("segment", "default"),
            "messages": body["messages"],
            "params": {key: body[key] for key in PARAMS_KEPT if key in body},
            "grader": {"type": "jury"},
            "recorded": {
                "text": choices[0].get("message", {}).get("content") or "",
                "model": reply.get("model", body.get("model")),
                "cost_usd": usage.get("cost_usd"),
            },
        })
        self._reply(200, reply)
