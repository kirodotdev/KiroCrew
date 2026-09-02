# Design proposals

Proposals for changes large enough to be agreed before they are built. A document
here describes intent and the decisions taken; the shipped behaviour lives in
[../system-specs/](../system-specs/README.md), which is the source of truth once a
proposal lands.

| Document | Covers |
|---|---|
| [pipeline-conductor.md](pipeline-conductor.md) | A dedicated fleet-orchestrator agent + harness skill for the issue→PR pipelines: probe/verify/intervene/adjudicate loop, resource-posture flow control, per-item credit budgets, and the PipelineSpec template seams for running the same conductor on any repository and campaign type. |
| [playwright-cli-migration.md](playwright-cli-migration.md) | Moving browsing onto `playwright-cli`: the capability model, the install flow, snapshot retention, and the dashboard surface. |

Indexed from [../README.md](../README.md).
