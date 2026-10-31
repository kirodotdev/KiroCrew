# The crewmate share file is a `.crewjam` file; old `.crewmate` files still import

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-31
Supersedes: 2026-10-15-the-standing-teammate-agent-is-called-a-mate.md

## Decision

Export writes the share file as `<name>.crewjam`, with `"format": "kirocrew.crewjam"` and the manifest entry `crewjam.json`; import keeps accepting the old `.crewmate` / `kirocrew.crewmate` / `crewmate.json` form through the same reader checks.

## Why

- The maintainer decided the share file follows the Crew Jam rename.
- This supersedes only the data-file-name part of the 2026-10-15 entry ("data-file names keep the `crewmate` spelling") for the share file. Code identifiers, i18n keys, API paths and every other data-file name keep the `crewmate` spelling.
- The Jam entry proposed in #26452 (`2026-10-30-the-standing-agent-is-called-a-jam.md`) keeps the `.crewmate` share-file extension; this entry changes only that point and leaves the rest of it as written.
- Files people already shared must keep importing, so the reader takes both forms.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/26601#issuecomment-6548626351 -- the maintainer's decision, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/26601 -- the change, with the matching amendment to `docs/request-for-change/rfc-crewmate-share.md` §5.2.
