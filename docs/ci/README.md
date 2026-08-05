# CI and review gates

Everything that gates a pull request.

| Document | Covers |
|---|---|
| [ci-and-reviews.md](ci-and-reviews.md) | The CI jobs, the AI review workflows, and the aggregate readiness status. |
| [e2e-gate.md](e2e-gate.md) | The offline browser E2E gate (`python setup.py test_e2e`). |
| [i18n-gates.md](i18n-gates.md) | The i18n gate chain: what fails, what only reports, and the ratchet rule. |
| [prepare-pr-portability.md](prepare-pr-portability.md) | How the `prepare-pr` skill resolves a per-project profile. |

The local gate you run before committing is in [../../AGENTS.md](../../AGENTS.md);
test determinism and speed are in
[../system-specs/common/testing-conventions.md](../system-specs/common/testing-conventions.md).
