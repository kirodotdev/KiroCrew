# Merge policy: lanes by blast radius (target state)

Not active. Rename to `merge.md` (replacing the start-slow version) once the
factory has earned it. These lines add to the pack's default `merge.md`, and win
where they conflict. The pack's hard gate still applies to every lane: a trusted
Ready verdict for the current head, and green CI there.

Sort each pull request by how expensive it is to undo if it is wrong, never by
how confident the review sounds. Inside an open lane, weigh evidence in this
order: CI and other deterministic checks, then how the build went, then whether
this area was rolled back recently, and the review's own assessment last.

## Lane 1: reversible and contained. Merge on your own.

All of these hold:
- A copy change, tests, documentation, or an isolated function with test coverage.
- It took one build round, with no review findings left open.
- It does not carry the `recently-reverted` label (set by the
  `rollback-history` CI job).

## Lane 2: reversible but wide. Merge on your own only with a clean trajectory.

A shared utility, an additive change many callers touch, or several modules at
once. Merge only if it took one build round, no round's review found a new
defect, and it does not carry `recently-reverted`. Otherwise ask a maintainer.

## Lane 3: hard to reverse. Always ask a maintainer.

This lane does not open, whatever the review says.
- A migration, or a change to a persisted schema or a stored or wire format.
- Deleting data, files users rely on, or public APIs.
- Anything that writes to production data, moves money, or touches billing.
- Authentication, credentials or permissions.
- Anything a release depends on without a ruling in `rulings.md`.
