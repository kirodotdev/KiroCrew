# temp-screenshots/

This directory holds **PR review evidence**: screenshots and GIFs captured
while preparing a pull request, not product assets. Nothing here ships in a
package, a wheel, or the desktop app.

## Why these are committed rather than attached

This repository is private, so GitHub's `raw.githubusercontent.com` host
returns 404 for everyone and cannot serve an image embedded in a PR
description. The `user-attachments` mechanism (drag-and-drop upload in the
GitHub web UI) works, but nothing in an automated PR workflow, CI or CLI,
can produce an attachment that way.

Committing the file into the PR branch and linking it with a commit-SHA-pinned
URL sidesteps both problems, because the URL is served from the repository
itself:

```
https://github.com/<owner>/<repo>/raw/<sha>/temp-screenshots/<feature>/<name>.png
```

## Naming

`temp-screenshots/<feature>/<name>.png` (or `.gif`, `.mp4`), one subdirectory
per feature or PR.

Reference it from the PR body with the commit SHA pinned, and **re-pin the SHA
after every amend or rebase**: the pinned URL only resolves the commit it
names, so an amended commit's old URL breaks.

## Lifecycle

`.github/workflows/cleanup-temp-screenshots.yml` prunes files older than the
retention window (14 days) weekly, opening a PR because `main` is protected.
Committed blobs stay reachable in git history by design even after the tip is
pruned, so an already-published PR description's pinned URL keeps resolving:
pruning the tip never breaks a past PR's images.

**Authors should not delete their own files before merge, and reviewers
should not ask them to.** The scheduled cleanup job is the only thing that
removes files here.
