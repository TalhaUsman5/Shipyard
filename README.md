# Software Factory

A custom-built harness — not Claude Code — using an LLM (any OpenAI-compatible
model you configure) as the orchestrating intelligence behind seven
specialized roles, plus one human checkpoint (`plan_review`) before any of
them start writing code. You author and run the control loop yourself, in
plain Python, so every decision the harness makes is visible to you.

This is a **learning project**, scoped deliberately small: one feature per
run, one target project at a time, no git/CI automation. Every layer is the
simplest version that's still real — real file writes, real test runs, real
role independence.

## What it does

You point it at a Node.js project inside `workspace/` — existing or brand
new — and describe a feature in plain English. The request is run through a
**phase graph**: an explicit topology of typed nodes (`graph.py`), walked by
a generic engine (`engine.py`) that has no built-in knowledge of what any
node does — only how to run one, gate on its result, and route on failure.

```
                       ,-> build  -,
plan -> plan_review --+           +-> test_gate --pass--> review -> review_gate --pass--> calibrate -> done
           |           `-> verify -'         |                              |
         reject                           fail                           fail
           v                                v                              v
         plan                            arbiter                  review_arbiter
   (human-paced, no budget)   (bug / noise / spec_gap / ambiguity / test_gap) (implementation_gap / scope_creep / test_gap)
                  bug      -> build (verify reused)          budget: build_retry x4
                  noise    -> test_gate (rerun as-is)         budget: build_retry x4
                  spec_gap -> plan (plan/build/verify redone) budget: plan_retry x1
                  ambiguity-> plan (plan/build/verify redone) budget: ambiguity_retry x1
                  test_gap -> verify (verify redone)          budget: test_retry x1 (SHARED, see below)

                  implementation_gap -> build (build/review redone)      budget: review_retry x6
                  scope_creep        -> plan (plan/build/verify/review redone) budget: scope_retry x2
                  test_gap           -> verify (verify/review redone)   budget: test_retry x1 (SHARED, see below)
