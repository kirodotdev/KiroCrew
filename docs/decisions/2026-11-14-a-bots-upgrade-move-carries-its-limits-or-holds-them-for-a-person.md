# When the upgrade moves a Bot, every limit its old agent set is carried into its file when it applies exactly, and any other one makes the actions it covers ask a person

Decided by: Ray (project owner, @buluoray)
Date: 2026-11-14
Supersedes: 2026-10-28-every-crewmate-on-a-user-level-agent-moves-to-the-conductor.md (only its rule that a restriction the crewmate file cannot carry moves as it is and is named in the notice)
Supersedes: 2026-11-02-a-crewmates-worker-carries-its-template-policy.md (only its line that the migration file carries no hooks, permissions or tool settings)

## Decision

When the upgrade moves a Bot onto its own file, every limit its old agent set (each `hooks` entry, each `permissions` rule that denies, asks or excludes, each `toolsSettings` entry) is carried into the Bot's file when its worker can apply it exactly; a Bot never fails to start because of this, and a limit that cannot be carried exactly, or every limit of a Bot whose worker runs on a backend that applies none of them, is not written in a way that stops the worker but makes any action it would have covered ask a person, never approved on its own under `auto` approval, Trust, YOLO, a trusted pattern or a hook grant alike; every other action behaves as before; the user is told once, in the Bot's own chat and on the dashboard, which limit did not apply, with a way to open that Bot; and a Bot an earlier release already moved without its limits gets the same treatment at the next gateway start.

The 2026-10-28 entry's rules on losses, gains, unreadable agents, shadowed ids and minted identities stand. Allow-only `permissions` rules stay grants and are compared like every other grant.

## Why

Moving a Bot without its deny rules removed the confinement its user set. Its worker asked for every action, and an `auto` approval mode (crons) approved writes outside its folder, shell, web and sub-agent calls its template denied (Slack report, 2026-11-11).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/35011#issuecomment-6874456761 -- the decision, as repo owner.
- https://github.com/kirodotdev/KiroCrew/pull/35011 -- the change that implements it.
