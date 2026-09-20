# Software Factory Memory

## Domain Rules (Permanent)
- Keep changes scoped to the files the contract names.
- Prefer explicit error handling over silent failures.

## Recent Patterns (Rolling)
- Validate all contract-required fields in external API data before persisting derived artifacts.
- Do not turn unspecified operational details into mandatory contract requirements; distinguish required behavior from implementation defaults.
- When testing browser code with a custom DOM fixture, implement standard APIs the app may invoke (such as focus, blur, and activeElement) before classifying resulting TypeErrors as implementation bugs.
- check exact output strings
- For UI wrappers around an existing CLI, keep workflow logic in the CLI and invoke it with an argument array rather than a shell command string; this preserves exact arguments and prevents shell interpretation of user input. [meta: seen=1, source=release-manager-review-ui]
- Model subprocess outcomes uniformly, including stdout, stderr, nonzero exit codes, and launch errors, then surface and audit the same actual result without implying success on failure. [meta: seen=1, source=release-manager-review-ui]
- Integration-test CLI wrappers at the real subprocess boundary with a temporary executable fixture that records argv, cwd, and inherited environment; verify exactly one invocation and cover every allowed enum value plus shell-like input. [meta: seen=1, source=release-manager-review-ui]
- For append-only JSONL auditing, verify that each action preserves all existing bytes, appends exactly one newline-terminated record, and records both successful and failed execution results in the established schema. [meta: seen=1, source=release-manager-review-ui]
- When external status JSON may vary in nesting or key style, use reusable structured-value rendering and carefully normalized candidate-key lookup while preserving explicit operator-facing labels for required fields. [meta: seen=1, source=release-manager-review-ui]
- Tests that replace neighboring executables or persistent files should move originals aside, restore them in teardown, and preserve preexisting audit content so the suite is isolated and non-destructive. [meta: seen=1, source=release-manager-review-ui]
- Do not turn unspecified operational concerns—such as global stale-entry cleanup, allocation limits, asymptotic complexity, or adversarial scalability—into mandatory contract requirements; keep acceptance criteria grounded in explicitly requested observable behavior. [meta: seen=1, source=Fixed-window-rate-limiter]
- For per-key time-window state, prune only the requested key during each operation; maintaining chronological per-key logs with binary-search expiration/removal avoids unnecessary scans across unrelated keys while preserving exact boundary semantics. [meta: seen=1, source=Fixed-window-rate-limiter]
- When extending an existing module with an alternative implementation, preserve the original class, exports, validation, and observable behavior, and verify the new class is independently stateful. [meta: seen=1, source=Fixed-window-rate-limiter]
