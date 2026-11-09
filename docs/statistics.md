# The statistics

## The problem

You want to check results as they arrive and stop paying once the answer is clear.
A fixed-sample test forbids that. Check it after every batch and its real false
positive rate climbs far above the nominal 5%. Offramp uses tests built for
continuous monitoring.

## Paired differences

For each case, both models answer and a grader scores them. That gives a paired
difference `d` in `[-1, 1]`, where positive favours the candidate. For checkable
graders, `d = q_candidate - q_incumbent`. For a jury, `d = -1` when every juror
prefers the incumbent and `0` otherwise, so jury evidence can only count against a
candidate. A tie-heavy `d` still lets a candidate pass, so the jury's margin reads as
"how often do independent jurors unanimously prefer the incumbent".

Pairing matters because it removes case difficulty from the comparison. A hard case
that defeats both models contributes zero, not noise.

## Non-inferiority by betting

With margin `m`, we want to reject `H0: E[d] <= -m`. Let `x_t = d_t + m`. Under
`H0`, `E[x_t | past] <= 0`, so this wealth process is a non-negative
supermartingale for any stake `lam_t >= 0` chosen from past data alone:

    W_t = prod_{i <= t} (1 + lam_i * x_i)

By Ville's inequality, `P(sup_t W_t >= 1/a) <= a`. The bound covers every time at
once, so stopping rules can't break it. `W_t` is an e-process, and its value is the
evidence plotted in the report.

A second process runs in the opposite direction to prove a candidate is worse than
the margin allows, so failing models stop early too.

Stakes use aGRAPA (Waudby-Smith & Ramdas, 2024), capped at 90% of the largest stake
that keeps wealth non-negative under the worst case `d = -1`.

## Many arms at once

An arm stops alone only when `W >= K / alpha`, where `K` is the number of arms. That
is Bonferroni, which is conservative and always valid. After the run, final verdicts
apply e-BH (Wang & Ramdas, 2022): reject the `k` largest e-values, for the largest
`k` with `e_(k) >= K / (k * alpha)`. This controls the false discovery rate under
arbitrary dependence, which matters because arms sharing a case are dependent. A
stopped e-process is still an e-value, so early stops are compatible with it. An arm
can therefore be proven at the end without having crossed its early-stop line.

## Assumptions

- Cases arrive in random order. The engine shuffles each segment with a recorded seed
  and writes the order to the ledger.
- The claim concerns the population your sample represents. Sampling from a finite
  file and shifting traffic are both outside the formal guarantee.
- An arm is dropped once a cheaper candidate in its segment has been proven. That
  only ends data collection, which is a stopping rule, so validity is unaffected.

## Sample size

The test assumes nothing about `d` except its bounds, so if every case ties, wealth
grows by about `1 + 0.9 * m / (1 - m)` per case. At a margin of 0.10 with 15 arms,
that means roughly 60 to 70 cases per segment to stop alone, and fewer under e-BH when
several arms succeed. A tighter margin or fewer cases will often give `undecided`,
which is the honest answer.

## Tests

`tests/test_offramp.py` simulates continuous peeking at the boundary of the null and
checks the crossing rate stays below alpha. It also checks power on a model that
matches the incumbent, and the e-BH thresholds.

## References

- Waudby-Smith, I. and Ramdas, A. (2024). Estimating means of bounded random variables
  by betting. *JRSS Series B*.
- Wang, R. and Ramdas, A. (2022). False discovery rate control with e-values. *JRSS
  Series B*.
- Ville, J. (1939). *Étude critique de la notion de collectif*.
