# Shared conventions

Cross-cutting conventions every module obeys. A module spec should reference these
rather than restating them.

| Document | Covers |
|---|---|
| [code-style.md](code-style.md) | Where each constant and limit lives, comment style, and the lint rules you will trip. |
| [error-handling.md](error-handling.md) | Exception boundaries, retries, and user-facing failure text. |
| [testing-conventions.md](testing-conventions.md) | Test patterns, the four flake classes and the one correct fix for each, and how to keep the suite fast. |
| [injected-messages.md](injected-messages.md) | The envelopes automation injects into a session (cron, subagent, auto-nudge) and how to treat them. |
