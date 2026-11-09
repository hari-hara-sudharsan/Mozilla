# Offramp

**Prove you can leave your frontier model.**

Everyone would like to move traffic to open-weight models. Almost nobody does,
because nobody can show it's safe. A vibe check on ten prompts won't convince the
person who owns the production incident, and a full evaluation costs real money
and engineering time.

Offramp replays your real LLM traffic on open-weight models through
[Otari](https://otari.ai). It stops paying the moment the statistics are settled,
then hands you the Otari routing policy the evidence earned, with receipts anyone
can recheck.

```
$ offramp run --workload traffic.jsonl --incumbent anthropic:claude-sonnet-4-6 \
    --candidates deepseek/deepseek-v4-pro-0813,... --judges ... --budget 2 --out runs/q4
```

Try it now, with no key and no cost:

```bash
uv run offramp demo --out runs/demo
```

The demo uses fictional models on a local simulator, and its report says so on
every page. It shows how Offramp works, and it says nothing about any real model.

## Results from a live run

Run through hosted Otari on November 7, 2026. The incumbent was `xai/grok-4.7`.
The candidates were `openai/gpt-oss-120b`, `deepseek/deepseek-v4-flash` and
`z-ai/glm-4.7-flash`, and the jurors were `google/gemma-3-27b-it` and
`meta/llama-3.3-70b-instruct`. The margin was 10 points, with a 5% false discovery
rate. The workload is synthetic, from `examples/make_workload.py`, not production
traffic.

| Segment | Share | Route | Evidence |
| --- | ---: | --- | --- |
| Classification | 14.5% | `z-ai/glm-4.7-flash` | All three candidates proven |
| Extraction | 20.4% | `deepseek/deepseek-v4-flash` | All three proven |
| Math | 20.4% | `deepseek/deepseek-v4-flash` | All three proven |
| Code, graded in Otari's sandbox | 9.8% | `deepseek/deepseek-v4-flash` | All three proven |
| Writing, by jury | 10.2% | `deepseek/deepseek-v4-flash` | DeepSeek proven, GLM undecided |
| Hard extraction | 12.3% | **Stay on Grok** | DeepSeek and GLM rejected, 0 of 10 each. gpt-oss undecided at 95.8%. |
| Hard math | 12.3% | **Stay on Grok** | GLM rejected at 26.7%. gpt-oss (47/47) and DeepSeek undecided. |

**75.5% of requests can move, but that cuts total cost by only 22%**, from $6.26 to
$4.88 per 1,000 requests. The hard segments carry most of the cost, at $11.83 and
$27.82 per 1,000 on Grok, and they are exactly what the cheap models couldn't prove
they can do. Only per-segment evidence shows that.

The evidence is in [`evidence/`](evidence/). It holds each run's ledger, report,
results and workload, the combined [`policy.yaml`](evidence/policy.yaml), and
[`combined.json`](evidence/combined.json). To recheck it:

```bash
uv run offramp verify evidence/live-2 --head 9a350b4471ef77a95d423ed3923d0a7a41d387e34c4821c67eff5851a021391b
uv run offramp verify evidence/live-4-hard --head b443d82811a1636ed70c441f0af9bcf6da1563f67a9a03088b9190482a078cc2
```

### Every live run, including the ones that failed

| Run | Model cost | Outcome |
| --- | ---: | --- |
| Pilot, 20 cases | $0.04 | Found a code test that wrongly failed every model, a gateway timeout scored as a model failure, and Grok refusing `temperature`. All fixed. |
| Code check, 96 cases | $0.24 | Validated every code test: none was failed by every model. |
| `live-1` | $0.75 | Showed every model passing. An audit found the tasks too easy and the jury favouring longer answers. Superseded. |
| `live-2` | $3.15 | **Run of record** for the five easy segments. Its hard-math result is invalid, because Grok was cut off by the token limit on 18 answers. |
| `live-3` | $2.61 | Stopped twice, once when the machine ran low on memory and once when the wallet ran out. No verdicts. |
| `live-4-hard` | $2.00 | **Run of record** for the two hard segments, with a 16,000-token limit. Stopped early by low memory, but its chain verifies and every verdict in it holds, since an e-process may be stopped at any time. |

Sandbox runs report no price. About 1,250 of them appear to have cost about $13.50
on top of the table above, inferred from the wallet, not confirmed. That is why
`--budget` now counts them.

The two runs of record each control the false discovery rate on their own. The
combined claim spans two runs and two accounts, and has no joint guarantee.

## What makes it different

**It may look at the results after every batch, and that costs no validity.**
Stopping an ordinary A/B test as soon as it looks good inflates its false positive
rate. Offramp uses anytime-valid e-processes, so it can stop an arm the moment the
answer is known, whichever way that goes. Across arms, e-BH controls the false
discovery rate even though arms sharing a case are dependent. Details are in
[docs/statistics.md](docs/statistics.md).

**It answers the question you actually have.** The question is not "which model
is best?" but "is this one more than N points worse than what I run today?" That
is a non-inferiority test. Each segment of your traffic gets its own answer,
because a model that handles classification well may fail at code.

**Absence of proof means you stay put.** A segment moves only when its evidence
crosses the line. Too little data, and the verdict is `undecided` and the policy
keeps the incumbent.

**Deterministic checks come first.** Labels, numbers and JSON fields are checked
by code. Generated code runs against its tests in Otari's sandbox, and never on
your machine. Only open-ended answers go to a jury.

**The jury can hold a model back, but never earn it a route.** Jurors must come
from a different model family to both the candidate and the incumbent, or Offramp
refuses to grade. They never learn which answer is which, and a pair of jurors sees
opposite orders. A candidate is marked down only when every juror prefers the
incumbent, and a jury win never counts in its favour. Live runs showed why: jurors
favoured longer answers, so preferring a candidate is not evidence it is better.

**The result is a deployable artifact.** You get one Otari routing policy per
segment, with the incumbent as the last fallback, so an open-weight failure
degrades to today's behaviour instead of dropping the request.

**Conclusions can be rechecked.** Every call and score goes into a hash-chained
ledger. `offramp verify` checks the chain and recomputes every verdict, the
results and the policy from the raw scores. Publish the ledger head and the run
is pinned to it.

## How it uses Otari

| Otari feature | What Offramp does with it |
| --- | --- |
| Open-weight models | They are the point of the tool. Candidates and jurors all go through one endpoint. |
| Multiple models, distinct roles | Incumbent, candidates and a cross-family jury each play a separate role. |
| Routing and fallbacks | The output is `routing.policies` with `on_failure` chains. On hosted Otari, create them on the Routing page, since an API key can't write them. `offramp apply` serves deployments where the caller can. |
| Code execution | `POST /code/execute` grades generated code in the sandbox. |
| Decisions API | `--decision-model` adds a typed `choice` juror through `POST /decisions`, on deployments with a decision provider. Hosted Otari had none configured as of November 7, 2026, so the published run doesn't use it. |
| Budgets | `--budget` is a hard stop, enforced from `usage.cost_usd`. Offramp stops cleanly on `403 budget_exceeded` and `402 insufficient_funds`. It also reads `GET /key/budget` where a deployment serves it, which hosted Otari did not as of November 6, 2026. |
| Exact cost | `usage.cost_usd` feeds the ledger, the cost comparison and the savings projection. |
| Request tags | Every call carries `metadata` with its run, role and segment, so spend can be grouped by tag in Otari. |
| Idempotency | Every call has a deterministic `Idempotency-Key`, so a retry after a lost response isn't billed twice. |

## Quick start

```bash
cp .env.example .env        # add OTARI_API_KEY
uv run offramp doctor       # check key, models, sandbox and budget
uv run offramp models --grep deepseek
```

Give the key its own spend ceiling in Otari, so the gateway enforces a limit too.

Before spending anything, `plan` checks every model id, the sandbox and the jury's
independence against the live gateway, and prices the run from the catalog:

```bash
uv run offramp plan --workload examples/workload.jsonl \
  --incumbent xai/grok-4.7 \
  --candidates openai/gpt-oss-120b,deepseek/deepseek-v4-flash,alibaba/qwen-3.5-35b-a3b \
  --judges google/gemma-3-27b-it,meta/llama-3.3-70b-instruct
```

Run the bundled synthetic workload across classification, extraction, math, code
(24 distinct tasks, each phrased four ways) and writing:

```bash
uv run offramp run --workload examples/workload.jsonl \
  --incumbent <your current model> \
  --candidates <open-weight>,<open-weight>,<open-weight> \
  --judges <juror from another family>,<another juror> \
  --budget 2 --out runs/first
uv run offramp verify runs/first
```

Open `runs/first/report.html`. Deploy `policy.yaml`, or:

```bash
uv run offramp apply runs/first --workspace-id <id>    # needs management access, not an API key
```

### Use your real traffic

Record it through a local proxy, then replay it. The incumbent's answers come from
the recording, so that side costs nothing.

```bash
uv run offramp capture --out capture.jsonl
# Point your OpenAI client at http://127.0.0.1:8787/api/v1
# Label traffic with an X-Offramp-Segment header or metadata.segment
```

The capture file holds full prompts and answers, so treat it as sensitive. Recorded
cases go to the jury by default. Replace `grader` on any case with a checkable one
for stronger evidence.

### Workload format

One JSON object per line:

```json
{"id": "t-1", "segment": "support", "messages": [{"role": "user", "content": "..."}],
 "grader": {"type": "exact", "expected": "billing"}}
```

| Grader | Passes when |
| --- | --- |
| `exact` | The answer equals `expected`, ignoring case and outer punctuation. |
| `contains` | The answer contains each of `expected`. Partial credit. |
| `regex` | `pattern` matches. |
| `number` | The final number is within `tolerance` of `expected`. |
| `json_fields` | Fields of the JSON answer match `expected`. Partial credit, or all-or-nothing with `"strict": true`. Use strict when one wrong field makes the record wrong. |
| `python_tests` | The code passes `tests` in Otari's sandbox. |
| `jury` | Independent models compare it with the incumbent's answer. |

### In CI

```bash
uv run offramp gate runs/latest --min-share 0.5
```

It exits non-zero if less than half your traffic has proof behind it, so a model
or prompt change can't quietly lose its evidence. It also refuses simulated runs.

## What the guarantee does and does not cover

- It covers traffic like your sample. It says nothing about later drift, so rerun
  it when your workload changes.
- A pass means no worse than the incumbent by more than the margin, on the graders
  you chose. Weak graders give weak proof.
- A jury is still a model's opinion, even with these protections. Prefer checkable
  graders wherever you can.
- A model that refuses `temperature` runs at its default, which the report discloses.
  Answers cut off at the token limit count as given, and are listed per model.
- `weight` affects the savings projection and traffic share, not the statistics.
- If a candidate can't answer after retries, the case counts against it. If the
  incumbent can't answer, or its answer is cut off at the token limit, the case is
  dropped, so a handicapped baseline never gives a candidate credit.
- The ledger is hash-chained, not signed. It proves a run is unchanged only if you
  have published its head.
- `/code/execute` reports no price, so sandbox cost is an estimate. `--budget` counts
  each run at `--sandbox-cost`, $0.011 by default, inferred from one live account. Set
  a spend ceiling in Otari too, and check the dashboard for the true total.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests
python examples/make_workload.py > examples/workload.jsonl
```

Python 3.11+, with no runtime dependencies.

## License

Apache-2.0.