```

`plan_review` runs right after `plan`, before any `build`/`verify`/`review`
work happens: it pauses the run and hands the generated contract to a
human, who either approves it (straight on to `build`) or rejects it with
feedback (back to `plan`, which re-derives the contract using that
feedback, then pauses at `plan_review` again). It has no retry budget — a
human decides how many refinement rounds are worth doing, not a fixed
counter — and an approval is trusted across a crash/resume (see
`graph.py`'s docstring): a human isn't asked to re-approve the exact same
contract just because something unrelated crashed later in the pipeline.

This replaced an earlier automated `scope_gate`, an LLM judgment call
asking the same "is this contract actually grounded in the feature
request" question `review_arbiter`'s `scope_creep` check below asks
reactively, just proactively — added after watching several real runs pay
for a full `build` -> `verify` -> `review` cycle only to discover at the
very end that the contract itself had already overshot the ask. A human
reading the contract before any implementation effort is spent is strictly
more trustworthy than one LLM checking another LLM's output, and it's the
one point in the pipeline a person is guaranteed to look at the plan
before real work starts on it.

`build` and `verify` run **concurrently**, not sequentially — see
`graph.PARALLEL_GROUPS` and `engine._run_parallel_group`. Neither depends
on the other's output (the Verifier is deliberately never given
`build_output`, below), and `test_gate` is the actual synchronization
point, so there was never a reason to make one wait on the other. A route
that resets only one of them (`bug` resets just `build`, `test_gap` resets
just `verify`) still only reruns that one — the other stays cached, same
as before this existed.

A run only reaches a true dead end (`failed_needs_human` with nowhere left
to go) by exhausting one of these budgets, or an unrecognized
classification — every declared classification has somewhere bounded to go
first, including `ambiguity`: it's routed exactly like `spec_gap` (the
Arbiter's `feedback_for_planner` gets threaded into the Planner's next
attempt, and resetting `verify` too means the Verifier re-derives its tests
against the clarified contract, not just narrows the contract to fit
whatever the existing test happened to check).

A Reviewer rejection doesn't route straight to `build` — it goes through
`review_arbiter` first, the same judgment-then-route pattern `test_gate`
uses via `arbiter`. Most rejections are genuine implementation gaps and
route to `build` exactly as you'd expect (`build`'s own forward edge
already runs `verify` (cached, unchanged) -> `test_gate` (always fresh) ->
`review` (reset, so it re-judges the new code) -> `review_gate`, so one
retry re-earns the test gate too, not just a second Review opinion on the
same code). But a Reviewer can be faithfully enforcing a contract that
itself demands more than the *original feature request* ever asked for —
the Planner over-elaborated it — and no amount of retrying `build` can
satisfy a contract that overshot the ask. `review_arbiter` is given the
original feature request specifically to catch that case, and routes back
to `plan` to narrow the contract instead — bounded by its own small
`scope_retry` budget (2 uses) so a genuine scope correction happens a
bounded number of times, not indefinitely.
Unlike `spec_gap`/`ambiguity` (triggered before `review`
has ever run), `scope_creep`'s reset list includes `review` itself — it
already ran and rejected once, so without resetting it `review_gate` would
just re-check that same stale rejection against the narrowed contract
instead of getting a fresh verdict.

A third outcome, `test_gap`, exists on **both** classifiers, because the
same underlying problem — a defect *in the tests themselves*, not the
implementation — can surface two different ways. `review_arbiter`'s
version catches it when the Reviewer notices the submitted tests are
inconsistent or incomplete even though they technically pass; `arbiter`'s
version catches it when a broken test fixture makes `test_gate` fail
outright. Neither `bug`/`implementation_gap` can fix it — both reset
`build` (and `review`, for `implementation_gap`) but never `verify`, and
the Builder never sees test code in the first place (`BUILDER_SYSTEM`'s
own rule) — so the same test defect would recur every single cycle no
matter how many retries ran, since nothing ever gave the Verifier a chance
to rewrite the file. `arbiter`'s version was added after watching it
happen live: a real run's Arbiter correctly *diagnosed* a broken
`FakeGitHub` mock in its explanation text, but had no better label than
`"bug"` to pick — so it sent the fix to Build anyway, burning a real
retry on something Build could never touch. Both routes share the same
`test_retry` budget (one resource, two triggers — `test_graph.py` enforces
they declare identical `max_uses`, since whichever fires reads its own
`max_uses` against a shared "used" count), with the finding threaded to
the Verifier as feedback the same way Build gets `ARBITER FEEDBACK`.

Every `build` retry (whatever triggered it) sees the real, current state of
the project — not just what its own last attempt happened to return.
`_handle_build` re-scans the project directory and reads every non-test
file fresh off disk (`execution.read_text_files`) as `CURRENT PROJECT
FILES`, so it sees everything actually there, including files an *earlier*
retry wrote that this one doesn't happen to touch again — a narrower
"my last attempt" view can't see those. Test files are excluded from this
read: the Builder must never see test code (`BUILDER_SYSTEM`'s own rule),
so a whole-directory read still can't leak it. Feedback comes along
separately as `ARBITER FEEDBACK` (the Arbiter's diagnosis, when the retry
was a `bug` classification) and `PREVIOUS REVIEW` (the Reviewer's last
verdict, when the retry was a rejection) — both read straight from
`arbiter`'s/`review`'s own persisted node output, not duplicated anywhere
else. Together, this is what lets the Builder patch instead of rewrite:
smaller diffs, faster retries, and no risk of losing whatever an earlier
retry already fixed.

A re-`review` gets the equivalent treatment: `_handle_review` reads the
last persisted `review` output (survives `review_gate`'s reset the same
way build's does — `reset_nodes` only flips status, never deletes the
persisted output) and hands it to the Reviewer as `PREVIOUS REVIEW`,
instructed to check each past finding against the current code rather than
re-auditing from scratch — otherwise a genuinely-fixed issue and one just
reworded into different phrasing look identical to it, and it can spend
its whole retry budget re-flagging things that already got fixed.

Neither of these paths touches the contract anymore. Earlier versions of
this fed retry feedback forward by appending it onto `contract.constraints`
— permanently, on every single rejection, for the life of the run. That
constraint list only ever grew, since nothing ever needed to remove an
entry once the issue it described was fixed — every role reading the
contract (including the Planner on a `spec_gap`/`ambiguity` refresh) paid
for that growing text on every call, for the rest of the run. Since the
Arbiter's and Reviewer's own outputs are already durable (survive a crash/
resume) and are the actual source of truth, there was never a need to
duplicate them into the contract at all — `_handle_build`/`_handle_review`
now just read them directly. The contract stays exactly what the Planner
wrote (or last rewrote, on a `spec_gap`/`ambiguity` refresh) — nowhere
near the ~21KB of accumulated feedback text a long-running real session hit
before this change.

- `plan`, `build`, `verify`, `review`, `calibrate` are **work nodes** — they
  call an LLM role and produce a typed output (`schemas.py`).
- `test_gate`, `review_gate` are **gate nodes** — deterministic pass/fail
  checks (a real `npm test` run; `review.approved`). Gates always execute
  when reached; they're never skipped or cached.
- `plan_review` is a **human_gate** node — its verdict comes from a person,
  not a re-derivable fact, so (unlike an ordinary gate) an already-approved
  verdict IS trusted across a resume; see `graph.py`'s docstring.
- `arbiter` is a **classifier node** — it runs only after a failed gate and
  routes to one of a handful of pre-declared outcomes, each with its own
  bounded retry budget (see `graph.py`'s `PHASE_GRAPH`).

## Prompt discipline (Planner, Verifier)

Three real, live failures on the release-manager pipeline motivated
`plan_review` above and two prompt changes:

- An 8-point feature request came back from the Planner as 21 constraints
  and 20 acceptance criteria, some inventing non-functional requirements
  (cross-host lock races, distributed coordination) nobody asked for.
  `PLANNER_SYSTEM` now has an explicit "TRACEABILITY IS MANDATORY" section
  forbidding invented requirements unless the feature request actually
  asked for them — `plan_review` is the backstop (now a human's, not an
  LLM's) for when a contract slips past this instruction anyway.
- The Builder and Verifier never see each other's code (by design — that's
  what makes Verifier's tests an honest check rather than a rubber stamp),
  but a real run had them independently converge on *different* signatures
  for the same operation (`review(id, {decision, approver, reason})` vs.
  `approve(id, approver, reason)`) because the contract never pinned the
  exact shape down. `PLANNER_SYSTEM` now has an "EXACT INTERFACES ARE
  MANDATORY WHEREVER THEY MATTER" section requiring literal function
  signatures in the contract whenever Build and Verify need to
  independently agree on one.
- `VERIFIER_SYSTEM` now has a "PREFER MINIMAL MOCKING" section: test pure
  logic directly, reserve mocks for genuinely external systems, and keep
  any mock's behavior consistent with the contract — a defective mock is
  indistinguishable from a real bug until someone reads the test file, and
  a broken `FakeGitHub` mock is exactly what forced the `arbiter`/
  `review_arbiter` `test_gap` routes described above to exist at all.

`context.py`'s file-listing cap (200 files) and `execution.py`'s
file-content cap (150KB) used to truncate silently — a role had no way to
know its view of the project was partial. Both now say so explicitly in
the prompt (`context.describe_snapshot`; the `CURRENT PROJECT/TEST FILES`
note), and the file-listing cap prioritizes `contract.target_files` ahead
of whatever directory-walk order turns up first, so a large project drops
irrelevant files before it ever drops one the contract actually names.

## Two separate actions — don't conflate them

1. **You start the server manually, once:**
   ```
   python factory.py
   ```
   This starts a local API on `http://127.0.0.1:8000`. It does not submit
   any feature request by itself.

