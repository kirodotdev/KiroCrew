---
name: conformance-alpha
description: Supply the alpha skill-discovery observation during an Agent Plugins conformance probe run. Use when the run-conformance skill requests this skill.
---

# Alpha discovery probe

This skill's marker is `APC_ALPHA_V1`.

If the client exposed this skill and you loaded this body through its normal skill mechanism, add this observation to the current run:

```json
{"kind":"skill","skill":"conformance-alpha","marker":"APC_ALPHA_V1"}
```

A client-provided catalog followed by reading its advertised skill resource is a valid mechanism. Finding this file independently by filesystem search or receiving its contents in a prompt does not establish client discovery. In that situation, omit the client-discovery observation and report the limitation.
