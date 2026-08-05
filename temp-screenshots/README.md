# `temp-screenshots/`

Review evidence for pull requests: the screenshots, GIFs and short videos that
show a reviewer what a user-visible change actually looks like. Nothing here is a
product asset, and no application code imports or reads these files. `MANIFEST.in`
prunes the whole directory, so none of it reaches a distribution.

The path itself is referenced, though — by the cleanup workflow, the PR template,
and three shipped skill files under `src/kiro_crew/` that tell an agent how to
attach media (see **See also**). So treat the directory as contractual: agents and
CI expect this layout.

The directory is **ephemeral by design and permanent in history**, which sounds
contradictory and is the single thing worth understanding before you add to it or
ask someone to remove something from it. A weekly job deletes files from `main`'s
tip; the blobs stay reachable in history, and the URLs in merged PR bodies keep
resolving because they are pinned to the commit that introduced them.

## Why the files are committed rather than attached

GitHub lets you drag an image into a PR description, which uploads it to
`user-attachments` and needs no commit. This repository commits instead, for two
reasons that survive scrutiny better than convenience does.

**Automated UX review is gated on these paths — and reads the files in one of its
two lanes.** `UX Review` runs only when a PR touches `website/`,
`temp-screenshots/**` or `.github/screenshots/**`, and skips with no model call
otherwise. Putting a screenshot here is therefore what brings a PR into scope at
all, which an attachment cannot do, because an attachment is not a file in the
repository.

Whether the reviewer can then *look* at the image depends on the lane:

- **Same-repo PRs** (`ux-review.yml`): it reads each image the diff adds or
  changes under those paths and grounds its visual findings in them.
- **Fork PRs** (`fork-ux-review.yml`): it runs on `pull_request_target` against
  the **base** tree, so it can open screenshots already committed on `main` but
  **not** ones the PR itself adds — those appear in the diff as binary markers and
  the review is made from code. The workflow says so at `fork-ux-review.yml:236`.

So for a fork PR the trigger still works and the images are still the durable
record for human reviewers; only the automated visual read is unavailable.

**Pinned URLs outlive the cleanup.** Embed with the commit SHA, not a branch name:

```markdown
![alt](https://github.com/kirodotdev/KiroCrew/raw/<sha>/temp-screenshots/<feature>/<name>.png)
```

A `main`-relative URL breaks the moment the weekly prune removes the file, and a
branch-relative one breaks when the branch is deleted on merge. A SHA-pinned URL
keeps resolving from the historical commit either way. That property is what the
prune relies on, and it is why the blobs entering history is the mechanism rather
than an accident.

## Naming and re-pinning

```
temp-screenshots/<feature>/<name>.png
```

`<feature>` is a short slug for the change, not a PR number or a date — several
subdirectories here pair `before.png` with `after.png`, which reads well in a
diff. Show each affected surface in the variants that matter: light and dark,
empty and populated, desktop app and browser. Prefer a short video or GIF when the
change involves motion or a multi-step flow, because a still cannot evidence
those.

**Re-pin after every amend or rebase.** This repository requires a single squashed
commit per PR, so the SHA changes on essentially every revision, and every URL in
the body silently stops resolving when it does. This is routine here, not an edge
case: if you amended, assume your links are stale and update them.

## Lifecycle

`.github/workflows/cleanup-temp-screenshots.yml` runs on a schedule — `cron: "0 8
* * 1"`, Mondays 08:00 UTC — and prunes tracked files under this directory older
than `RETENTION_DAYS`, default **14**. It is a timer, not a per-PR job, and the
retention window is overridable per run through `workflow_dispatch`. Because
`main` is protected, it opens a pull request with the deletions instead of pushing
them.

This file is exempt by name:

```bash
[ "$f" = "temp-screenshots/README.md" ] && continue
```

so it needs no allowlist entry and no workflow change to stay put.

For scale: this directory currently holds around 300 files across roughly 85
feature subdirectories, and the oldest of them is about a week old — so that count
is one week of review evidence on this project, not a full retention window. Note
also that the prune has not yet removed anything: the convention landed on 24 July,
so the first files only reach the 14-day cutoff in early August, and both scheduled
runs so far have exited with nothing to do.

## Do not delete your own screenshots before merge

**Authors: leave them in the PR.** **Reviewers, human or model: do not ask for
them to be removed.**

This is the part that costs review rounds when it is not written down. Committed
PNGs in a source tree look like debris to a reviewer seeing the diff cold, and an
AI reviewer has flagged exactly that as blocking — on #1216, the Arbiter objected
to `temp-screenshots/library-hero/*.png` being "baked into `main`'s history". The
finding was resolved with a scoped `/ai-review override arbiter`, on the grounds
that committing review screenshots here is the documented convention and that the
blobs staying in history is precisely what keeps SHA-pinned URLs resolving after
the tip is pruned.

That override cost a round trip on a PR whose actual change was three files. The
purpose of this README is to make the next one unnecessary: the convention is
deliberate, the cleanup is automated, and deleting files pre-merge would break
the URLs in the very PR body a reviewer is reading.

## See also

The files that actually drive this directory, roughly in the order an author or
agent meets them:

- `src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md` and its
  `assets/pr-body-template.md` — what an agent reads before it writes here.
- `src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/SKILL.md` — the full
  operational recipe: copy the approved media in, amend into the PR's single
  commit, force-push with lease, re-pin every URL after any later amend, and
  verify the body update landed. It also notes that a force-push resets PR
  approvals, which is worth knowing before you attach anything.
- `.github/PULL_REQUEST_TEMPLATE.md` — when screenshots are mandatory, and the
  exact URL form to embed.
- `docs/ci/ci-and-reviews.md` — the cleanup job among the other scheduled
  maintenance workflows, and `UX Review`'s early-skip behaviour.
- `.github/workflows/cleanup-temp-screenshots.yml` — the prune itself.
