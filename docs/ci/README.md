# CI and review gates

Everything that gates a pull request.

| Document | Covers |
|---|---|
| [ci-and-reviews.md](ci-and-reviews.md) | The Fast Gate barrier, the CI jobs, the AI review workflows, and the aggregate readiness status. |
| [denial-differential.md](denial-differential.md) | The denial-differential gate: how a deny-rule change is proved not to newly refuse a golden path, why its corpus is read from the base ref, and what a green does not cover. |
| [e2e-gate.md](e2e-gate.md) | The offline browser E2E gate (`python setup.py test_e2e`). |
| [harness-parity-gate.md](harness-parity-gate.md) | The added-line gate keeping the Kiro harness first-class: the six rules, and why it reports rather than enforces whole-tree. |
| [i18n-gates.md](i18n-gates.md) | The i18n gate chain: what fails, what only reports, and the ratchet rule. |

The local gate you run before committing is in [../../AGENTS.md](../../AGENTS.md);
test determinism and speed are in
[../system-specs/common/testing-conventions.md](../system-specs/common/testing-conventions.md).
