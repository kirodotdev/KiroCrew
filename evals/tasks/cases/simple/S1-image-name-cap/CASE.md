# S1: a long image name keeps its extension when it is cut

Tier: simple. Source: Kiro Crew PR #36699 (merged 2026-11).

## Where it comes from

`chat_attachments._safe_name` cut a stored chat-image name to its first 64
characters, so a dashboard upload of a default macOS screenshot lost its `.png`
suffix and stopped reading as an image. The merged fix cuts the stem instead and
keeps the extension.

## How the case is built

`base/` holds the function and its constants as they stood before the fix, in a
small module of its own (`attach_names.py`), with a few visible tests. It is a
cut-down copy, not the whole repository, so the case builds offline in a second.
`ref/` holds the same module after the merged change.

## The hidden check

`hidden/check.py` judges origin `trunk`: a long image name keeps its extension
(any case) within the cap; names at or under the cap and non-image names are
unchanged; the visible tests still pass. It fails on `base/` and passes on
`ref/`.

## Traps

- Cutting the whole name and re-appending the extension past the cap.
- Changing the result for non-image names or names under the cap.
- Lower-casing the extension.
