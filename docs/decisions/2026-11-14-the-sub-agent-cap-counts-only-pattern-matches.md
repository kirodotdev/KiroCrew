# The cap of 8 sub-agents no longer counts the agents that an agent's `availableAgents` list names outright; it counts only the agents a `*` pattern adds

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision
The cap of 8 sub-agents (`MAX_SUBAGENT_TARGETS`) no longer counts the agents that an agent's `availableAgents` list names outright; it counts only the agents a `*` pattern adds, and there is no new setting for the cap.

## Why
The cap exists because a broad `*` pattern could slow every chat start, while a list of names costs only what its author chose.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/36881
- https://github.com/kirodotdev/KiroCrew/issues/36881#issuecomment-6876174049
