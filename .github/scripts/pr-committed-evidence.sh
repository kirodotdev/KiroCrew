#!/usr/bin/env bash
# Materialize the review evidence a pull request COMMITS, straight out of the
# object store -- for the contributors who cannot attach it to the PR
# description at all, and for every lane that admits committed media.
#
# `gh pr create|edit --attach` uploads through an endpoint that answers READ
# and TRIAGE permission with a 404 (cli/cli#14302), so a contributor working
# from a fork has no CLI path to a description attachment; dragging the file
# into the web editor is their only one. The committed convention therefore
# stays open to them: media added under `temp-screenshots/` or
# `.github/screenshots/` (both gitignored, so it takes `git add -f`) is review
# evidence too, and this script is what puts it in front of the reviewer.
#
# Sourced (not executed) AFTER pr-attachment-evidence.sh by the fork UX lane
# and by BOTH design lanes, so `$n` (images kept) and `$clips` (recordings
# listed) continue under the same MAX_SHOTS/MAX_CLIPS caps: a description
# attachment is the normal home for evidence and must never be dropped by the
# cap in favour of a committed file. It is a separate file from that one
# because the bytes come from a different place -- a git object rather than an
# HTTP download -- and because four lanes source the attachment script, whose
# behaviour this must not change.
#
# ONE PATH, whether or not the caller has a checkout. The fork lanes never
# check the fork head out; the same-repo design lane has the files at HEAD.
# Both source this script and both read blobs from the object store at
# HEAD_SHA: the working tree is never consulted, so there is no "checkout
# mode" and no second listing for it to drift from. A same-repo checkout
# fetched with `fetch-depth: 0` holds every blob of its head, so the read is
# the same read. (A lane that re-spelled this loop inline once listed by
# inclusion, additions and modifications only, and lost a renamed screenshot;
# that is the defect this rule exists to make unrepeatable.)
#
# Inputs, all environment variables:
#   BASE_SHA, HEAD_SHA   revision range; every blob is read AT HEAD_SHA
#   FETCH_DIR            scratch dir for the bytes before they are typed
#   DEST_DIR             where kept images land, as "$NAME_STEM-NN.<ext>"
#   NAME_STEM            copy-name stem, shared with the attachment pass
#   SHOTS, SHOT_MAP, CLIPS  list files this APPENDS to: kept image paths,
#                        "<name>\t<origin>" origins, recording origins
#   MAX_SHOTS, MAX_CLIPS caps, shared with the attachment pass
#
# Every byte here is UNTRUSTED PR content, so nothing about the committed
# file reaches disk except its bytes. The path never becomes a destination
# name (each copy is "$NAME_STEM-NN.<ext>", index-named, as the blind-read
# wall requires); the type comes from file(1) reading the bytes, never from
# the extension; and the blob is read with `git cat-file` instead of from a
# checkout, so the PR's tree is never materialized by this script -- a tracked
# symlink, a mode bit, or a path like `.git/hooks/pre-commit` cannot exist on
# disk to be followed, and where a checkout does exist it is not what admits.
#
# ADMISSION CONTRACT
# ==================
# This block is the one statement of which committed paths reach the reviewer
# and what is said about each one that does not. The loop below is this
# contract in the same order, one labelled stage per clause, and no path is
# decided anywhere else.
#
# Candidates. A candidate is every path under `temp-screenshots/` or
# `.github/screenshots/` that HEAD_SHA holds differently from BASE_SHA. The
# listing is `git diff -z -M --name-status --diff-filter=d`:
#   - `--diff-filter=d` admits every status but D. A deleted path has no bytes
#     at HEAD to read; everything else -- an addition (A), a modification (M),
#     a type change (T), a detected rename (R) or copy (C) -- names a path HEAD
#     holds, and is a candidate. Listing by inclusion (`AM`) is the shape that
#     loses a path: a renamed screenshot is neither A nor M, so it would never
#     enter the loop and its author would be told nothing was found.
#   - `-M` asks for rename detection explicitly, so the listing does not
#     change with the runner's `diff.renames` setting. A rename or copy is two
#     paths, and its DESTINATION is the candidate, because that is the path
#     HEAD holds.
#   - `--name-status` carries the status letter into the loop, so the rename
#     decision (stage 4) is made on it rather than lost in the listing.
#   - `-z` prints every path exactly, so a space or a non-ASCII byte in a name
#     is read as the name and the same string reaches every git read below.
# The pathspec on the listing and the extension sort in stage 1 are COST
# filters, not correctness filters: they keep a README or a source file in
# those directories from costing a tree read or a blob read. Nothing the
# reviewer receives is decided by a name -- the tree entry decides the mode
# and size (stage 5) and the bytes decide the type (stage 7).
#
# Classification. A candidate meets these stages in order; the first that
# applies decides it.
#   1. Name sort, on the lower-cased basename (a hidden PARENT directory must
#      not hide the media inside it): a media extension goes on; a text
#      sidecar or a dotfile is SILENT; any other extension is REFUSED by
#      format, announced at stage 3.
#   2. Counted. Every candidate that is not silent is one "media path" in the
#      summary. Silent is the only outcome above the counter and the only one
#      not counted: a README or a provenance JSON beside the screenshots was
#      never offered as evidence and must not become a warning on every run.
#   3. Control character in either path of the candidate: REFUSED, with the
#      name printed `%q`-quoted. This is the first stage that writes a path
#      anywhere, and every sink -- $SHOT_MAP, $CLIPS, the `::warning::` and
#      TRUNCATED lines of the log -- is below it, so a newline, a tab or a
#      carriage return in a tracked name can forge no record and no workflow
#      command. The format refusal decided at stage 1 is announced right
#      after this check, by file and by reason, at the price of no read.
#   4. Moved unchanged (status R100 or C100, the destination byte-identical
#      to a path the base already held): REFUSED, naming both paths. A file
#      whose bytes were already on the base shows the base's rendering, not
#      this revision's, so it is not evidence of this change -- and it is
#      counted and announced so its author is never told "0 media path(s)"
#      for a screenshot they moved. A rename whose bytes changed goes on: at
#      HEAD it is a modified file under a new name.
#   5. Tree entry: a mode other than 100644/100755 (a symlink is 120000, a
#      submodule 160000) or a size over 10 MB is REFUSED before any bytes are
#      read. `git ls-tree` takes a pathspec where `git cat-file` takes an
#      exact path, so the pathspec is pinned with `:(literal)`: the mode and
#      size gates judge exactly the entry whose bytes are read, and a `*`,
#      `[`, `?` or `!` in a name names that one entry.
#   6. Read budget: MAX_SHOTS + MAX_CLIPS blob reads, shared with the
#      attachment pass and consumed whether the bytes are kept or refused.
#      Beyond it a candidate is TRUNCATED: logged with its path, not read,
#      not counted as skipped -- the same accounting the attachment script
#      keeps for its downloads.
#   7. Blob read and byte typing: an unreadable blob, or a mime the table
#      does not list, is REFUSED. A kept image is copied under an index name
#      and mapped to its origin; a recording is listed; beyond MAX_SHOTS or
#      MAX_CLIPS the list files carry a TRUNCATED line instead.
#
# Outcomes. SILENT: not counted, nothing printed. REFUSED: counted as found
# and as skipped, one `::warning::SKIPPED (<reason>): <path>` line naming the
# file and the reason its author acts on. TRUNCATED: counted as found, a
# TRUNCATED line in the log or the list file. KEPT / LISTED: counted as found.
# The summary line reports found, kept and skipped; it names the moved-
# unchanged count separately when there is one.
set -euo pipefail
mkdir -p "$FETCH_DIR" "$DEST_DIR"
# Continue the attachment pass's counters when it ran first, and stand alone
# when it did not, so the caps below bound the two sources TOGETHER.
n="${n:-0}"
clips="${clips:-0}"
committed_found=0
committed_kept=0
committed_skipped=0
committed_moved=0
# The attachment pass leaves its per-download attempt count in `fetched`.
# Start there so the expensive-read budget covers both evidence sources; a
# standalone invocation starts at zero.
committed_read="${fetched:-0}"
# --- Candidates (see the contract) --------------------------------------------
# The listing goes to a file before the loop reads it, so that a git failure
# is a failure: the exit status of a process substitution is invisible to
# `set -e`, and an empty listing would read as "this PR commits no media",
# which the design lane reports as evidence the author never supplied. The
# attachment script fails its step the same way when the description cannot
# be read -- a red step the author can re-run, never a silent "nothing here".
paths="$(mktemp "$FETCH_DIR/committed-paths.XXXXXX")"
if ! git diff -z -M --name-status --diff-filter=d "$BASE_SHA...$HEAD_SHA" -- \
    temp-screenshots .github/screenshots > "$paths"; then
  echo "::error::Could not enumerate the media committed between $BASE_SHA and $HEAD_SHA (git diff failed, see above), so the committed evidence cannot be collected; re-run the workflow."
  # Record the lane's own failure before leaving, because `exit 1` from a
  # SOURCED script skips the rest of the caller's step, including the line
  # that writes this output. The fork lanes' Finalize step resolves an errored
  # advisory run as NEUTRAL, which PR readiness scores as a pass, so without
  # this the step goes red while the check-run says "review incomplete
  # (advisory)" and nothing blocks. `unfetched` is the output both fork lanes
  # already turn into a FAILED check-run naming the re-run as the remedy; the
  # design lanes do not read it, so writing it there is inert.
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "unfetched=true" >> "$GITHUB_OUTPUT"
  fi
  rm -f -- "$paths"
  exit 1
