# Failure Log

Every entry here surfaced by actually running the system — none were caught
by code review. That's the throughline of this whole project: the real
test suite was live operation, and automated tests came *after*, written
as regression coverage for what running it for real had already taught.
Entries 1–11 are exported from [`shipyard_dossier.html`](../shipyard_dossier.html)
§4 ("Problems found, in the order they were found"); entries 12 onward
happened after that dossier was published and are new to this log.

Evidence links point to files actually committed in a repo. Where a
session's full JSON/event log wasn't published as evidence, the session ID
is still given so the record can be pulled from `sessions/` if needed —
`sessions/*.json` is gitignored by design (see `docs/HANDOFF.md`), so most
of the sessions below are cited by ID rather than a live link.

---

### 1. No route existed for "the test is broken, not the code"
- **What broke:** An Arbiter correctly diagnosed a broken test fixture in its own explanation text, but had no classification label better than `"bug"` — which routes to the Builder, who never sees test code and could never fix it. A real retry burned for nothing.
- **Root cause:** The classifier's route set didn't have a label for "the test itself is defective," only for "the implementation is wrong."
- **Fix:** Added `test_gap` to both classifiers (`arbiter` and `review_arbiter`), routing to `verify` instead of `build`, sharing one budget between the two triggers.
- **Lesson:** A classifier is only as good as its label set — a correct diagnosis with the wrong available route still produces the wrong outcome.
- **Evidence:** Session `5601a0c16d7a` (legacy analysis); `graph.py`'s `test_gap` routes.

### 2. An 8-point spec became 21 invented constraints
- **What broke:** The Planner turned a short, real feature request into 21 constraints and 20 acceptance criteria, inventing cross-host lock races and distributed-coordination requirements nobody asked for.
- **Root cause:** Nothing in the Planner's prompt distinguished "required behavior" from "plausible-sounding elaboration," and nothing downstream checked the plan against the original ask before real work started on it.
- **Fix, round one:** An explicit traceability rule in the Planner's system prompt. **Fix, round two:** the rule alone wasn't enough — this is the same evidence that motivated replacing `scope_gate` with the human `plan_review` gate (see `DECISION_LOG.md`, 2026-09-16 – 2026-09-19).
- **Lesson:** A prompt-level instruction can reduce a failure mode; it took a structural gate (a human actually reading the plan) to close it.
- **Evidence:** Session `5601a0c16d7a`.

### 3. Build and Verify silently diverged on an interface
- **What broke:** Builder and Verifier never see each other's code by design, but a real run had them independently invent *different* shapes for the same operation — `review(id, {decision, approver, reason})` vs. `approve(id, approver, reason)`.
- **Root cause:** The contract never pinned the exact function signature down when both sides needed to converge on one, leaving each side to guess independently.
- **Fix:** "EXACT INTERFACES ARE MANDATORY WHEREVER THEY MATTER" — the Planner must specify literal function signatures whenever both the Builder and Verifier depend on the same one.
- **Lesson:** Independence between two roles is a feature until they need to agree on something neither one controls alone — that agreement has to be handed to them, not assumed.
- **Evidence:** Session `5601a0c16d7a`.

### 4. Windows subprocess handling: npm resolution, encoding, and a 60-second timeout that hung for 89 minutes
- **What broke:** `subprocess.run(["npm", ...])` couldn't resolve `npm.cmd` without a shell; `text=True` silently used the OS codepage and crashed on real UTF-8 output. Worst of all: on Windows, `npm test` spawns `node.exe` as a grandchild that survives a timeout's kill signal and keeps stdout/stderr pipes open — a "60-second timeout" hung for **89 minutes** before anyone caught it.
- **Root cause:** `subprocess.run(..., timeout=N)` only kills the immediate child process it started, not the process tree beneath it.
- **Fix:** `shutil.which()` resolution, explicit UTF-8 decoding, and a real process-tree kill (`taskkill /T /F` on Windows) driven by a polling wait loop instead of a single blocking `communicate(timeout=)`.
- **Lesson:** A timeout parameter is a promise about the process you started, not the processes it starts — on Windows especially, that gap is real and silent until something hangs long enough to notice.
- **Evidence:** This was the incident that opened the whole engagement — diagnosed directly, before structured per-session logging existed in its current form.

