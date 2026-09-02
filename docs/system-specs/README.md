# System specifications

**These are change-control contracts.** Read the spec for the subsystem you are
touching before changing it, and update the spec in the SAME commit when you change
what it documents. A spec that disagrees with the code is worse than no spec, because
readers still trust it.

## The three tiers

| Tier | What belongs there |
|---|---|
| [modules/](modules/README.md) | One spec per backend subsystem. Most changes land here, and it is the on-demand load target for an AI session. |
| [features/](features/README.md) | User-visible features that span several modules. A feature owned by one subsystem belongs in its module spec instead. |
| [common/](common/README.md) | Cross-cutting conventions every module obeys: code style, error handling, testing, injected messages. |

[post-launch-removals.md](post-launch-removals.md) is the one root-level spec: a
cross-module ledger of what was deliberately removed and why it must not come back.

## Related, outside this tree

- [`../architecture/`](../architecture/README.md) for how the subsystems fit
  together. Architecture docs are maps; they link here for mechanism detail.
- [`../request-for-change/`](../request-for-change/README.md) for proposals not yet
  built. Once a change ships, its behavior belongs in a spec here.
- [`../../AGENTS.md`](../../AGENTS.md) for the routing table that maps a subsystem to
  its spec.

## Writing a spec

- Describe **current** behavior in present tense. No changelog lines, no
  "previously/used to/we now" narration, no PR numbers or commit SHAs. Git holds
  history.
- State invariants and why they are load-bearing, not merely that they exist.
- Cite the code: name the module, function, or test that enforces a claim so the next
  reader can verify it instead of trusting prose.
- Do not restate a number the code already pins. Name the test that pins it, because
  a copied constant goes stale silently.
- Add the spec to its tier's `README.md` in the same commit.
  `../../scripts/docs-lint.sh` fails the build otherwise.