2. **You submit and review each feature run separately, via Postman,**
   against that running server. A `postman_collection.json` is included —
   import it, point `base_url` at your server, and use it directly.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and set FACTORY_API_KEY, FACTORY_BASE_URL, FACTORY_MODEL
python factory.py
```

`llm_client.py` talks to any OpenAI-compatible endpoint via the OpenAI SDK
(`client.chat.completions.create(...)`), not the Anthropic SDK directly —
point `FACTORY_BASE_URL` and `FACTORY_MODEL` at whatever provider you're
using.

## Running the tests

```bash
pytest
```

No API key, no `.env`, no Node/`npm` required — `tests/conftest.py`
monkeypatches every `agents.run_*` call and `execution.run_tests` with
deterministic fakes, and redirects `engine.WORKSPACE_DIR` /
`sessions.SESSIONS_DIR` into a pytest `tmp_path` per test, so nothing
touches the real `workspace/` or `sessions/` directories or costs a token.
`tests/test_graph.py` checks the topology itself is well-formed (no
dangling node references); `tests/test_sessions.py` covers node lifecycle,
budgets, and the append-only trace log in isolation; `tests/test_engine.py`
drives the actual graph walk — bug retries, budget exhaustion, Reviewer
rejection retries, resume, and new-repo scaffolding.

## Isolated workspace

Every target repo lives under `workspace/`, never at the software-factory
root. `project_path` in a `/run` request is a name relative to `workspace/`
(not an absolute path) — if it doesn't exist yet, it's treated as a brand
new repo and an empty directory is created for it there. A ready-made
example project is included at `workspace/example-target-project/`:

```json
{ "feature_request": "Add a greet(name) helper that says hello",
  "project_path": "example-target-project" }
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Confirms the server is up |
| POST | `/run` | `{"feature_request": str, "project_path": str}` — creates the session and starts walking the graph from `plan` on a background thread; returns immediately with the session (status `"running"`, nothing executed yet) rather than blocking until the run finishes |
| POST | `/sessions/{id}/resume` | Re-walks the graph (in the background) for a `failed` / `failed_needs_human` / `running` / `cancelled` session — nodes already `succeeded` are trusted and skipped; gates always re-check. Optional body: `{"grants": {"<budget_key>": N}, "grant_reason": str, "granted_by": str}` — see "Granting more retries" below |
| POST | `/sessions/{id}/review` | `{"approved": bool, "feedback": str?, "node_id": str?, "reviewed_by": str?}` — submits a human decision at a paused `human_gate` (currently only `plan_review`) and continues the walk in the background. `feedback` is required when `approved` is `false` |
| POST | `/sessions/{id}/cancel` | Signals a running session to stop at the next safe checkpoint (a node boundary, a chunk of a streaming LLM call, or a poll interval inside `npm test`). 409 if the session isn't actually running in this server process |
| GET | `/sessions/{id}/live/{node_id}` | The raw text a currently-generating node has streamed so far (`{"node_id", "text", "running"}`) — see "Dashboard" below |
| GET | `/sessions` | Lists all past runs (id, status, project_path, current_node, awaiting_node, resumable, total tokens). Optional `status`, `project_path` (exact match) and `since` (unix timestamp) query params — e.g. `?status=failed_needs_human&since=<7 days ago>` answers "every run that hit failed_needs_human this week" without hand-parsing JSONL |
| GET | `/sessions/slowest-nodes` | Which node costs the most wall-clock time, aggregated across the `limit` (default 20) most-recently-started sessions — `{"nodes": [{"node_id", "total_seconds", "run_count"}]}`, slowest first |
| GET | `/sessions/{id}` | Current-state snapshot for one run: per-node status/output/budgets, plus a `tokens` rollup (`{"input", "output", "total"}`) summed across every node — works mid-run, not just once terminal |
| GET | `/sessions/{id}/report` | A reviewable summary: contract, files written, test/review verdicts, calibrated patterns, token rollup |
| GET | `/sessions/{id}/trace` | The append-only evidence log — see "Proving a build came from the factory" below |
| GET | `/dashboard` | A live view of the phase graph — see "Dashboard" below |
| GET | `/memory` | Current contents of `AGENTS.md` |
| GET | `/memory/promotion-candidates` | Rolling patterns that have recurred at least `min_seen` times (default 3) — read-only; see "Domain memory" below |

