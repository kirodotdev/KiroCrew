# temp-screenshots/

This directory holds **PR review evidence**: screenshots and GIFs captured
while preparing a pull request, not product assets. Nothing here ships in a
package, a wheel, or the desktop app.

## Why these are committed rather than attached

The `user-attachments` mechanism (drag-and-drop upload in the GitHub web UI)
renders fine in a PR description, but nothing in an automated PR workflow, CI
or CLI, can produce an attachment that way. Committing the file and linking it
with a commit-SHA-pinned URL is reachable from a script:

```
https://github.com/<owner>/<repo>/raw/<sha>/temp-screenshots/<feature>/<name>.png
```

Committing also puts the PR in scope for automated UX review, which an
attachment cannot do because an attachment is not a file in the repository.
Both `ux-review.yml` (:70) and `fork-ux-review.yml` (:205) gate on the changed
paths:

```bash
grep -qE '^(website/|temp-screenshots/|\.github/screenshots/)'
```

Whether the reviewer can then *see* the image depends on the lane. A same-repo
PR's reviewer reads the files the diff adds. A fork PR's reviewer runs against
the base tree, so it can open screenshots already on `main` but not ones the PR
itself adds — those are judged from the diff and surrounding code, as
`fork-ux-review.yml:236` and `:275` note. The trigger works either way, so the
images remain the durable record for human reviewers.

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

## See also

No application code imports these files, but the path itself is referenced by
three shipped skills under `src/kiro_crew/`, which are what tell an author or
agent to write here in the first place:

- `builtin_skills/kirocrew-dev/prepare-pr/SKILL.md` and its
  `assets/pr-body-template.md` — read before a PR body is written.
- `apps/builtins/dev_fleet/skills/pod-e2e/SKILL.md` — the operational recipe:
  copy the media in, amend into the PR's single commit, force-push, then verify
  the body update landed.
