# A Kiro CLI harness choice runs on Kiro Crew's KAS path whenever the installed kiro-cli offers its v3 engine

Decided by: Ray (project owner, @buluoray)
Date: 2026-10-28

## Decision

An `agent.acp_backend` stored as `"kiro"` runs through Kiro Crew's KAS path (KAS's launch, projection, permissions and compaction) once the gateway's check confirms the installed kiro-cli offers its v3 engine and while KAS is selectable, and the user is then told once in the dashboard with a one-click way to save `kas`; everywhere else (a kiro-cli without v3, a check that could not run, the KAS bundle route, a policy that denies KAS) it keeps the v2 Kiro CLI launch, and `kirocrew doctor` offers the kiro-cli update; no session is refused, and the stored value, the Kiro CLI harness, the skill-view aliases and the `cli.json` overlay stay in place.

## Why

- kiro-cli switches the default engine of a bare `kiro-cli acp` to v3 (KAS) on 2026-11-02 and removes v2 at the end of November. After the switch, a Kiro CLI choice would run the v3 engine while Kiro Crew still drives it as the v2 harness, writing skill-view aliases, the `cli.json` effort overlay and re-filtered agent specs that the v3 engine does not read.
- This replaces the rule recorded with the KAS default (#20359, harness-parity H1) that every explicit choice, Kiro CLI included, runs as itself. That rule had no entry in this directory; this entry is now the one to cite.
- Removing the Kiro CLI harness, the skill-view aliases and the `cli.json` overlay is a later change, made once kiro-cli removes v2.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/25288 -- the issue Ray filed and assigned, which states the decision.
- https://github.com/kirodotdev/KiroCrew/pull/25350#issuecomment-6482067414 -- Ray's owner decision restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/25350 -- the pull request that adds this entry and implements it.