### 5. The Verifier's own retries accumulated instead of converging
- **What broke:** The Builder gets shown the real, current state of the project before every retry; the Verifier didn't — so a retry invented a new test filename instead of fixing the old one. Four abandoned generations of a test suite, each spinning up its own mock server, ended up running *simultaneously* under Node's default test discovery. A "60-second" test run genuinely could not finish even at 180 seconds.
- **Root cause:** Asymmetric context — one role's retries were grounded in real disk state, the other's weren't.
- **Fix:** Gave `verify` the same current-file visibility `build` already had, and made `test/` writes fully replace the directory instead of adding to it — a retry's response is now the complete, authoritative state of its own test suite.
- **Lesson:** If one role's retry loop is grounded in reality and another's isn't, the ungrounded one will drift, and the failure mode looks like flakiness until you check what each retry actually saw.
- **Evidence:** Session `5601a0c16d7a`; `execution.apply_files_replacing_directory`.

### 6. No request timeout — a call once hung 13+ minutes with no ceiling in sight
- **What broke:** The SDK's own default timeout (roughly 10 minutes, times its own internal retries) meant a genuinely hung network call could stall the whole harness for over 20 minutes before anyone could be sure it wasn't just slow.
- **Root cause:** No explicit timeout was ever set — the harness inherited whatever the SDK defaulted to.
- **Fix:** An explicit 300-second request ceiling, chosen with real headroom above every observed real call's duration.
- **Lesson:** An SDK's default timeout is tuned for the SDK author's assumptions, not yours — it needs to be set explicitly, not inherited.
- **Evidence:** `llm_client.py`'s `REQUEST_TIMEOUT_SECONDS`.

### 7. A run that was "cancelled" kept running
- **What broke:** The harness had no real cancel mechanism — closing a client didn't stop server-side execution. A mistakenly-submitted run kept executing after being believed stopped, and overwrote a proven, live-verified test suite. Recovered from durable session state, but it was close.
- **Root cause:** "Cancel" existed only as a client-side idea, never checked anywhere in the actual execution path.
- **Fix:** Real cooperative cancellation (`runtime.py`), checked at the walker's node boundary, inside the LLM streaming loop chunk-by-chunk, and inside the test runner's wait loop — proven live in two later runs, once mid-streaming-call (session `5dc049bfb535`) and again mid-`verify` (session `1da77212bd9f`, 2026-09-22).
- **Lesson:** Durable persistence is what actually saved this incident, not process discipline — but "recovered by luck" isn't a control, so cancellation had to become real.
- **Evidence:** [`evidence/1da77212bd9f.json`](../evidence/1da77212bd9f.json), [`evidence/1da77212bd9f.events.jsonl`](../evidence/1da77212bd9f.events.jsonl).

### 8. No secrets boundary, and no way to tell a quota problem from a bad implementation
- **What broke:** Two structural gaps found in the same review pass: every role sent full file contents and the raw feature request to the model with zero scanning, and every inference-layer failure — a real rate limit, a blocked model, a genuine bug — surfaced as the same generic error string.
- **Root cause:** No redaction chokepoint existed anywhere in the call path; no exception classification existed beyond a generic catch.
- **Fix:** `redaction.py` scrubs credential-shaped content before every call (proven live — a real API call echoed back only the redaction placeholder, never the planted secret). `llm_client.InferenceError` and its subclasses (`ModelBlocked` / `AllowanceExhausted` / `CapacityInsufficient`) now classify provider failures from the real SDK exception types.
- **Lesson:** "We probably don't send secrets" is not a control — only an explicit, single-chokepoint scan is.
- **Evidence:** `redaction.py`, `llm_client.py`'s `_classify()`.

### 9. The test suite had been quietly polluting the real, shared memory file
- **What broke:** Adding recurrence-tracking to the Calibrator's rolling patterns revealed something the old, duller dedup logic had been masking: the test suite had been writing to the *real* `AGENTS.md` the whole time. One pattern showed `seen=22` — 22 silent writes from test runs, invisible until a better counter made the number visible.
- **Root cause:** The autouse test-isolation fixture redirected the workspace and sessions directories but never redirected `memory.MEMORY_PATH`.
- **Fix:** Isolated `memory.MEMORY_PATH` in the autouse fixture, and cleaned the real file back to its legitimate history.
- **Lesson:** Test isolation has to be checked per side-effecting resource, explicitly — a fixture that isolates three of four shared files looks complete until a sharper instrument (the new recurrence counter) reveals the fourth.
- **Evidence:** `tests/conftest.py`'s `isolated_dirs` fixture.

