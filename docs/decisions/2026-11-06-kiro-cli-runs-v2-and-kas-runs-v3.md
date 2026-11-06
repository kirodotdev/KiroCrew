# The Kiro CLI harness runs kiro-cli's V2 engine, and the KAS harness runs V3

Decided by: Ray (project owner, @buluoray), for the team
Date: 2026-11-06
Supersedes: 2026-10-28-a-kiro-cli-choice-runs-on-kas.md

## Decision

An `agent.acp_backend` stored as `"kiro"` always launches `kiro-cli acp --agent-engine v2`, whatever engines the installed kiro-cli offers and whether or not KAS is selectable; it never moves onto Kiro Crew's KAS path. The KAS harness (`"kas"`, and no choice) runs the V3 engine as before. Settings → Agent Harness says which engine each choice runs. On a kiro-cli that lists its engines without V2, a Kiro CLI session does not start: it says V2 is not available and points to Kiro Agent (KAS), and so do Settings → Agent Harness and `kirocrew doctor`. Nothing switches the user's harness silently. First-run setup is unchanged.

## Why

- The team wants each choice to mean one engine, so a person who picks Kiro CLI gets V2 and a person who picks KAS gets V3, with no hidden rerouting between them.
- kiro-cli supports V2 until the end of January. After that a bare launch would run V3 under the V2 harness, so a session that cannot get V2 is refused with the fix rather than run on the wrong engine.

## Evidence

- The pull request that adds this entry records the decision; Ray restates it there.
