# On the opencode 1.x line, Kiro Crew's routing seed denies opencode's own `task` tool, so the model is not offered opencode's sub-agents there; Kiro Crew's own sub-agents are unchanged

Decided by: Bolin Chen (bolichen97), maintainer
Date: 2026-11-15

## Decision
On the opencode 1.x line, Kiro Crew's routing seed denies opencode's own `task` tool, so the model is not offered opencode's sub-agents there; Kiro Crew's own sub-agents are unchanged.

## Why
opencode 1.x never sends a child session's permission requests over ACP, so every `task` call hung the chat. The fix exists only upstream in v2. The deny is the one place that can stop it on 1.x, and the v2 move removes it.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/35458
- The decision comment: https://github.com/kirodotdev/KiroCrew/issues/35458#issuecomment-6892265276
