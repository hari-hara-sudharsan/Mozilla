"""The sequential replay loop."""

from __future__ import annotations

import random
import secrets
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import graders
from .graders import SandboxUnavailable
from .ledger import Ledger, sha256
from .otari import Completion, Otari, OtariError
from .stats import Arm, early_stop_log_threshold
from .workload import Case, prompt_digest

UNAVAILABLE_STATUSES = {400, 401, 403, 404, 422, 424}
MIN_CASES_TO_COMPARE_COST = 10


@dataclass
class Config:
    incumbent: str
    candidates: list[str]
    judges: list[str] = field(default_factory=list)
    decision_model: str | None = None
    budget_usd: float = 2.0
    # The sandbox route reports no price. Hosted Otari bills $0.01 a run, which
    # mozilla-ai/otari#2864 shows and our own wallet confirmed.
    sandbox_cost_usd: float = 0.010
    margin: float = 0.10
    alpha: float = 0.05
    seed: int = 7
    batch: int = 4
    workers: int = 8
    store_text: bool = False
    # An Otari end user to bill (requires a service key): a gateway-enforced budget
    # for the whole run, sandbox included, independent of Offramp's own accounting.
    end_user: str | None = None
    # An Otari guardrail profile to request on every juror call, in monitor mode.
    guardrail: str | None = None


@dataclass
class Outcome:
    """What happened to one candidate on one case. Holds no shared state."""

    candidate: str
    d: float | None = None
    q_candidate: float | None = None
    calls: list[dict] = field(default_factory=list)
    unavailable: str | None = None


@dataclass
class CaseResult:
    case: Case
    q_incumbent: float | None
    calls: list[dict]
    outcomes: list[Outcome]
    dropped: str | None = None
    ungradable: str | None = None


class BudgetStop(Exception):
    pass


