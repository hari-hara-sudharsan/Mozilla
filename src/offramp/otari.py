"""Small stdlib client for the Otari gateway."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.otari.ai/api/v1"
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_CONFLICT_WAITS = 40


class OtariError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def budget_exhausted(self) -> bool:
        return self.code in ("budget_exceeded", "insufficient_funds") or self.status == 402


@dataclass
class Completion:
    text: str
    cost_usd: float | None
    latency_s: float
    request_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = ""


def load_env(path: Path = Path(".env")) -> None:
    """Read KEY=value lines without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


class Otari:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 180.0,
        max_retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("OTARI_API_KEY is not set. Create a key at https://otari.ai.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.simulated = False
        self.refuses_temperature: set[str] = set()
        self.sandbox_runs = 0
        self._lock = threading.Lock()

    def _once(self, method: str, path: str, body: dict | None, headers: dict) -> tuple[dict, dict]:
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "offramp/0.1",
                **headers,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            reply_headers = {key.lower(): value for key, value in response.headers.items()}
        if reply_headers.get("otari-simulator") == "1":
            self.simulated = True
        return (json.loads(raw) if raw else {}), reply_headers

    def request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[dict, dict]:
        attempt = conflicts = 0
        while True:
            try:
                return self._once(method, path, body, headers or {})
            except urllib.error.HTTPError as error:
                if error.headers and error.headers.get("Otari-Simulator") == "1":
                    self.simulated = True
                failure = _parse_error(error)
                if error.code == 421:
                    failure.message += " Set OTARI_BASE_URL to the host this error names."
                wait = _retry_after(error)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                failure = OtariError(0, "network_error", str(error))
                wait = None
            if failure.status == 409 and conflicts < MAX_CONFLICT_WAITS:
                # The original request is still running, so wait for its result.
                # It isn't a failure yet, and it shouldn't use up the retries.
                conflicts += 1
                time.sleep(wait if wait is not None else self.backoff * 5)
                continue
            if failure.status and failure.status not in RETRYABLE:
                raise failure
            if attempt >= self.max_retries:
                raise failure
            time.sleep(wait if wait is not None else self.backoff * 2**attempt)
            attempt += 1

    def chat(
        self,
        model: str,
        messages: list[dict],
        params: dict | None = None,
        tags: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Completion:
        body = {"model": model, "messages": messages, **(params or {})}
        if tags:
            body["metadata"] = tags
        if model in self.refuses_temperature:
            body.pop("temperature", None)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        started = time.perf_counter()
        try:
            reply, reply_headers = self.request("POST", "/chat/completions", body, headers)
        except OtariError as error:
            # Some reasoning models refuse a sampling temperature outright.
            if error.status != 400 or "temperature" not in body:
                raise
            body.pop("temperature")
            reply, reply_headers = self.request("POST", "/chat/completions", body, {})
            self.refuses_temperature.add(model)
        latency = time.perf_counter() - started
        choices = reply.get("choices") or []
        if not choices:
            raise OtariError(502, "no_choices", f"{model} returned no choices")
        usage = reply.get("usage") or {}
        cost = usage.get("cost_usd")
        return Completion(
            text=_message_text(choices[0].get("message") or {}),
            cost_usd=float(cost) if cost is not None else None,
            latency_s=latency,
            request_id=reply_headers.get("otari-request-id", ""),
            model=reply.get("model", model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choices[0].get("finish_reason") or ""),
        )

    def execute(self, code: str, timeout_seconds: int = 20) -> dict:
        """Run code in Otari's sandbox. Model-written code never runs locally."""
        reply, _ = self.request(
            "POST", "/code/execute", {"code": code, "timeout_seconds": timeout_seconds}
        )
        with self._lock:
            self.sandbox_runs += 1
        return reply

    def decide(self, model: str, state: str, questions: dict) -> dict:
        reply, _ = self.request(
            "POST", "/decisions", {"model": model, "state": state, "questions": questions}
        )
        return reply

    def models(self) -> list[dict]:
        reply, _ = self.request("GET", "/models")
        return reply.get("data", []) if isinstance(reply, dict) else reply

    def tools(self) -> dict:
        return self.request("GET", "/tools")[0]

    def key_budget(self) -> dict:
        return self.request("GET", "/key/budget")[0]

    def remaining_usd(self) -> float | None:
        """The gateway's own view of what this key may spend, if it has a cap."""
        try:
            value = self.key_budget().get("remaining_usd")
        except OtariError:
            return None
        return float(value) if value is not None else None


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None


def _parse_error(error: urllib.error.HTTPError) -> OtariError:
    raw = error.read().decode("utf-8", "replace")
    try:
        body = json.loads(raw)
    except ValueError:
        return OtariError(error.code, "http_error", raw[:300])
    detail = body.get("error") or body.get("detail") or body
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("type") or body.get("code") or "http_error")
        return OtariError(error.code, code, str(detail.get("message") or detail)[:300])
    return OtariError(error.code, str(body.get("code") or "http_error"), str(detail)[:300])
