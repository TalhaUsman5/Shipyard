# Handoff

Written so another engineer can pick this up cold. Two real products:
**Shipyard**, the harness, and **Release Manager**, the thing it built.
They live in two separate repos on purpose (see `DECISION_LOG.md`,
2026-09-21) — don't merge them back together.

## Repos and what's actually in them

- **Shipyard** — [`github.com/TalhaUsman5/Shipyard`](https://github.com/TalhaUsman5/Shipyard). The orchestration harness itself: `graph.py` (topology as data), `engine.py` (the generic walker), `agents.py` (the seven LLM roles), `llm_client.py`, `redaction.py`, `runtime.py` (cancellation), `worktrees.py` / `project_lock.py` (per-run isolation), `sessions.py`, `factory.py` (the FastAPI server), `dashboard.html`. Prompts live in `prompts/*.md`, one file per role, read fresh on every call — editing a prompt takes effect immediately, no restart.
- **Release Manager** — [`github.com/TalhaUsman5/Release-Manager`](https://github.com/TalhaUsman5/Release-Manager). The product Shipyard built: `release-manager-v2/` (the CLI — `configure`/`scan`/`prepare`/`approve`/`reject`/`publish`/`recover`/`status`) and `release-manager-review-ui/` (a thin web console that shells out to the real CLI, never reimplements its logic). Deployed live on Render.

## How to run Shipyard

```bash
cd Shipyard
pip install -r requirements.txt
cp .env.example .env   # fill in FACTORY_API_KEY and the per-role model vars
python -m uvicorn factory:app --host 127.0.0.1 --port 8000
```
Dashboard at `http://127.0.0.1:8000/dashboard`. To submit a run: `POST /run` with `{"feature_request": "...", "project_path": "<name under workspace/>"}`. A run pauses at `plan_review` for a human decision (`POST /sessions/{id}/review`) before any file gets written. Tests: `pytest -q` (158 passing as of this handoff, no real LLM/network calls — everything's mocked via `fake_agents`/`fake_agents_raw` in `tests/conftest.py`).

## How to run Release Manager

**CLI**, directly:
```bash
cd release-manager-v2 && npm install
GITHUB_TOKEN=... node release-manager.js configure --repo <owner>/<repo>
```
**Review console**, locally:
```bash
cd release-manager-review-ui && npm install
GITHUB_TOKEN=... REVIEW_UI_USERNAME=... REVIEW_UI_PASSWORD=... PUBLIC_ORIGIN=http://127.0.0.1:3000 node server.js
```
**Deployed**: hosted on Render via `render.yaml` (Blueprint), auto-deploys on push to `main`. `GITHUB_TOKEN`, `REVIEW_UI_USERNAME`, `REVIEW_UI_PASSWORD`, `PUBLIC_ORIGIN`, and (as of 2026-09-22) `RELEASE_APPROVERS` are all required and set in Render's Environment tab, never in the repo. Tests for both projects: `node --test` in each directory (9/9 in `release-manager-v2`, 25/25 in `release-manager-review-ui`).

## Known risks

- **`RELEASE_APPROVERS` must be set on the live deployment.** As of the approver-allowlist fix, `approve` fails closed if it's unset — this is correct behavior, but it means the deployed instance needs this env var added on Render or every approval will fail. Check this first if approvals suddenly stop working.
- **Audit log is not persistent on Render's free tier.** `audit-log.jsonl` has no path-override mechanism (unlike `.release-manager.json`'s `RELEASE_MANAGER_STATE_FILE`), so it resets on every redeploy. Documented in the Release-Manager README; not yet fixed. See "Open threads" below.
- **Whether Render's idle spin-down resets the filesystem the same way a redeploy does is unverified.** Confirm live rather than assuming either way before relying on state surviving an idle period.
- **A stuck `running` Shipyard session blocks its project indefinitely.** `project_lock.py` only releases on `completed` — a session left `running` after a server crash (not `cancelled`, not `failed`) holds the lock until it's explicitly resumed. `stuck_run_warning` in `sessions.py` surfaces this via the dashboard; the fix is to resume the stuck session, not to hand-delete its lock file, unless you've confirmed the session is genuinely abandoned.
- **`shipyard-rm-throwaway`** (the real GitHub repo used for all live Release-Manager testing) is a disposable test repo, not production infrastructure — treat anything published there (releases, commits) as evidence artifacts, not real product state.

## What not to touch without understanding why first

- **`worktrees.py` / `project_lock.py`'s interaction.** The merge-on-completion logic in `worktrees.merge_worktree` assumes no concurrent worktree can exist for the same project — that assumption is *entirely* provided by `project_lock.py`. Changing one without understanding the other reopens a real collision risk that's already caused one near-miss (`FAILURE_LOG.md` #7) and motivated this whole isolation layer.
- **The `_worktree_path` top-level-sibling placement.** It looks like it could be "cleaned up" into a nested directory — don't; see `FAILURE_LOG.md` #17. This isn't cosmetic.
- **`redaction.py`'s single chokepoint in `llm_client.call()`.** Don't add a second, parallel redaction path anywhere else — the whole guarantee is that there's exactly one place secret-shaped content can leak through, and that's provable specifically because there's only one.
- **The structural test-path strip in `engine.py`'s `_handle_build`.** It looks redundant with the Builder's own system prompt telling it not to write tests. It is not redundant — see `FAILURE_LOG.md` #10. The prompt-only version already failed once, live.
- **`server.js`'s hardcoded three-route evidence allowlist.** Don't convert it to a generic directory-serving route "for convenience" — the whole point is that exactly three fixed filenames are servable, with no request-controlled path ever reaching the filesystem.

## Open threads (not started, or started and deliberately stopped)

- **`AUDIT_LOG_PATH` override for `release-manager-review-ui`'s audit log**, following the same pattern as `RELEASE_MANAGER_STATE_FILE`, so the audit trail can also live on a persistent volume (or survive a Render redeploy). Explicitly deferred, not forgotten — see `DECISION_LOG.md` and the Release-Manager README's "Known limitation" note.
- **A `graph.py` budget change (`build_retry`/`review_retry` 4/6 → 2/2) is drafted but not yet committed to Shipyard.** It exists as uncommitted local changes (`AGENTS.md`, `README.md`, `graph.py`, `tests/conftest.py`, `tests/test_engine.py`) at the time of this handoff — real evidence already showed the wider budgets were never actually needed by any observed run, but the commit itself is still pending a deliberate decision to land it.
- **No fuzzing or property-based testing anywhere** — all coverage across both repos is example-based. Not required by anything specific, but it's the natural next rung if correctness confidence needs to go further.
- **No security pass has been done on `server.js` specifically**, beyond the auth/origin/injection-resistance work already covered in `FAILURE_LOG.md`. `release-manager.js` (the CLI) got more scrutiny by virtue of handling the real GitHub token.

## Loom + Runner vs. Shipyard — do not conflate these

Herald's trial documentation names this explicitly: *"Shipyard is Talha's
deterministic-harness factory. It is a parallel, complementary approach to
Herald's Loom + Runner skill-type factory. The two must not be conflated."*
Concretely, here's the actual difference, not just a naming distinction:

**Loom + Runner** is a prompt/skill-driven factory — the control lives in
an agent's own judgment and discipline. **Loom** is the planning compiler:
grill → SuperSpec → critique → canonical PRD → a Linear dependency graph.
Input is an interrogated idea; output is an authoritative PRD plus a task
breakdown in Linear. It stops at "here is the settled contract and the work
breakdown" — it never builds anything itself. **Runner** is the execution
engine against that graph: it takes the Linear-backed queue and drives
coding agents (Cursor/Codex workers) through the issues, with review gates
and test checks layered on as *operating policy* — skills like
`runner-low-burn-v2` exist precisely because that control is advisory, not
enforced by anything that can refuse to proceed.

**Shipyard** is both halves in one code-enforced harness. The phase graph —
plan → build → verify → test_gate → review → review_gate → calibrate,
arbiters, retry budgets, resume — is executed by deterministic code. LLMs
are workers *inside* phases; they can't skip a gate, exceed a retry budget,
or declare success without the gate actually passing. The session report
is a machine-readable artifact of what genuinely happened, not a summary
an agent chose to write.

Three real, concrete differences:

1. **Enforcement.** Loom+Runner relies on the agent following its skills; a
   lazy or hallucinating agent in Runner can wave work through unnoticed.
   In Shipyard the run literally *cannot* complete unless `test_gate`
   passes — there is no code path where a session reaches `"completed"`
   with a failing test suite.
2. **Source of truth.** Loom+Runner's source of truth is Linear issues plus
   the canonical PRD. Shipyard's is its own session state: a trace, a
   per-phase attempt count, and durable artifacts (`sessions/*.json` and
   `.events.jsonl`) written by the harness itself, not curated by an agent
   after the fact.
3. **Failure handling.** Loom+Runner's review gates and test checks are
   operating policy layered on by skill design — configurable, and only as
   reliable as the skill that enforces them. Shipyard's gates are the code
   path itself: a classifier's route, a retry budget, and
   `failed_needs_human` when a budget runs out are the only ways a session
   can end besides `completed`. There's no route back to "call it done
   anyway."

Neither approach is strictly better across every axis — Loom+Runner's
planning compiler (grill → SuperSpec → critique) is a genuinely different
and valuable capability Shipyard doesn't have (Shipyard's `plan_review` is
a single human gate on one Planner's output, not an interrogation
pipeline). They're complementary, evaluated on different axes, exactly as
the trial documentation frames it — not competing claims about which
factory is "the real one."