### 10. A live, unscripted Builder wrote its own test file
- **What broke:** The Builder is told it never *sees* test code — but nothing stopped it from *writing* one. A real run had it return a self-authored test alongside its implementation. It happened to be overwritten by the Verifier's own file through a filename coincidence — a Builder grading its own work was one different filename away from actually happening.
- **Root cause:** The independence rule lived only in the prompt, never enforced in code.
- **Fix:** `_handle_build` now structurally strips any `test/`-path file from a Builder's output — from disk *and* from the persisted record — before Arbiter/Reviewer ever see it.
- **Lesson:** A rule that matters for correctness belongs in code, not just in the instructions an LLM might or might not follow under pressure.
- **Evidence:** Session `5dc049bfb535`.

### 11. A GitHub token mistake cost two debugging rounds it didn't need to
- **What broke:** Fine-grained GitHub PATs don't have a separate "Releases" permission — it's bundled under "Contents," which wasn't known going in. Then a token thought to be correctly scoped turned out to still be read-only, only discovered when a real `publish` call failed.
- **Root cause:** No pre-flight check existed for a new external credential before code was written to depend on it.
- **Fix:** `tools/check_github_token.py` — a standalone pre-flight check for exactly this, run before any integration code gets written against a new credential.
- **Lesson:** A new external integration should be checked before code is written around it, not after a failure reveals a scoping mistake — the same principle `llm_client._get_client()` already applied to `FACTORY_API_KEY`.
- **Evidence:** `tools/check_github_token.py`.

---

### 12. A project-scaffolding gap left pre-seeded projects unable to ever pass `test_gate`
- **What broke:** A project directory seeded with real starter files (e.g. `.env.example`) *before* a run was submitted never got `package.json` scaffolded, because the old check keyed off "does this directory already exist," not "does it already have a package.json." `test_gate` was doomed from the very first attempt.
- **Root cause:** `_resolve_project_path`'s scaffolding condition checked directory existence, not the actual file it needed to guarantee.
- **Fix:** Scaffold on the absence of `package.json` itself, regardless of whether the directory pre-existed.
- **Lesson:** Scaffolding should key off the specific precondition it's trying to guarantee, not a proxy for it.
- **Evidence:** Session `feb70199ccfd` (Config-loader).

