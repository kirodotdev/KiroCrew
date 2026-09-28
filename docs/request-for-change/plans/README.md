# Implementation Plans

Dated, task-by-task execution plans for documents in
[../](../README.md). A plan is the *how*; its RFC stays the *what* and the *why*.
Each plan names its RFC as its spec, so the two are read together.

Checkbox state is a progress record, not a status: read the RFC's `status` for
that, and read the **State** column here for whether a plan is being worked.

| Plan | Spec | State |
|---|---|---|
| [2026-08-22-durable-run-coordinator.md](2026-08-22-durable-run-coordinator.md) | [rfc-durable-run-coordinator.md](../rfc-durable-run-coordinator.md) | **Obsolete.** 0 of 52 checklist steps are done. The RFC is superseded by `rfc-overload-resilience.md`; its durable store now ships as `src/kiro_crew/taskq/`. |
| [2026-08-27-agentcore-identity-gateway.md](2026-08-27-agentcore-identity-gateway.md) | [rfc-agentcore-identity-gateway.md](../rfc-agentcore-identity-gateway.md) | **Partial on main.** 11 of 29 checklist steps are marked done, but current code contains only the AWS-free core seam (`platform/agentcore_schema.py`, `AgentIdentityProvider`, public Default, and governance row); later AWS/IAM/Gateway work is absent. |

Indexed from [../README.md](../README.md).
