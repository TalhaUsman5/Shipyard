# Roadmap

Every item below is written as a falsifiable outcome — something you can
check and say yes or no to — not a direction like "improve reliability."
If an item can't be checked off with a specific test, log, or artifact,
it's written wrong.

## 30-day product roadmap

- [ ] `AUDIT_LOG_PATH` env-var override ships for `release-manager-review-ui`, mirroring `RELEASE_MANAGER_STATE_FILE`. **Check:** deploy to Render, trigger a redeploy, confirm `audit-log.jsonl` entries from before the redeploy are still present after.
- [ ] The pending `build_retry`/`review_retry` budget change (4/6 → 2/2) is either committed or explicitly reverted — not left uncommitted. **Check:** `git status` on Shipyard shows no pending changes to `graph.py`/`AGENTS.md`/`README.md`/`tests/`.
- [ ] Release Manager's review console gets Basic Auth credential rotation without a redeploy. **Check:** update `REVIEW_UI_USERNAME`/`REVIEW_UI_PASSWORD` in Render's dashboard, confirm the running instance picks up the new credentials without a manual restart or redeploy.
- [ ] A second real GitHub repo (beyond `shipyard-rm-throwaway`) is exercised end-to-end through Release Manager — configure, scan, prepare, approve, publish. **Check:** a real published release exists on a second, independently-owned repo, verified against the GitHub API.
- [ ] Shipyard's stuck-run detector (`stuck_run_warning`) fires a real alert (not just a dashboard banner) — email, Slack, or equivalent — when a session sits in `running` past a defined threshold. **Check:** deliberately stall a session past that threshold, confirm the alert actually arrives.
- [ ] Every Shipyard run's git worktree lifecycle (create → merge → cleanup) is exercised against at least 10 consecutive real sessions with zero manual intervention required. **Check:** `git worktree list` on each affected project shows only the canonical directory between sessions, with no leftover branches or worktree registrations.

## 90-day technical + commercial roadmap

**Technical**
- [ ] Shipyard supports at least one non-Node.js target project (e.g. Python) end-to-end — Builder, Verifier, and `test_gate` all work against a real `pytest`-based project, not just `node --test`. **Check:** a real feature request completes successfully against a real Python project with a passing test suite.
- [ ] A property-based or fuzz-testing pass exists for at least one high-risk surface (e.g. `redaction.py`'s pattern matching, or `release-manager.js`'s state-transition logic). **Check:** a fuzzing/property-test run is committed and passing in CI, with at least one real bug it caught documented.
- [ ] Shipyard's session data becomes queryable without hand-written `python -c` one-liners — a real `GET /sessions?query=...` or equivalent. **Check:** the same investigations done manually throughout this project (e.g. "find every session with a cancel-then-resume-then-complete lifecycle") are answerable with one HTTP call.
- [ ] `factory.py` runs behind real authentication (it's currently unauthenticated, unlike Release Manager). **Check:** an unauthenticated request to `POST /run` returns 401, not a started session.

**Commercial**
- [ ] Release Manager has at least one external (non-Herald) user who has published a real release through it, unprompted by this project's own team. **Check:** a real GitHub release exists, published via Release Manager, on a repo Herald doesn't own.
- [ ] Shipyard's per-run token cost is published as a real, load-bearing number in any pitch — not an estimate. **Check:** the number cited traces to `sessions.total_tokens()` output from real sessions, not a projection.
- [ ] A second product beyond Release Manager has been built through Shipyard, all the way to a deployed, externally-reachable instance. **Check:** a live URL exists, and its own `evidence/` folder shows real Shipyard session records, same standard as Release Manager.

## Recommended priorities if Herald continues

In order, with the reasoning for the order:

1. **Land the `AUDIT_LOG_PATH` fix before anything else.** It's the single remaining gap in Release Manager's "fully working, live" acceptance bar (`evidence` requirements named this explicitly) — everything else on the 30-day list is genuinely new work; this one is closing a documented, known, already-scoped gap.
2. **Get a second real external user or repo through Release Manager before adding new Shipyard capability.** The strongest evidence this project can produce right now is "a stranger used this for something real," not another internal feature. Commercial validation compounds; internal feature depth doesn't prove the product works for anyone else.
3. **Only after that, expand Shipyard to a second language target.** It's the highest-leverage technical proof (the harness isn't Node.js-specific by accident, it's just never been tested against anything else) but it's also the largest single chunk of new work on this list — sequence it after the cheaper, higher-signal commercial proof above.
4. **Authenticate `factory.py`.** Currently the one real asymmetry between the harness and the product it built: Release Manager learned this lesson live (Basic Auth, origin validation, an allowlist) and Shipyard's own control plane hasn't applied the same lesson to itself yet.
5. **Everything else on the 90-day list is genuinely parallelizable** once 1–4 are underway — none of it blocks the others.
