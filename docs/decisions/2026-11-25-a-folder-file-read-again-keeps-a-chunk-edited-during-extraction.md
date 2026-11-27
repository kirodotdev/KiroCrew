# A folder file read again keeps a chunk the user edited or archived while its new version was extracted

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

When an item of a folder file's indexed group is edited or archived while that file's new version is being extracted, the read keeps the old group whole, as the user left it, drops its own new chunks, and leaves the file's new text to be read on its next change; this entry covers the folder watcher's file reads only (a changed file, a retried or interrupted one, a re-read the scan selects), and decides nothing about the other re-read paths or about edits made before extraction starts.

## Why

- The issue reported that a chunk edited or archived during a folder file's re-read was deleted with the old group, so the edit was lost and an archived chunk came back as active text, and left open what to keep.
- The edit is newer than the read.
- Every chunk of a document carries the whole document's hash, which the duplicate gate and the de-duplication sweep rely on, so keeping only some old chunks beside the new ones would leave one document carrying two versions' hashes.
- Carrying the edited fields onto the new chunks has no stable old-to-new mapping once a change moves the chunk boundaries.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/42056#issuecomment-7168763187
- Issue: https://github.com/kirodotdev/KiroCrew/issues/42056