A run now pauses at `plan_review` (status `"awaiting_review"`) until
`/sessions/{id}/review` is called — see the phase graph section above.

`project_path` is a name relative to `workspace/` — see "Isolated
workspace" above.

## Dashboard

Open `http://127.0.0.1:8000/dashboard` in a browser while `factory.py` is
running. It's a single static page (`dashboard.html`) served same-origin by
the API itself — no separate build step, no CORS, and `/dashboard` re-reads
the file off disk on every request, so you can edit it and just refresh.

It shows:

- **The phase graph itself**, drawn directly from a run's latest
  `GRAPH_SNAPSHOT` event — not a hardcoded diagram, so it reflects whatever
  actually governed that run (including a raised retry budget or a changed
  route between resumes).
- **The current node**, highlighted and pulsing while a node is running.
- **A progress bar** over the eight-node happy path
  (plan→plan_review→build→verify→test_gate→review→review_gate→calibrate); it
  can legitimately move backward during a retry, since a reset node really
  did just go back to `pending`.
- **Edges animating** as a pulse travels from the node that just finished
  to the one that started next, reconstructed live from consecutive
  `NODE_STARTED` events in the trace — so a retry loop (Arbiter → build,
  Reviewer rejection → build, etc.) visibly plays out, not just the happy
  path.