class Engine:
    def __init__(self, client: Otari, cases: list[Case], digest: str, config: Config, out: Path):
        self.client = client
        self.config = config
        client.end_user = config.end_user
        self.digest = digest
        self.out = out
        self.run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        self.nonce = f"offramp-ok-{secrets.token_hex(8)}"
        self.ledger = Ledger(out / "ledger.jsonl")
        self.spent = 0.0
        self.calls_priced = 0

        rng = random.Random(config.seed)
        self.queues: dict[str, list[Case]] = defaultdict(list)
        for case in cases:
            self.queues[case.segment].append(case)
        for queue in self.queues.values():
            rng.shuffle(queue)

        self.arms = {
            (segment, candidate): Arm(config.margin)
            for segment in self.queues
            for candidate in config.candidates
        }
        self.active = set(self.arms)
        self.arm_cost: dict[tuple[str, str], float] = defaultdict(float)
        self.stop_at = early_stop_log_threshold(config.alpha, len(self.arms))

    def _call(self, role: str, model: str, case: Case, messages: list[dict], params: dict,
              candidate: str | None = None,
              guardrails: list[dict] | None = None) -> tuple[Completion | None, dict]:
        tags = {
            "offramp_run": self.run_id,
            "offramp_role": role,
            "offramp_segment": case.segment[:64],
        }
        key = sha256(f"{self.run_id}|{role}|{model}|{case.id}|{candidate}")
        record: dict = {"role": role, "model": model, "case": case.id, "segment": case.segment,
                        "candidate": candidate}
        try:
            reply = self.client.chat(model, messages, params, tags, key,
                                     user=self.config.end_user, guardrails=guardrails)
        except OtariError as error:
            if error.budget_exhausted:
                raise BudgetStop(str(error)) from error
            record.update(ok=False, status=error.status, error=str(error)[:300])
            return None, record
        record.update(
            ok=True,
            cost_usd=reply.cost_usd,
            latency_s=round(reply.latency_s, 4),
            request_id=reply.request_id,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            finish_reason=reply.finish_reason,
            text_sha256=sha256(reply.text),
        )
        if reply.guardrails:
            record["guardrails"] = reply.guardrails[:500]
        if self.config.store_text:
            record["text"] = reply.text
        return reply, record

    def _jury(self, case: Case, candidate: str, incumbent_text: str,
              candidate_text: str) -> tuple[float | None, list[dict], str | None]:
        judges = graders.independent_judges(
            self.config.judges, self.config.incumbent, candidate
        )
        if not judges and not self.config.decision_model:
            return None, [], "no judge outside the incumbent's and candidate's model families"

        steering = graders.steers_jury(candidate_text)
        if steering:
            # Decided before any juror reads it, so the attempt can't work.
            return -1.0, [{"role": "screen", "model": "offramp/jury-screen", "case": case.id,
                           "segment": case.segment, "candidate": candidate, "ok": True,
                           "cost_usd": 0.0, "steering": steering}], None

        request = "\n\n".join(str(message.get("content", "")) for message in case.messages)
        guardrails = ([{"profile": self.config.guardrail, "mode": "monitor"}]
                      if self.config.guardrail else None)
        votes: list[float] = []
        calls: list[dict] = []
        for index, judge in enumerate(judges):
            first_is_candidate = graders.candidate_goes_first(case.id, candidate, index)
            pair = (candidate_text, incumbent_text)
            first, second = pair if first_is_candidate else pair[::-1]
            reply, record = self._call(
                "judge", judge, case, graders.judge_prompt(request, first, second),
                {"temperature": 0, "max_tokens": 1024}, candidate, guardrails,
            )
            calls.append(record)
            verdict = graders.parse_verdict(reply.text) if reply else None
            if verdict:
                votes.append(graders.vote_value(verdict, first_is_candidate))

        if self.config.decision_model:
            vote, record = self._decision_vote(case, candidate, request, incumbent_text,
                                               candidate_text, len(judges))
            calls.append(record)
            if vote is not None:
                votes.append(vote)

        return (graders.jury_score(votes) if votes else None), calls, None

    def _decision_vote(self, case: Case, candidate: str, request: str, incumbent_text: str,
                       candidate_text: str, index: int) -> tuple[float | None, dict]:
        """One juror answering through Otari's typed Decisions API."""
        model = self.config.decision_model
        first_is_candidate = graders.candidate_goes_first(case.id, candidate, index)
        pair = (candidate_text, incumbent_text)
        first, second = pair if first_is_candidate else pair[::-1]
        record: dict = {"role": "decision", "model": model, "case": case.id,
                        "segment": case.segment, "candidate": candidate}
        question = {"winner": {
            "type": "choice",
            "instructions": graders.JUDGE_SYSTEM.split(" Reply with JSON")[0],
            "criteria": {"A": "Answer A is better", "B": "Answer B is better",
                         "tie": "The difference would not matter"},
        }}
        state = graders.judge_prompt(request, first, second)[1]["content"]
        try:
            reply = self.client.decide(model, state, question)
            choice = (reply.get("answers") or {}).get("winner", {}).get("choice")
        except OtariError as error:
            if error.budget_exhausted:
                raise BudgetStop(str(error)) from error
            record.update(ok=False, status=error.status, error=str(error)[:300])
            return None, record
        usage = reply.get("usage") or {}
        cost = usage.get("cost_usd")
        record.update(ok=True, cost_usd=float(cost) if cost is not None else None, choice=choice)
        return (graders.vote_value(choice, first_is_candidate)
                if choice in ("A", "B", "tie") else None), record

    def _candidate(self, case: Case, candidate: str, q_incumbent: float | None,
                   incumbent_text: str, first_call: bool) -> Outcome:
        outcome = Outcome(candidate)
        reply, record = self._call(
            "candidate", candidate, case, case.messages, case.request_params, candidate
        )
        outcome.calls.append(record)
        jury = case.grader["type"] == "jury"

        if reply is None:
            if first_call and record.get("status") in UNAVAILABLE_STATUSES:
                outcome.unavailable = record["error"]
                return outcome
            # Counts against the candidate, because a model that cannot answer in
            # production has failed the caller too.
            outcome.q_candidate = 0.0
            outcome.d = -1.0 if jury else -float(q_incumbent)
            return outcome

        if jury:
            d, calls, problem = self._jury(case, candidate, incumbent_text, reply.text)
            outcome.calls.extend(calls)
            outcome.unavailable = problem
            outcome.d = d
            return outcome

        outcome.q_candidate = graders.grade(reply.text, case.grader, self.client, self.nonce)
        outcome.d = outcome.q_candidate - float(q_incumbent)
        return outcome

    def _case(self, case: Case, candidates: list[str]) -> CaseResult:
        calls: list[dict] = []
        if case.recorded:
            text = case.recorded["text"]
            cost = case.recorded.get("cost_usd")
            calls.append({
                "role": "incumbent", "model": self.config.incumbent, "case": case.id,
                "segment": case.segment, "candidate": None, "ok": True, "recorded": True,
                "cost_usd": float(cost) if cost is not None else None,
                "latency_s": case.recorded.get("latency_s"), "text_sha256": sha256(text),
            })
        else:
            reply, record = self._call(
                "incumbent", self.config.incumbent, case, case.messages, case.request_params
            )
            calls.append(record)
            if reply is None:
                return CaseResult(case, None, calls, [], dropped=record["error"])
            if reply.finish_reason == "length":
                # A cut-off baseline isn't a fair baseline, and a candidate must
                # never earn credit from one.
                return CaseResult(case, None, calls, [],
                                  dropped="incumbent answer was cut off at the token limit")
            text = reply.text

        try:
            q_incumbent = None
            if case.grader["type"] != "jury":
                q_incumbent = graders.grade(text, case.grader, self.client, self.nonce)
            outcomes = [
                self._candidate(case, candidate, q_incumbent, text,
                                self.arms[(case.segment, candidate)].n == 0)
                for candidate in candidates
            ]
        except SandboxUnavailable as error:
            return CaseResult(case, None, calls, [], ungradable=str(error))
        return CaseResult(case, q_incumbent, calls, outcomes)

    def _log_calls(self, calls: list[dict]) -> None:
        for record in calls:
            self.ledger.append("call", **record)
            cost = record.get("cost_usd")
            if cost is not None and not record.get("recorded"):
                self.spent += cost
                self.calls_priced += 1
                if record.get("candidate"):
                    self.arm_cost[(record["segment"], record["candidate"])] += cost

    def _set_status(self, arm_key: tuple[str, str], status: str, reason: str) -> None:
        if arm_key not in self.active:
            return
        self.active.discard(arm_key)
        segment, candidate = arm_key
        self.ledger.append("arm_status", segment=segment, candidate=candidate, status=status,
                           reason=reason, at_n=self.arms[arm_key].n)

    def _apply(self, result: CaseResult) -> None:
        self._log_calls(result.calls)
        segment = result.case.segment
        if result.ungradable:
            for arm_key in [key for key in self.active if key[0] == segment]:
                self._set_status(arm_key, "unavailable",
                                 f"cannot grade this segment: {result.ungradable}"[:300])
            return
        if result.dropped:
            self.ledger.append("drop", case=result.case.id, segment=segment,
                               reason=result.dropped[:300])
            return
        for outcome in result.outcomes:
            self._log_calls(outcome.calls)
            arm_key = (segment, outcome.candidate)
            if outcome.unavailable:
                self._set_status(arm_key, "unavailable", outcome.unavailable[:300])
            elif outcome.d is None:
                self.ledger.append("drop", case=result.case.id, segment=segment,
                                   candidate=outcome.candidate, reason="no usable jury vote")
            elif arm_key in self.active:
                arm = self.arms[arm_key]
                arm.update(outcome.d)
                self.ledger.append(
                    "score", segment=segment, candidate=outcome.candidate, case=result.case.id,
                    weight=result.case.weight, d=outcome.d, q_incumbent=result.q_incumbent,
                    q_candidate=outcome.q_candidate,
                )
                self._check_stop(arm_key)
        self._drop_dominated(segment)

    def _check_stop(self, arm_key: tuple[str, str]) -> None:
        arm = self.arms[arm_key]
        if arm.log_e_for >= self.stop_at:
            self._set_status(arm_key, "proven", "evidence crossed the early-stop threshold")
        elif arm.log_e_against >= self.stop_at:
            self._set_status(arm_key, "rejected", "evidence crossed the early-stop threshold")

    def _mean_cost(self, arm_key: tuple[str, str]) -> float | None:
        arm = self.arms[arm_key]
        return self.arm_cost[arm_key] / arm.n if arm.n >= MIN_CASES_TO_COMPARE_COST else None

    def _drop_dominated(self, segment: str) -> None:
        """Stop paying for a model once a cheaper one has already been proven."""
        proven = [
            cost for key in self.arms
            if key[0] == segment and key not in self.active
            and self.arms[key].log_e_for >= self.stop_at
            and (cost := self._mean_cost(key)) is not None
        ]
        if not proven:
            return
        for key in [k for k in self.active if k[0] == segment]:
            cost = self._mean_cost(key)
            if cost is not None and cost > min(proven):
                self._set_status(key, "dominated", "a cheaper candidate was already proven")

    def _round_plan(self) -> list[tuple[Case, list[str]]]:
        plan = []
        for segment, queue in self.queues.items():
            candidates = [c for c in self.config.candidates if (segment, c) in self.active]
            if candidates:
                for _ in range(min(self.config.batch, len(queue))):
                    plan.append((queue.pop(), candidates))
        return plan

    def _can_afford(self, plan: list[tuple[Case, list[str]]], gateway_start: float | None) -> bool:
        calls = sum(len(candidates) + 1 for _, candidates in plan)
        average = self.spent / self.calls_priced if self.calls_priced else 0.0
        sandbox = self.client.sandbox_runs + sum(
            len(names) + 1 for case, names in plan if case.grader["type"] == "python_tests")
        committed = self.spent + sandbox * self.config.sandbox_cost_usd
        if committed + 1.5 * average * calls > self.config.budget_usd:
            return False
        if gateway_start is None:
            return True
        # The gateway also counts what this client cannot see, such as sandbox runs.
        now = self.client.remaining_usd()
        return now is None or gateway_start - now < self.config.budget_usd

    def run(self) -> str:
        gateway_start = self.client.remaining_usd()
        if gateway_start is not None and gateway_start < self.config.budget_usd:
            print(f"warning: this key can spend ${gateway_start:.2f}, "
                  f"which is under the ${self.config.budget_usd:.2f} budget")
        config = asdict(self.config)
        self.ledger.append(
            "run_start", run_id=self.run_id, config=config, workload_sha256=self.digest,
            simulated=self.client.simulated, gateway_remaining_usd=gateway_start,
            base_url=self.client.base_url, n_arms=len(self.arms),
            segments={segment: {
                "cases": len(queue),
                "weight": sum(case.weight for case in queue),
                "order": [case.id for case in reversed(queue)],
                "prompts": [prompt_digest(case.messages) for case in reversed(queue)],
            } for segment, queue in self.queues.items()},
        )

        stop_reason = "workload exhausted"
        try:
            with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
                while self.active:
                    plan = self._round_plan()
                    if not plan:
                        break
                    if not self._can_afford(plan, gateway_start):
                        stop_reason = "budget reached"
                        break
                    jobs = [pool.submit(self._case, case, names) for case, names in plan]
                    for job in jobs:
                        self._apply(job.result())
                    print(self._progress(), flush=True)
                else:
                    stop_reason = "every arm decided"
        except BudgetStop as error:
            stop_reason = f"gateway refused for budget: {error}"

        for arm_key in sorted(self.active):
            self._set_status(arm_key, "open", stop_reason)
        self.ledger.append(
            "run_end", stop_reason=stop_reason, reported_spend_usd=round(self.spent, 6),
            models_refusing_temperature=sorted(self.client.refuses_temperature),
            sandbox_runs=self.client.sandbox_runs,
            gateway_remaining_usd=self.client.remaining_usd(),
        )
        self.ledger.close()
        return self.ledger.head

    def _progress(self) -> str:
        decided = len(self.arms) - len(self.active)
        scored = sum(arm.n for arm in self.arms.values())
        return (f"  {scored:5d} comparisons | {decided}/{len(self.arms)} arms decided | "
                f"${self.spent:.4f} of ${self.config.budget_usd:.2f}")