### 13. Five consecutive Shipyard sessions each silently erased the test suite the one before it had written
- **What broke:** Every feature session against `release-manager-review-ui`'s test file fully replaced it with only that session's own new-feature tests, dropping everything before it — CLI action routes and audit-trail coverage from the original build, then Basic Auth, then `LISTEN_HOST`/`PORT`, then evidence routes, each erased in turn as the next feature landed. Coverage went from 56 tests down to 8 before anyone noticed.
- **Root cause:** Shipyard's full-replace-on-write semantics for `test/` (a deliberate fix for failure #5 above) meant each Verifier run was only as complete as what its own contract, plus whatever it happened to be shown, called for — and neither `review` nor `review_arbiter` checked "did we lose coverage the contract didn't mention," only "does this satisfy the current contract."
- **Fix:** One hand-written, consolidated test file restoring the full cumulative history in a single pass — deliberately breaking the "everything built through Shipyard" chain for this one file, after five consecutive real-but-narrow automated fixes and ~179K tokens spent on the same file's test suite in one session.
- **Lesson:** A design that fixes one real failure (test accumulation) can create the conditions for a different one (silent coverage loss) — and the gate that would catch it needs to check for regression against history, not just conformance to the current ask.
- **Evidence:** Session `ebbb35571d8f`; git history of `release-manager-review-ui/test/server.test.js` in the [Release-Manager repo](https://github.com/TalhaUsman5/Release-Manager/commits/main/release-manager-review-ui/test/server.test.js).

### 14. A real deploy-blocking regression: a fix that was built correctly but never actually shipped
- **What broke:** `server.js`'s origin-scheme detection needed to trust `X-Forwarded-Proto` to work behind Render's TLS-terminating proxy. The fix was built correctly through Shipyard (session `618077136514`) — but was never copied from the Shipyard workspace into the deployed repo, so the live site kept rejecting every request with "Cross-origin action rejected" after deploy.
- **Root cause:** A process gap, not a code gap — the build output existed and was correct; it just never got synced to the repo that actually gets deployed.
- **Fix:** Copied the already-correct fix over, verified live against the real hosted URL.
- **Lesson:** "Built and tested" isn't the same claim as "shipped" — a build-then-deploy workflow across two repos needs the sync step checked explicitly, not assumed.
- **Evidence:** Session `618077136514`.

### 15. An auth regression narrowed origin validation to POST-only, unnoticed until live testing
- **What broke:** While adding Basic Auth, origin/`PUBLIC_ORIGIN` validation was restructured to only run for POST requests — leaving every GET route (the status page, audit history) reachable from any `Host` header at all, not just the configured one.
- **Root cause:** The refactor that added the auth gate moved the origin check inside an `if (request.method === 'POST')` block without an explicit instruction to keep it unconditional.
- **Fix:** Restored origin validation to run for every request, with Basic Auth as a separate, first gate ahead of it — verified live with a spoofed `Host` header before and after the fix.
- **Lesson:** A security check's scope has to be stated explicitly in the contract ("every route," not just "the routes I'm thinking about right now") — silence gets filled with a narrower assumption.
- **Evidence:** Session `247a94cabb44`.

### 16. A newly-added worktree write-fence rejected legitimate evidence-serving code
- **What broke:** A feature request asking Shipyard's Build to serve real files from a sibling `evidence/` directory caused the Builder to try writing into that directory — which Shipyard's own path-escape guard correctly blocked, since it's outside the project's own root.
- **Root cause:** My own staging mistake — the referenced evidence files hadn't actually been placed at the real sibling location the code needed to read from, so the Builder tried to create them there itself.
- **Fix:** Staged the real evidence files at the correct sibling location before resubmitting; the guard was working exactly as intended and needed no change.
- **Lesson:** When a contract references an external fixture, the fixture has to genuinely exist where the contract says it does — the write-fence rejecting an attempt to fill that gap was the system behaving correctly, not a bug to route around.
- **Evidence:** Session `ff5c91885676` (rejected attempt); session `24b148412d5f` (corrected, successful retry).

### 17. Git worktrees broke a project's own reference to a real sibling project
- **What broke:** Placing worktrees one level deeper than project directories (`workspace/.worktrees/<project>__<session>`) silently broke any project's own code that resolves *another* project as a relative sibling — exactly what `release-manager-review-ui`'s `server.js` does to find its CLI (`path.resolve(__dirname, '../release-manager-v2')`).
- **Root cause:** A worktree's `..` no longer resolved to `workspace/` itself, only to the (nonexistent) sibling of the nested `.worktrees/` folder.
- **Fix:** Made a worktree a true top-level sibling of every other real project directory, matching what the canonical directory itself would resolve to.
- **Lesson:** An isolation mechanism has to preserve every path assumption the isolated code already depends on, not just the ones it was designed around — found by diagnosing a plan *before* approving it, not after a wasted run.
- **Evidence:** Regression test in [`tests/test_worktrees.py`](../tests/test_worktrees.py); diagnosed during review of session `86582c7ff3a2`'s plan, before build ran.

### 18. A structural chicken-and-egg: testing a fix that a sibling project can't yet see
- **What broke:** The approver-allowlist feature needed an integration test that spawns the real review UI pointed at the CLI's new (unmerged) fix — but a project's own worktree can only ever reference a sibling project's last-*merged* code, by definition. The Verifier had no way to resolve this from inside its own sandbox, and repeatedly hit it across retries until the shared `test_retry` budget ran out.
- **Root cause:** `server.js`'s CLI working directory was hardcoded to the real sibling path, with no way to point it at an in-progress checkout instead.
- **Fix:** Added an optional `RELEASE_MANAGER_CLI_CWD` override to `server.js` (same safe, default-preserving pattern as `LISTEN_HOST`/`RELEASE_MANAGER_STATE_FILE`), which is what let the missing test actually exercise the real fix. Finished by hand once the budget was legitimately exhausted on a problem code changes had to fix, not another test-writing attempt.
- **Lesson:** `review_arbiter` correctly diagnosed this as a real, in-scope gap twice rather than misclassifying it as scope creep — but diagnosing correctly doesn't create the missing capability. Some fixes need a small, deliberate seam added to production code specifically so the thing can be tested at all.
- **Evidence:** Session `86582c7ff3a2`; [`RELEASE_APPROVERS` commit](https://github.com/TalhaUsman5/Release-Manager/commit/1b82422) in the Release-Manager repo.