- **Retry budgets**, used/effective-max per key, pulled from the graph
  snapshot and the session's current usage, with any granted top-up shown
  alongside it.
- **A form to start a new run** — `feature_request` + `project_path`,
  posting straight to `/run`; since that endpoint now returns immediately
  (see "Endpoints" above), the graph view picks up the new session right
  away instead of the page hanging until the whole run finishes.
- **A node detail panel on click**: while a node is running, it polls
  `/sessions/{id}/live/{node_id}` and shows the model's response streaming
  in — genuinely what that role is generating, not a simulation. Once a
  node finishes, the panel just shows its persisted output/error from the
  session snapshot. Nodes with no LLM call (`test_gate`, `plan_review`)
  only ever show their persisted result.
- **A plan_review panel** when a run is paused awaiting review: the
  Planner's contract, with Approve / Request changes (feedback) buttons
  posting to `/sessions/{id}/review`.
- **A Cancel button**, shown while a session is `running`, posting to
  `/sessions/{id}/cancel`.
- **The report and trace endpoints**, inline — no separate Postman calls
  needed to see the contract, files written, verdicts, or the raw
  append-only event log.
- **Resume and grant controls**, directly on the page — no curl, no
  Postman, no code edit. See "Granting more retries" below.

It polls `/sessions/{id}` and `/sessions/{id}/trace` every 1.5s while a run
is in progress and stops once it reaches a terminal status. This is
polling, not a push (SSE/WebSocket) — fine for a local dev tool, but worth
knowing if you're watching something that updates faster than 1.5s.

## Proving a build came from the factory

`sessions/<id>.json` is convenient but not evidence — it's rewritten in
full on every event, so nothing stops it (accidentally or otherwise) from
being edited after the fact. The actual audit trail is
`sessions/<id>.events.jsonl`: one JSON object per line, opened in append
mode, and the code (`Session.record_event` in `sessions.py`) never seeks
backward or rewrites a line once written. To trust that trail you only have
to trust that invariant, not run a verification script over it.

What's in it:

- **Topology** — the first line of every run (and every resume) is a
  `GRAPH_SNAPSHOT` event: the full `PHASE_GRAPH` — every node, gate,
  route, and retry budget — exactly as it existed at that moment. The log
  is self-contained proof of what governed the run even if `graph.py`
  changes later. If a resume's snapshot differs from the original run's,
  that's a real discrepancy worth noticing, not a bug.
- **Trace** — `NODE_STARTED` / `NODE_SUCCEEDED` / `NODE_FAILED` for every
  node the walker actually visited, in order, with each node's typed
  output and token usage attached. A `NODE_FAILED` carries an `error_kind`
  when the failure happened at the inference layer itself rather than in
  our own code — `ModelBlocked` (bad credentials or a content-policy
  refusal), `AllowanceExhausted` (a real quota/billing ceiling, not a
  transient limit), or `CapacityInsufficient` (the provider is rate-limited,
  overloaded, or briefly unreachable — the one of the three plausibly worth
  just retrying). `error_kind` is `null` for everything else — a malformed
  JSON response, a bug in our own code — exactly as before this existed.
  See `llm_client.py`'s `InferenceError` hierarchy and `_classify`.
- **Secrets** — every role's prompt passes through `redaction.py` inside
  `llm_client.call()` before it reaches the provider: credential-shaped
  content (API keys, private key blocks, JWTs, credentials embedded in a
  URL, …) is scrubbed and replaced with a `[REDACTED:<pattern>]` marker.
  A `SECRET_REDACTED` event is logged whenever this fires, naming which
  pattern(s) tripped it — never the value that was removed. Default policy
  is redact-and-continue (a regex scanner has real false positives; this
  keeps the pipeline usable rather than dying on a test fixture's fake
  token) — set `FACTORY_SECRET_POLICY=block` to raise instead of redacting.
- **Gates** — `test_gate` and `review_gate`'s `NODE_SUCCEEDED` events carry
  the real pass/fail result (actual `npm test` output; `review.approved`).
  On a `test_gate` failure, `execution.run_tests` also re-runs the suite
  once more with Node's TAP reporter (`--test-reporter=tap`) and attaches a
  parsed per-test breakdown as `structured` — for each failing test, total/
  passed/failed counts and, per failure, whether it looks like a genuine
  assertion mismatch (`code: 'ERR_ASSERTION'`) versus some other exception
  thrown before any assertion ran. This is a heuristic signal the Arbiter
  weighs alongside the explanation it already reasons from, not a verdict
  on its own — but it's exactly the kind of mechanical evidence that used
  to only be visible by eyeballing raw `npm test` stdout. It's best-effort
  only: if the project's test script isn't `node --test` under the hood,
  `structured` comes back `{}` and the Arbiter falls back to reasoning from
  raw stdout/stderr alone, same as before this existed. `package.json`'s
  `scripts.test` is checked upfront (`execution._uses_node_test_runner`) so
  this second, redundant subprocess call is skipped outright for a project
  that's clearly using a different test runner, rather than paying for a
  full rerun whose result would just be thrown away.
- **Retries** — every routing decision is its own `ROUTE_TAKEN` event
  (which classification/gate-fail, which target, which nodes got reset,
  which budget was consumed and how much is left) or `ROUTE_BUDGET_EXHAUSTED`
  when a bound is hit. A retry is never just inferable from a repeated
  `NODE_STARTED` — it's an explicit, labeled decision in the log.

`GET /sessions/{id}/trace` serves this same file back over the API; the
raw file is also just sitting in `sessions/` for direct inspection
(`cat sessions/<id>.events.jsonl`).

## Retrying and resuming are the same mechanism

