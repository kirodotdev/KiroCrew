# System specifications

**These are change-control contracts.** Read the spec for the subsystem you are
touching before changing it, and update the spec in the SAME commit when you change
what it documents. A spec that disagrees with the code is worse than no spec, because
readers still trust it.

## The two tiers

| Tier | What belongs there |
|---|---|
| [modules/](modules/README.md) | One spec per subsystem — a backend module, a built-in app, or a user-visible surface. Every change lands here, and it is the on-demand load target for an AI session. |
| [common/](common/README.md) | Cross-cutting conventions every module obeys: code style, error handling, testing, injected messages. |

There is no separate feature tier. A membership rule that distinguishes "a
user-visible feature spanning several modules" from "a subsystem" cannot be applied
consistently, and a directory whose rule is not obeyed stops being a routing signal.
One flat spec tier keeps `modules/README.md` the single complete index an agent
loads from.

Two root-level specs sit outside the tiers:

- [oss-fork-boundaries.md](oss-fork-boundaries.md) — what this public fork must never
  re-introduce, the modules that are deliberately inert, and the fork's intentional UX
  divergences.
- [post-launch-removals.md](post-launch-removals.md) — a cross-module ledger of what
  was deliberately removed and why it must not come back.

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
  reader can verify it instead of trusting prose. Name symbols, never `file.py:NNN` —
  a line number rots on the next refactor while the claim stays true, which is the
  most common way a spec starts lying.
- Do not restate a number the code already pins. Name the test that pins it, because
  a copied constant goes stale silently.
- Add the spec to its tier's `README.md` in the same commit.
  `../../scripts/docs-lint.sh` fails the build otherwise.
