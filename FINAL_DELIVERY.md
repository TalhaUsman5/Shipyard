# Final Delivery

Trial-final-day delivery index. Every requirement below maps to a specific
artifact — this file exists so grading is a table lookup, not a scavenger
hunt. All links are real and checkable; nothing here is a summary standing
in for the artifact itself.

## Requirement → artifact

| Requirement | Artifact | Link |
|---|---|---|
| Decision log (table: date / decision / options / why) | `docs/DECISION_LOG.md` | [View](docs/DECISION_LOG.md) |
| Failure log (chronological: broke / root cause / fix / lesson) | `docs/FAILURE_LOG.md` | [View](docs/FAILURE_LOG.md) |
| Handoff doc (what's where, how to run, risks, don't-touch, open threads, Loom+Runner positioning) | `docs/HANDOFF.md` | [View](docs/HANDOFF.md) |
| Roadmap (30-day, 90-day, recommended priorities — falsifiable outcomes) | `docs/ROADMAP.md` | [View](docs/ROADMAP.md) |
| Presentation deck (10–15 slides, both products, dossier style) | Published artifact | [Shipyard & Release Manager](https://claude.ai/artifact/7D2YTZPLyZsNgZt3Cf6owJ) |
| Architecture dossier (full retrospective — architecture, phase graph, 11 original problems, optimizations, learnings) | `shipyard_dossier.html` | [View](shipyard_dossier.html) |

## Repositories

| Repo | Contents | Link |
|---|---|---|
| Shipyard | The harness itself | [github.com/TalhaUsman5/Shipyard](https://github.com/TalhaUsman5/Shipyard) |
| Release Manager | The product built through it | [github.com/TalhaUsman5/Release-Manager](https://github.com/TalhaUsman5/Release-Manager) |

## Deployed product

| What | Link | Verified |
|---|---|---|
| Release Manager review console | [release-manager-review.onrender.com](https://release-manager-review.onrender.com/) | `GET /` → `401` (Basic Auth live), `GET /evidence/trace` → `200` (public evidence route live) — both checked independently at delivery time |
| Self-hosted execution evidence | [/evidence/trace](https://release-manager-review.onrender.com/evidence/trace) | Served by the deployed product itself, no credentials required |

## Test receipts

| Suite | Result | Command |
|---|---|---|
| Shipyard (`pytest`) | **158 / 158 passing**, no mocked LLM calls in real execution paths — `fake_agents`/`fake_agents_raw` fixtures stand in for the model, not for the harness logic | `pytest -q` |
| Release Manager CLI (`release-manager-v2`) | **9 / 9 passing** | `node --test` |
| Release Manager review console (`release-manager-review-ui`) | **25 / 25 passing** | `node --test` |
| **Total** | **192 / 192 passing** | — |

## Real evidence (not summaries)

| Evidence | What it proves | Link |
|---|---|---|
| `evidence/1da77212bd9f.json` + `.events.jsonl` (Shipyard) | A real interrupted-and-resumed Shipyard session, completed, with its worktree genuinely merged | [View](evidence/1da77212bd9f.json) |
| `evidence/3db140f839a8.json` + trace page (Release-Manager) | A real Shipyard session with genuine retries (`test_gap`, `implementation_gap` ×2) that built the review console's UI | [View](https://github.com/TalhaUsman5/Release-Manager/tree/main/evidence) |
| `evidence/failed-and-recovered-publish.json` (Release-Manager) | A real GitHub release (`v0.1.5`), a staged interruption reconstructed from real values, and a real `recover` reconciling against the real GitHub API | [View](https://github.com/TalhaUsman5/Release-Manager/blob/main/evidence/failed-and-recovered-publish.json) |

## Commits at time of delivery

- Shipyard: `4034ede` (2026-09-22)
- Release Manager: `f5e9286` (2026-09-22)

## Known open items (also listed in `docs/HANDOFF.md`)

- `AUDIT_LOG_PATH` override for the review console's audit log is not yet built — audit trail resets on Render redeploy.
- A drafted `graph.py` retry-budget change (4/6 → 2/2) is real, evidenced, but not yet committed to Shipyard.
- No fuzzing/property-based testing yet on either repo; no dedicated security pass on `server.js`.
