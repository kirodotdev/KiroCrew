# PR description rules

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (*PR description contract*). The ten template sections are in `pr-description-contract.md`.

### Two checks, two strengths

`diff_signals.py --check-body` applies two rules, plus CI's template check, to the
finished body. Both stop the loop (why: `references/rationale.md`):

| check | what it measures | on breach |
|---|---|---|
| Accounting | every changed area (the `PR Scope` unit, in `pr-policy.yml`: a module directory under `src/kiro_crew/` or `website/src/`, the top-level component elsewhere) is named in the body — by the area, a changed path or its `dir/file` tail, or a unique non-generic file name | **exit 20** — stop, fix the body or the diff |
| Length | prose words in `What changed` (fences, tables, images excluded) against the 500 of section 3's paragraph rule; table rows against a picture's eight | **exit 21** — stop, compress the prose |

The template check runs CI's `.github/scripts/pr-description-check.sh` from
`origin/<base>`: a missing section is **exit 22**. A `fix:` body's `Proof:` line is
judged by `proof_line_check.py` the same way, and a `PROVEN` line is compared with
`prove.py`'s record: findings print, and are **exit 24** once CI enforces them. On a
`fix`/`revert` body a missing `## Pattern harvest` heading, or one with no line
starting `Rule candidate:` or `Not generalizable:`, is **exit 26**: PR Hygiene fails
the same body, and the colon right after those words is what it matches. The
lowest code wins.

### Snapshot, not changelog

Rewrite the body from `git diff origin/<base>...HEAD` every round, then compare
it with the published body and remove unsupported claims. Copy the frozen goal
verbatim every round. Describe current code, never one round's fix; history
belongs in disposition comments. No history words: `also`, `additionally`,
`now also`, `after review`, `round N`, `follow-up fix`, `per reviewer`,
`addressed`, `updated to`.

### Writing register: Age 5

Use the Age 5 row of the `explain-for` skill. Age 5 is the *register*, never the
depth or the reader; the facts stay complete and technically exact.

- **Punch line first, in every section.** State the effect in user words, then
  only the facts needed to support it. One idea per sentence.
- **Do not recite the diff.** No per-file walkthrough or nested bullets; evidence
  and history belong in the review thread. One general sentence may cover a whole
  area ("docs updated to name the nightly").
- **Lead with a table when the change has more than one moving part.**
- Keep identifiers, paths, errors and flags verbatim. Cut decorative jargon.
- Draw ONE picture at most, only when it explains a changed shape faster than
  prose; how is in `references/body-picture.md`.

Before publishing, reread section 3 and rewrite any sentence that needs a second read.

### Screenshots

Capture each affected surface in its meaningful variants **by looking at the change
yourself** with the `web-verify` skill, in the full application chrome (nav/sidebar
and the whole page or dialog in context), not a cropped element shot. A video renders
as a player only alone in
its own paragraph.

- **Attach, never commit.** Capture into `$KIROCREW_SCRATCH/evidence/` or the
  gitignored `temp-screenshots/<feature>/`. Write ordinary local paths in the body
  file and pass the same files to `gh` (gh >= 2.99), BEFORE the push that needs
  judging:

  ```bash
  gh pr create --base <base> --head <branch> --title "<CC title>" --body-file <body> \
    --attach ./evidence/after.png --attach ./evidence/demo.mp4
  gh pr edit <n> --body-file <body> --attach ./evidence/after-v2.png
  ```

  Each path becomes a permanent `https://github.com/user-attachments/assets/<uuid>`
  URL that survives force-pushes and merge; carry those URLs over verbatim when a
  later round regenerates the body.
- **Verify:** `gh api repos/<owner>/<repo>/pulls/<n> --jq .body | grep -c user-attachments` MUST print the number of files attached.
- **Limits and formats:** PNG, JPEG, GIF, WebP, MP4, MOV, WebM; 10 MB per image or GIF, 100 MB per video. **No SVG**, attached or committed: the reviewer's Read tool opens it as markup, not pixels, so both lanes skip it; export a PNG.
- **Push access is required for upstream `--attach`; without it, upload off-PR first and commit only as the last resort.** `--attach` on the upstream PR uploads through an endpoint that 404s on read permission (cli/cli#14302), so a fork contributor has none. One order of preference, binaries off the PR until nothing else works: (1) `--attach` on your OWN fork even without upstream write (you are admin there): `gh api -X PATCH repos/<you>/<fork> -f has_issues=true` then `gh issue create --repo <you>/<fork> --attach <file> ...`, which mints account-scoped `user-attachments` URLs that render cross-repo -- read them from the issue body and embed upstream (keep that issue as a reusable upload holder: GitHub retains unlinked media only ~30 days and whether deleting the source issue orphans the asset is undocumented, so keeping it is the safe default); (2) drag the file into the description in the web UI (same `user-attachments` URL, same limits); (3) only when neither upload path is available, `git add -f temp-screenshots/<topic>/after.png` and reference the path -- the fork lanes read the committed bytes, 10 MB per file, video included, same formats (another format is skipped with a warning naming it); a bigger recording is dragged in, not committed. Raw `raw.githubusercontent.com` URLs are NOT evidence -- `pr-attachment-evidence.sh` admits only `github.com/user-attachments/assets/...`, so a raw link reaches no review lane. A committed blob is in `main`'s history for good once merged; why: `references/rationale.md`.
- Non-media evidence (a JSON dump, a perf baseline) goes in a fenced block in a PR
  comment; link its permalink.

**No-visual-delta waiver** — when the diff touches watched frontend paths but
changes no pixel, both lines together:

```markdown
<!-- no-visual-delta -->
**Why no screenshot:** <one-line reason>
```

Use it for comment-only, type-only or identical-output refactors and non-rendering
attributes. Screenshot instead for any component, layout, theme, spacing or
user-visible string change. **When in doubt, screenshot.**

### Issue link

`gh pr view <n> --json closingIssuesReferences` reads the closure back; an empty list
with an issue named in the body means the keyword is missing or malformed, and
`pr_status.py` prints a `NOTICE:`. Only `Fixes|Closes|Resolves #<n>` as a whole
line closes the issue; `Part of #<n>` and `Refs #<n>` satisfy `Issue Gate` but
close nothing.