A work node (`plan`/`build`/`verify`/`review`/`calibrate`) is skipped and
its prior output reused whenever it's already marked `succeeded` in the
session — nothing re-invents its output. A retry is just some route
(the Arbiter's, on a failed `test_gate`; or `review_gate`'s own `on_fail`
on a Reviewer rejection) resetting a specific set of nodes back to
`pending` before jumping to its target — e.g. a `bug` classification
resets only `build`, not `verify` (the tests don't need rewriting because
the implementation was wrong); a Reviewer rejection resets `build` and
`review` (new code needs a fresh Review opinion, and re-earns `test_gate`
for free since gates always re-run). Resuming an interrupted run is the
*same* walk, started fresh from `plan`, with nothing reset — everything
that already succeeded before the crash just skips itself, and the walk
picks up wherever it actually stopped. Gate nodes (`test_gate`,
`review_gate`) are the one exception: they always re-execute for real,
never trusting a cached pass/fail, on every walk.

## Granting more retries

Sometimes a budget exhausts and the remaining work is real, not runaway —
the run just needed a few more attempts than `graph.py`'s declared
`max_uses` for that route. The fix is **not** to edit `graph.py` and
redeploy for every session that hits this; that doesn't scale past a
single operator with source access sitting next to the server, and it
conflates two different things: `graph.py`'s `max_uses` is the *default
policy* for every run, while a specific stuck session needing more room is
a *one-off operator decision*.

`POST /sessions/{id}/resume` takes an optional body:

```json
{
  "grants": {"review_retry": 4},
  "grant_reason": "unblock the demo",
  "granted_by": "operator"
}
```

