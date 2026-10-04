# The LM Studio backend is a direct in-process ACP adapter over the server's own OpenAI-compatible wire, not a spawned harness

Decided by: pending — a maintainer's on-record restatement on the pull request
that adds this entry is required before this file merges (see "Why")
Date: 2026-10-04

## Decision

A local LM Studio server is reached through an adapter Kiro Crew owns and runs
in its own process (`kiro_crew.acp.lmstudio_server`, registered as
`ACP_BACKEND_LMSTUDIO = "lmstudio"`), which translates ACP onto the server's
OpenAI-compatible wire, advertises the models the server is actually serving,
and pins a model over the wire with `session/set_model` against that live
catalog — rather than by installing or spawning a harness.

## Why

- The route considered instead was a harness: write an agent document, spawn a
  foreign process, and mirror its configuration. LM Studio serves one
  OpenAI-compatible endpoint and owns its own model load/unload; there is no CLI
  to mirror and no agent document to project, so a harness would exist only to
  be configured. A direct adapter is the smallest thing that makes the models
  reachable.
- The permission model is unchanged. The adapter executes Crew's own built-in
  tools (`bash`, `read_file`, `write_file`) and every bridged MCP call only after
  the host gate answers `session/request_permission`, so no second approval path
  is introduced and nothing is delegated to a foreign host.
- Reading the model list from the live server rather than from a list written
  into this build is what makes the picker show real context windows: a loaded
  model reports its active `context_length`, so the window the adapter budgets
  its prompt against is the window actually in force.
- This entry is prepared for the pull request that adds the adapter. Its
  `Decided by` and `Evidence` lines are deliberately not filled in by an agent:
  this directory records decisions a maintainer made, and a maintainer restates
  this one on the pull request before it merges.

## Evidence

- The pull request that adds the adapter and carries this entry — link to be
  recorded here with the maintainer's restatement when it is opened.
- The feature request describing the backend family (`kirodotdev/KiroCrew`
  issue) — link to be recorded once it is filed.
