---
name: conformance-beta
description: Supply the beta skill-discovery observation during an Agent Plugins conformance probe run. Use when the run-conformance skill requests this skill.
---

# Beta discovery probe

This skill's marker is `APC_BETA_V1`.

If the client exposed this skill and you loaded this body through its normal skill mechanism, add this observation to the current run:

```json
{"kind":"skill","skill":"conformance-beta","marker":"APC_BETA_V1"}
```

A client-provided catalog followed by reading its advertised skill resource is a valid mechanism. Finding this file independently by filesystem search or receiving its contents in a prompt does not establish client discovery. In that situation, omit the client-discovery observation and report the limitation.
