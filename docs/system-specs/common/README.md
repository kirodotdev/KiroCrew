# Shared conventions

Cross-cutting conventions every module obeys. A module spec should reference these
rather than restating them.

| Document | Covers |
|---|---|
| [code-style.md](code-style.md) | Where each constant and limit lives, comment style, and the lint rules you will trip. |
| [model-selection.md](model-selection.md) | Never hardcoding a model id: the `"auto"` default, the resolver, role pins, and the entitlement predicate. |
| [platform-compat.md](platform-compat.md) | The POSIX-call shim table: which `platform_compat` helper replaces each `fcntl` / `os.kill` / `resource` call, and why. |
| [error-handling.md](error-handling.md) | Exception boundaries, retries, and user-facing failure text. |
| [testing-conventions.md](testing-conventions.md) | Test patterns, which conftest owns which isolation, the side-effect floor, the six flake classes and the one correct fix for each, and how to keep the suite fast. |
| [injected-messages.md](injected-messages.md) | The envelopes automation injects into a session (cron, subagent, auto-nudge) and how to treat them. |
