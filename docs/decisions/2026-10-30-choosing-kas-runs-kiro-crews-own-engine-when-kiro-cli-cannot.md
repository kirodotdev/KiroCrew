# Choosing KAS runs Kiro Crew's own engine and sign-in when kiro-cli's relay cannot run

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-30

## Decision

Choosing KAS means Kiro Crew's own engine and Kiro Crew's own sign-in: when KAS is the backend and kiro-cli's relay cannot run (no kiro-cli, or no usable kiro-cli login), the spawn takes the bundle route, the gateway installs a missing pinned engine (in the gateway process, never in the sandbox, one install at a time, the download and an offline error with the retry command shown in the dashboard) and continues, every KAS prompt names the real route and points to the Kiro sign-in rather than `kiro-cli login`, and the switch is logged and shown in the UI and `kirocrew doctor`, never silent; a stored Kiro CLI pick (`agent.acp_backend: "kiro"`) is not moved.

## Why

- A user who chose KAS and signed in with Kiro Crew's own Kiro sign-in was still sent to `kiro-cli login`: the route was chosen by `agent.kas_bundle` alone (default off), so with no kiro-cli the spawn failed before the vault was read, and with a kiro-cli launcher that has no `kiro-cli-chat` beside it the launcher refused on its own login first.
- This replaces the stance written in `acp/harness/kas.py` that a missing engine is a refusal and the route is never switched, "so a kiro-cli problem never looks like a bundle problem". That stance had no entry in this directory. The switch stays visible instead of silent.
- A stored Kiro CLI pick runs on KAS only because kiro-cli itself offers v3 (`2026-10-28-a-kiro-cli-choice-runs-on-kas.md`), so it keeps kiro-cli.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26385 -- the issue stating the bug and the decision.
- https://github.com/kirodotdev/KiroCrew/pull/26393#issuecomment-6533143760 -- Zezhen's owner decision restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/26393 -- the pull request that adds this entry and implements it.
