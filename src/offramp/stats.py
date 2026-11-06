"""Anytime-valid non-inferiority testing.

The question for each (segment, candidate) arm is whether the candidate is worse
than the incumbent by more than a margin. Each case yields a paired difference
d in [-1, 1], where positive favours the candidate.

An ordinary test is invalid here, because we want to look after every batch and
stop paying as soon as the answer is known. Peeking inflates a fixed-sample
test's error rate. So each arm instead runs two test supermartingales, or
e-processes, built by betting (Waudby-Smith & Ramdas, 2024):

    for   H0: E[d] <= -margin      W_t = prod(1 + lam_i * (d_i + margin))
    against H0': E[d] >= -margin   V_t = prod(1 - lam_i * (d_i + margin))

Under its null, each process is a non-negative supermartingale, so Ville's
inequality gives P(sup_t W_t >= 1/a) <= a. That bound holds at every time, so
stopping whenever we like costs no validity. Each lam_i uses only earlier data.

The final verdicts across arms use e-BH (Wang & Ramdas, 2022), which controls the
false discovery rate under arbitrary dependence, and arms sharing a case are
dependent. A stopped e-process is still an e-value, so early stops are allowed.

The guarantee assumes cases arrive in random order, so the engine shuffles them
with a recorded seed. It covers the workload the sample came from, and it does
not cover traffic that later drifts away from that workload.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Fraction of the largest safe bet ever staked. Below 1 so one bad case cannot
# zero the wealth, which could never recover.
BET_CAP = 0.9


@dataclass
class _Bettor:
    """One betting e-process on x_t, testing H0: E[x] <= 0, with x >= -lower."""

    lower: float
    prior_mean: float
    prior_var: float = 0.25
    n: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    log_e: float = 0.0

    def stake(self) -> float:
        """aGRAPA stake, from past data only, so it stays predictable."""
        count = self.n + 1
        mean = (self.prior_mean + self.total) / count
        second = (self.prior_var + self.prior_mean**2 + self.total_sq) / count
        var = max(second - mean**2, 1e-6)
        raw = mean / (var + mean**2)
        return min(max(raw, 0.0), BET_CAP / self.lower)

    def update(self, x: float) -> None:
        self.log_e += math.log1p(self.stake() * x)
        self.n += 1
        self.total += x
        self.total_sq += x * x


@dataclass
class Arm:
    """Sequential evidence for one candidate on one segment."""

    margin: float
    trail: list[tuple[int, float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0 < self.margin < 1:
            raise ValueError("margin must be between 0 and 1")
        self._for = _Bettor(lower=1 - self.margin, prior_mean=self.margin)
        self._against = _Bettor(lower=1 + self.margin, prior_mean=-self.margin)
        self.sum_d = 0.0

    def update(self, d: float) -> None:
        if not -1.0 <= d <= 1.0:
            raise ValueError(f"paired difference {d} is outside [-1, 1]")
        x = d + self.margin
        self._for.update(x)
        self._against.update(-x)
        self.sum_d += d
        self.trail.append((self.n, self.log_e_for, self.log_e_against))

    @property
    def n(self) -> int:
        return self._for.n

    @property
    def mean_d(self) -> float:
        return self.sum_d / self.n if self.n else 0.0

    @property
    def log_e_for(self) -> float:
        return self._for.log_e

    @property
    def log_e_against(self) -> float:
        return self._against.log_e


def early_stop_log_threshold(alpha: float, n_arms: int) -> float:
    """Evidence at which one arm may stop by itself (Bonferroni, conservative)."""
    return math.log(max(n_arms, 1) / alpha)


def e_bh(log_e: list[float], alpha: float) -> list[bool]:
    """Benjamini-Hochberg for e-values: reject the k largest, for the largest k
    with e_(k) >= K / (k * alpha)."""
    total = len(log_e)
    order = sorted(range(total), key=lambda i: log_e[i], reverse=True)
    accepted = 0
    for rank, index in enumerate(order, start=1):
        if log_e[index] >= math.log(total / (rank * alpha)):
            accepted = rank
    rejected = [False] * total
    for index in order[:accepted]:
        rejected[index] = True
    return rejected