fi
# One record is "<status>\0<path>\0"; a rename or copy is
# "<status><score>\0<source>\0<destination>\0". The destination is the
# candidate, and the source is kept only for stage 3 and the stage 4 message.
while IFS= read -r -d '' status; do
  from=""
  case "$status" in
    R*|C*) IFS= read -r -d '' from || break ;;
  esac
  IFS= read -r -d '' path || break
  [ -n "$path" ] || continue
  # --- Stage 1: name sort -----------------------------------------------------
  # Lower-cased through `tr` in the C locale: it folds the ASCII letters and
  # passes every other byte through, so a non-ASCII name still matches its
  # extension, and it runs under the bash 3.2 of macOS, which has no `${var,,}`
  # and aborts the whole step on it.
  lower="$(printf '%s' "$path" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
  # SVG gets its own reason, the one its author acts on: the Read tool opens
  # it as markup, not pixels, so a blind reader would be shown text -- the
  # same reason the mime table at stage 7 omits it. prepare-pr/SKILL.md states
  # the same exclusion beside its format list. The final arm is the
  # extensionless tier (README, LICENSE): silent, like the sidecars.
  refuse=""
  case "${lower##*/}" in
    *.png|*.jpg|*.jpeg|*.webp|*.gif|*.webm|*.mp4|*.mov) ;;
    .*|*.txt|*.md|*.json|*.yaml|*.yml|*.csv|*.log|*.html) continue ;;
    *.svg) refuse="SVG is opened as markup, not pixels; export the image as PNG" ;;
    *.*) refuse="not a format the reviewer reads; commit PNG, JPEG, WebP, GIF, MP4, MOV or WebM" ;;
    *) continue ;;
  esac
  committed_found=$((committed_found + 1))  # --- Stage 2: counted
  # --- Stage 3: control characters, then the format refusal -------------------
  # Both paths are fork-controlled text, and from here down they are written
  # into $SHOT_MAP and $CLIPS -- files the reviewer's prompt presents as
  # written by the workflow -- and into the `::warning::` and TRUNCATED lines
  # of the log. No line above this check interpolates either of them.
  case "$from$path" in
    *[[:cntrl:]]*)
      echo "::warning::SKIPPED (path contains a control character): $(printf '%q' "$path")"
      committed_skipped=$((committed_skipped + 1))
      continue ;;
  esac
  if [ -n "$refuse" ]; then
    echo "::warning::SKIPPED ($refuse): $path"
    committed_skipped=$((committed_skipped + 1))
    continue
  fi
  # --- Stage 4: moved unchanged -----------------------------------------------
  case "$status" in
    R100|C100)
      echo "::warning::SKIPPED (moved from $from with no change in its bytes, so it shows the base, not this revision; capture the screenshot again): $path"
      committed_skipped=$((committed_skipped + 1))
      committed_moved=$((committed_moved + 1))
      continue ;;
  esac
  # --- Stage 5: tree entry ----------------------------------------------------
  # `git ls-tree -l` prints "<mode> <type> <oid> <size>\t<path>".
  meta="$(git ls-tree -l "$HEAD_SHA" -- ":(literal)$path" 2>/dev/null || true)"
  mode="$(awk '{print $1; exit}' <<< "$meta")"
  size="$(awk '{print $4; exit}' <<< "$meta")"
  case "$mode" in
    100644|100755) ;;
    *)
      echo "::warning::SKIPPED (not a regular file at $HEAD_SHA, mode ${mode:-unknown}): $path"
      committed_skipped=$((committed_skipped + 1))
      continue ;;
  esac
  # One ceiling for every committed blob, image or recording: 10 MB, GitHub's
  # per-image attachment limit. A committed file is permanent history that
  # every clone carries, and a recording is the worst case for that cost, so
  # the 100 MB the attachment path allows a download -- transient bytes on a
  # runner -- does not carry over to a video here. A recording that needs
  # more has a home that costs the repository nothing: dragged into the
  # description in the web editor, which the attachment script reads.
  if [ "${size:-0}" -gt 10485760 ]; then
    echo "::warning::SKIPPED (${size} bytes, over the 10 MB ceiling): $path"
    committed_skipped=$((committed_skipped + 1))
    continue
  fi
  # --- Stage 6: read budget ---------------------------------------------------
  if [ "$committed_read" -ge "$((MAX_SHOTS + MAX_CLIPS))" ]; then
    echo "TRUNCATED: more than $((MAX_SHOTS + MAX_CLIPS)) pieces of evidence; not read: $path"
    continue
  fi
  committed_read=$((committed_read + 1))
  # --- Stage 7: blob read and byte typing -------------------------------------
  tmp="$(mktemp "$FETCH_DIR/committed.XXXXXX")"
  if ! git cat-file blob "$HEAD_SHA:$path" > "$tmp" 2>/dev/null; then
    echo "::warning::SKIPPED (blob not readable at $HEAD_SHA): $path"
    committed_skipped=$((committed_skipped + 1))
    rm -f -- "$tmp"
    continue
  fi
  # Type by bytes, never by the path: a fork controls the name it commits. SVG
  # is left out on purpose, as in the attachment script, and was already
  # refused by name at stage 1, so a `.svg` never reaches this table; a `.png`
  # holding SVG bytes lands here as image/svg+xml and is skipped by its mime.
  mime="$(file --mime-type -b -- "$tmp")"
  case "$mime" in
    image/png) ext=png ;;
    image/jpeg) ext=jpg ;;
    image/webp) ext=webp ;;
    image/gif) ext=gif ;;
    video/mp4) ext=mp4 ;;
    video/quicktime) ext=mov ;;
    video/webm) ext=webm ;;
    *)
      echo "::warning::SKIPPED (mime $mime): $path"
      committed_skipped=$((committed_skipped + 1))
      rm -f -- "$tmp"
      continue ;;
  esac
  case "$ext" in
    mp4|mov|webm|gif)
      if [ "$clips" -lt "$MAX_CLIPS" ]; then
        clips=$((clips + 1))
        printf '%s\n' "$path" >> "$CLIPS"
      else
        echo "TRUNCATED: more than $MAX_CLIPS recordings; one was not listed" >> "$CLIPS"
      fi ;;
  esac
  case "$ext" in
    png|jpg|webp|gif)
      # A GIF is both: the model can open its first frame, and its existence
      # is what the continuity lens asks about.
      if [ "$n" -lt "$MAX_SHOTS" ]; then
        n=$((n + 1))
        committed_kept=$((committed_kept + 1))
        name="$(printf '%s-%02d.%s' "$NAME_STEM" "$n" "$ext")"
        mv -- "$tmp" "$DEST_DIR/$name"
        printf '%s\n' "$DEST_DIR/$name" >> "$SHOTS"
        printf '%s\t%s\n' "$name" "$path" >> "$SHOT_MAP"
        continue
      else
        echo "TRUNCATED: more than $MAX_SHOTS images; one was not listed" >> "$SHOTS"
        printf 'TRUNCATED\t%s\n' "$path" >> "$SHOT_MAP"
      fi ;;
  esac
  rm -f -- "$tmp"
done < "$paths"
rm -f -- "$paths"
moved_note=""
if [ "$committed_moved" -gt 0 ]; then
  moved_note=" $committed_moved of the skipped were moved from the base without a change in bytes and show the base, not this revision."
fi
echo "Committed evidence: $committed_found media path(s) added or changed under temp-screenshots/ or .github/screenshots/, $committed_kept image(s) kept, $committed_skipped skipped.$moved_note"