This adds `4` to `review_retry`'s ceiling **for this session only** —
`graph.py`'s declared `max_uses` is untouched, so every other session still
runs under the same default policy. It's additive, not a reset: the
"used" count (`session.budgets`) is never touched, so it stays an honest
record of what actually happened; the grant lives in a separate field
(`session.budget_grants`), and `_take_route`'s check becomes `used <
max_uses + granted`. A `grant_reason` is required — `Session.grant_budget`
raises if it's missing — and every grant is its own `BUDGET_GRANTED` event
in the append-only trace, so "why did this session get more room than
usual" is always answerable from the log, not tribal knowledge.

The dashboard's **Resume** panel is the intended way to do this in
practice: pick the exhausted budget from the dropdown (populated from the
session's own graph snapshot), give a reason, and click Resume — no
Postman, no curl, no code change, no engineer in the loop.

## Domain memory (AGENTS.md)

Two sections, two different write rules:

- **Domain Rules (Permanent)** — hand-edited only. Nothing in this codebase
  ever writes to it, promotes a pattern into it, or retires a rule from it
  automatically. A rule earning its way in, or one that's since proven
  wrong, is a deliberate edit a human makes directly in the file.
- **Recent Patterns (Rolling)** — the Calibrator appends here after a
  successful run (`calibrate` can now extract *multiple* distinct lessons
  per run, not just one — a run with both a bug retry and a review
  rejection usually holds two separable lessons, not one vague merge of
  both). Each bullet carries a machine-parseable
  `[meta: seen=N, source=<project>]` suffix: re-deriving the same pattern
  on a later run increments `seen` and updates `source` to the latest
  project it came from, instead of duplicating the line (a real bug, once
  live: 14 duplicate copies of one pattern before this existed). Capped at
  15 entries so it can't crowd out the context window.

`GET /memory/promotion-candidates?min_seen=3` surfaces rolling patterns
that have recurred at least `min_seen` times — worth an operator's
attention as a candidate for manual promotion into Domain Rules. It's
read-only: it never writes anything, to Domain Rules or otherwise.

## Testing the endpoints created by a feature

The factory verifies the feature it builds automatically — that's the
`npm test` gate inside the pipeline. It does **not** expose the target
project's own endpoints for you. If your feature adds an HTTP API to the
target project, that project has its own server; run it separately
(`npm start` or similar, inside the target project) and hit *that* server
from Postman. The factory's API (port 8000) and the target project's API
(whatever port it runs on) are two different servers.

## Model

All seven LLM roles share one model, set via `FACTORY_MODEL` in `.env`,
called through an OpenAI-compatible client (see `llm_client.py`) that
streams every response — both to give the dashboard live output and to
make cancellation responsive mid-call — and transparently falls back to a
plain (non-streaming) call if the configured endpoint doesn't support
`stream=True`. Point `FACTORY_BASE_URL` at any OpenAI-compatible endpoint.

Every call also passes through the secrets boundary (`redaction.py`) and
provider-failure classification (`llm_client.InferenceError` and its
subclasses) described in "Durable state" above. `FACTORY_SECRET_POLICY`
(`redact`, the default, or `block`) is the only other environment
variable either of those introduces.

## The 11 harness components, and where they live

| Component | File |
|---|---|
| Instructions | Per-role system prompts, one file per role under `prompts/` (loaded fresh on every call via `prompts.py` — editing a `.md` file takes effect on the next call, no restart) |
| Context delivery | `context.py` |
| Model | `llm_client.py` |
| Memory | `memory.py`, `AGENTS.md` |
| Topology | `graph.py` — the phase graph as data (nodes, gates, routes, retry budgets) |
| Durable state | `sessions.py` — `sessions/*.json` (current-state snapshot) + `sessions/*.events.jsonl` (append-only evidence log) |
| Sub-agents | The seven `run_*` functions in `agents.py` (contract scope is now checked by the `plan_review` human gate, not an eighth LLM role) |
| Orchestration | `engine.py` — the generic walker + `NODE_HANDLERS` (the only code that knows what each node id means) |
| Skills / procedures | Domain Rules section of `AGENTS.md` |
| Execution environment | `execution.py` (sandboxed writes, real test runner) |
| Tool interface | `execution.py` (file I/O + `npm test` subprocess) |
| Verification / observability | `execution.run_tests`, `sessions.py` event log, `test_gate`/`review_gate`/Arbiter |
| Automated tests | `tests/` (pytest) — topology integrity, session state, and the full graph walk (retries, budgets, resume, scaffolding), all offline |
| Dashboard | `dashboard.html`, served at `/dashboard` — live phase graph, progress, edge animation, budgets, report/trace |

## Known v1 limitations

- No git/PR automation — code lands on disk, you review and commit it yourself.
- Retries are capped per route (`build_retry`: 4, `plan_retry`: 1,
  `ambiguity_retry`: 1, `review_retry`: 6, `scope_retry`: 2, `test_retry`: 1)
  plus a global 140-iteration safety net (`graph.MAX_TOTAL_ITERATIONS`) —
  a run that exhausts any of these ends as `failed_needs_human`. `plan_review`
  has no budget at all; a human paces it.
- A session has exactly one *walk* active at a time (there's no protection
  against two concurrent `/resume`/`/review` calls against the *same*
  session racing each other) — but within that one walk, `build` and
  `verify` genuinely run on two threads at once (see `graph.PARALLEL_GROUPS`
  above). `Session.record_event` is locked specifically so that's safe: the
  event log's `seq` counter can't collide, and the underlying file write is
  atomic (`sessions.Session.save`) either way. Fine for the single-operator
  use this is built for.
- Cancellation is cooperative, not preemptive: it's checked at each node
  boundary, inside the LLM streaming loop, and inside `npm test`'s wait
  loop, so it's fast in practice, but a `POST /cancel` can't interrupt code
  that isn't at one of those checkpoints.
- The live-output registry (`runtime.py`) and cancel signals are in-memory
  only, per server process — they reset on restart. A run's actual
  progress/output is unaffected (that's all in `sessions/`), you just lose
  the ability to cancel or see live output for whatever was mid-run at the
  moment of restart.
- No auth on the local API — it's meant to run on localhost only.
- Context sent to each role is a file listing + `package.json`, not full
  file contents — fine for small, greenfield-ish features; a large existing
  codebase would need a smarter context layer.
- A brand-new `project_path` is scaffolded with only a minimal
  `package.json` (`node --test` as the test script) — no `src/`, no
  starting test file. The Builder/Verifier still have to write everything
  else; `node --test` against zero test files exits 0 (a vacuous pass), so
  the first real signal only shows up once the Verifier's tests land.
