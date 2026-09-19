# Software Factory Memory

## Domain Rules (Permanent)
- Keep changes scoped to the files the contract names.
- Prefer explicit error handling over silent failures.

## Recent Patterns (Rolling)
- Validate all contract-required fields in external API data before persisting derived artifacts.
- Do not turn unspecified operational details into mandatory contract requirements; distinguish required behavior from implementation defaults.
- When testing browser code with a custom DOM fixture, implement standard APIs the app may invoke (such as focus, blur, and activeElement) before classifying resulting TypeErrors as implementation bugs.
- check exact output strings
