# A `.kirobot` import skips the empty folder entries an archiver writes for its two folders

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

Importing a `.kirobot`, `.crewjam` or `.crewmate` file skips an empty folder entry named exactly `skills/`, `crons/` or `skills/<name>/` with a name the reader allows; every other folder entry, and any folder entry that declares bytes, is still refused, and every folder entry still counts toward the 64-entry cap.

## Why

- `zip -r`, Windows `tar.exe` (bsdtar), 7-Zip and Python's `shutil.make_archive` add these entries when they zip a folder, so a file someone unzipped and zipped again was refused although every file in it sits where the format allows.
- A skipped entry is never read, so it adds nothing to what the import shows or writes; every file entry still goes through the same reader checks.
- The refusal wording stays as it is: the import dialog shows its own line for each refusal code.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39747#issuecomment-7092974858 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/39747 -- the report and the reproduction with the tools that write these entries.
