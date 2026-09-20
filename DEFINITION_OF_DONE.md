# Definition of done

A recurring cost across this project: converting already-working capability
into *provable* evidence (fresh-clone proofs, independent API verification,
live route testing) happened well after the capability existed, as a
separate retrofitted pass rather than part of building the feature. This
checklist exists so "prove it" is budgeted in from the start of a feature,
not discovered as overhead at the end.

A feature (in Shipyard itself, or in something Shipyard builds) isn't done
until:

- [ ] **It ran for real, not just in a mocked test.** Unit/integration
  tests with fakes prove the logic; they don't prove the actual external
  call, actual subprocess, or actual model behaves as assumed. At least
  one real end-to-end exercise is required before calling something done.
- [ ] **The result was independently re-checked, not just trusted from the
  tool's own report.** Re-run the delivered test suite yourself. Query the
  external system directly (GitHub's API, not just the CLI's own `status`)
  to confirm a claimed result actually landed. A "review approved" or
  "tests passed" claim from inside the system being verified isn't
  evidence on its own.
- [ ] **Both the success and failure paths were actually exercised.** A
  wrapper that's only ever seen a happy path hasn't proven it "surfaces
  the real result on failure" — that needs an actual failure to surface.
- [ ] **Secrets handling was checked, not assumed.** Before anything is
  committed or pushed: diff review for anything secret-shaped, confirm
  `.gitignore` actually excludes what it's supposed to, and confirm
  against the *remote* after pushing, not just the local working tree.
- [ ] **A fresh-environment check happened, not just "it works on my
  checkout."** Clean install, clean directory, from what's actually
  committed — not the working directory that still has yesterday's manual
  fixes sitting in it uncommitted.
- [ ] **The commit actually happened.** Working code sitting uncommitted
  for days was the single biggest avoidable risk found in this project — a
  near-miss data-loss incident happened during exactly that kind of
  window, saved by the harness's own durable persistence rather than by
  process discipline. Commit at the end of the unit of work that produced
  it, not "eventually."
- [ ] **A new external integration was checked before code was written
  around it**, not after a failure revealed a scoping/permission mistake —
  see `tools/check_github_token.py` for the concrete tool this project
  built after paying for that mistake twice.

None of this replaces genuine automated tests — it's the layer above them:
proof that the tested thing actually behaves the same way in reality.
