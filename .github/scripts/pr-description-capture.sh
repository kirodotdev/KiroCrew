#!/usr/bin/env bash
# Capture a PR's title and description as ONE bounded data file, and name those
# exact bytes with a digest.
#
# Sourced (not executed) by every review lane whose model judges the author's
# stated intent: design-review.yml, ux-review.yml, first-principles-review.yml
# and their fork counterparts. One copy, because the bytes a lane captured are
# the bytes its published verdict claims to have read, and two implementations
# of the cap would make the same digest mean two different things.
#
# Inputs, all environment variables:
#   REPO, PR, GH_TOKEN   which PR's title and description to read, via `gh api`
#   INTENT               destination file for the captured prose
#   EVIDENCE_LIST        OPTIONAL. A file holding one absolute path per line,
#                        naming the evidence files THIS lane's model is pointed
#                        at besides $INTENT. Set it in a lane whose verdict also
#                        reads what the description's attachments resolved to;
#                        leave it unset in a lane that judges prose only, which
#                        keeps that lane's digest exactly $INTENT's. See the
#                        digest block below for why it exists.
#
# Outputs:
#   $INTENT              the captured title + description, media stripped,
#                        capped at 8000 bytes of well-formed UTF-8
#   $INTENT_DIGEST       sha256 naming everything this verdict read, set in the
#                        caller's shell: $INTENT's own digest when no
#                        $EVIDENCE_LIST was given, otherwise the digest of a
#                        manifest covering $INTENT and each listed file
#   $EVIDENCE_COUNT      how many evidence files the digest folded in, 0 when
#                        none, so a lane can say what its stamp covers
#   description_digest   the same digest, appended to $GITHUB_OUTPUT when set,
#                        so the lane's comment step can stamp it
#   evidence_count       the same count, likewise
#
# WHY A CAPTURE AND NOT A LIVE READ. A lane that let the model fetch the
# description itself had no revision to name: the model chose when to read, so
# the verdict corresponded to whatever the description said at an unobserved
# moment. Capturing once gives the verdict a single, nameable input. The grant
# that permitted that live read is also unsafe on its own: `--allowedTools`
# Bash grants are PREFIX-matched, so `Bash(gh pr view:*)` also admits
# `gh pr view ... > <path>`, letting text injected into the diff redirect over
# the lane's own input files.
#
# The diff is deliberately NOT captured here. A diff is pinned to a commit and
# cannot change under the verdict; a title and a description can. Capture the
# mutable state, leave the immutable state live.
#
# TO RECOMPUTE THE DIGEST for a published verdict, run this script against the
# PR and compare with the `[DESCRIPTION-READ]` line in the lane's comment:
#
#   REPO=<owner/repo> PR=<number> INTENT=$(mktemp) \
#     bash -c '. .github/scripts/pr-description-capture.sh; echo "$INTENT_DIGEST"'
#
# A mismatch means the description moved after that verdict was formed, so any
# finding the verdict drew from the description is unproven. A match means the
# verdict read the description as it stands now.
set -uo pipefail

# A title/description this step could not READ is not a PR without stated
# intent. `2>/dev/null || true` discarded both the error text and the exit
# status, so a transient API failure handed the reviewer an empty intent file
# and the lane judged a PR that appeared to state nothing -- blaming the author
# for a description the workflow never read. The common case is transient, so
# absorb it here rather than spending the author's re-run on it: up to three
# attempts with a short backoff. A read that never succeeds fails closed while
# naming the read as the cause; a genuinely empty description reads
# successfully on the first attempt and is judged as written, which is correct.
raw=""
read_ok=""
for attempt in 1 2 3; do
  if raw="$(gh api "repos/$REPO/pulls/$PR" --jq '"Title: \(.title)\n\nDescription:\n\(.body // "")"')"; then
    read_ok=1
    break
  fi
  echo "Reading the PR title and description failed on attempt $attempt."
  if [ "$attempt" -lt 3 ]; then
    sleep "$attempt"
  fi
done
if [ -z "$read_ok" ]; then
  echo "::error::Could not read this PR's title and description from the API after 3 attempts, so the review would judge a PR that appears to state no intent. This is a read failure, not a missing description -- re-run this job."
  exit 1
fi
# Strip embedded media: a screenshot-heavy body is pure cost to a reviewer that
# judges prose, and an attachment URL is not intent.
stripped="$(printf '%s' "$raw" | perl -0777 -pe 's/!\[[^\]]*\]\([^)]*\)/[image removed]/g; s{<img\b[^>]*>}{[image removed]}gi; s{<video\b.*?</video>}{[video removed]}gsi; s{<source\b[^>]*>}{[media removed]}gi; s{https?://\S*user-attachments\S*}{[media removed]}g' 2>/dev/null || printf '%s' "$raw")"
# Cap at 8000 bytes, dropping a trailing split multibyte character so the file
# stays well-formed UTF-8. Mark an over-cap body rather than truncating it
# silently.
#
# One perl read does the cap, because the two obvious shell spellings both
# break on precisely the over-cap input the cap exists for. `printf | head -c`
# gives the writer SIGPIPE once `head` has its bytes, and under `pipefail` that
# 141 fails the step -- a race below the ~64 KiB pipe buffer (a 30 KB body lost
# it) and certain above. And `iconv -c` drops INVALID bytes but still exits 1 on
# an INCOMPLETE sequence at EOF, so a `|| <raw fallback>` appended a second copy
# to the partial output already captured: 15,998 bytes of malformed UTF-8 from
# an 8000-byte cap.
full="$(mktemp)"
printf '%s' "$stripped" > "$full"
perl -e '
  open(my $fh, "<:raw", $ARGV[0]) or die; read($fh, my $b, 8000);
  # A lead byte with fewer continuation bytes than it declares is the tail the
  # cap split; anything else invalid was already in the body.
  $b =~ s/(?:[\xC2-\xDF]|[\xE0-\xEF][\x80-\xBF]?|[\xF0-\xF4][\x80-\xBF]{0,2})\z//;
  print $b, "\n";
' "$full" > "$INTENT"
if [ "$(wc -c < "$full")" -gt 8000 ]; then
  printf '\n[description TRUNCATED at 8000 bytes]\n' >> "$INTENT"
fi
rm -f "$full"

# Name the captured bytes. The digest covers $INTENT exactly as the model
# receives it -- after the media strip and the cap -- so it changes when the
# model's input changes and not when a rewrite the strip erases leaves that
# input identical. A digest taken over the raw body instead would flag an
# image-URL swap as a description the verdict never saw, which is the same
# false confidence in the other direction.
#
# $INTENT ALONE IS NOT THE WHOLE INPUT IN EVERY LANE, which is why
# $EVIDENCE_LIST exists. The strip replaces every user-attachments URL with the
# same `[media removed]` placeholder, so it erases exactly the part of the URL
# that tells one asset from another. In a lane whose verdict also reads what
# those URLs resolved to, a media-only edit therefore leaves $INTENT
# byte-identical while changing what the model judged, and a digest over
# $INTENT alone would report a match on a verdict formed from other evidence.
# That is reachable two different ways:
#   - the UX lanes open the downloaded images themselves, so swapping one
#     attachment for another shows the reviewer different pixels;
#   - the design lanes read the manifest of what downloaded and typed as an
#     image, and BLOCK with "CANNOT EVALUATE -- REQUIRED EVIDENCE MISSING"
#     when it names none, so swapping an image URL for a NON-image URL leaves
#     the stripped text identical and can still flip the verdict.
# So the digest names everything the verdict read and nothing it did not: each
# lane hands over the evidence ITS model is pointed at, and a lane with no
# evidence (the two first-principles lanes) hands over nothing and keeps the
# $INTENT-only digest, which is why a media-only edit still does not move it
# there -- a false alarm would be the same false confidence inverted.
#
# Digest stdin, not the path: given a filename holding a backslash, sha256sum
# escapes the name and prefixes the whole line with one, so `cut` yields
# `\<hex>` and the stamp names a revision no reader can reproduce. Under `<` the
# name is never part of the output, which removes that class outright.
#
# macOS ships no sha256sum, only shasum, so pick the same way cli.sh:296,
# playwright-cli.sh:447 and ensure-node.sh:235 already do. The lanes run on
# ubuntu, but the digest is documented as reproducible BY HAND -- the verdict
# tells a reader to recompute it with this script -- so a reader on a Mac has
# to get the same hash, and the test that executes this runs on every shard.
if command -v sha256sum >/dev/null 2>&1; then _kc_sha="sha256sum"
elif command -v shasum >/dev/null 2>&1; then _kc_sha="shasum -a 256"
else
  echo "::error::Neither sha256sum nor shasum is available, so the captured description at $INTENT cannot be named by digest. Failing closed rather than publishing an unnamed verdict."
  exit 1
fi
INTENT_DIGEST="$($_kc_sha < "$INTENT" | cut -d' ' -f1)"
# Fold in the evidence when the lane named any. The folded value is the digest
# of an explicit MANIFEST -- one `<sha256> <ordinal>` line for the description
# and one per evidence file, in the order the lane listed them -- rather than a
# hash of concatenated bytes, so the composition is reproducible by hand and
# two evidence files cannot be confused for one longer one. The manifest holds
# ordinals, never paths: a runner temp path is per-run, so digesting it would
# change the stamp on a re-run that read identical bytes.
if [ -n "${EVIDENCE_LIST:-}" ] && [ -r "${EVIDENCE_LIST:-}" ]; then
  _kc_manifest="$(mktemp)"
  printf 'description %s\n' "$INTENT_DIGEST" > "$_kc_manifest"
  _kc_ordinal=0
  while IFS= read -r _kc_ev || [ -n "$_kc_ev" ]; do
    [ -n "$_kc_ev" ] || continue
    _kc_ordinal=$((_kc_ordinal + 1))
    if [ ! -r "$_kc_ev" ]; then
      # An unreadable file the lane named as evidence cannot be named by
      # digest, and a stamp that silently omitted it would claim to cover
      # evidence it never hashed. Fail closed, the same way an unreadable
      # description does.
      echo "::error::The lane listed an evidence file this verdict reads that is not readable, so the published digest could not name everything the verdict read. Failing closed rather than publishing a stamp that overstates its coverage."
      rm -f "$_kc_manifest"
      exit 1
    fi
    printf 'evidence-%s %s\n' "$_kc_ordinal" "$($_kc_sha < "$_kc_ev" | cut -d' ' -f1)" >> "$_kc_manifest"
  done < "$EVIDENCE_LIST"
  INTENT_DIGEST="$($_kc_sha < "$_kc_manifest" | cut -d' ' -f1)"
  EVIDENCE_COUNT="$_kc_ordinal"
  rm -f "$_kc_manifest"
  unset _kc_manifest _kc_ordinal _kc_ev
else
  EVIDENCE_COUNT=0
fi
unset _kc_sha
case "$INTENT_DIGEST" in
  *[!0-9a-f]* | "")
    echo "::error::Digesting the captured description at $INTENT yielded '$INTENT_DIGEST', which is not a bare sha256, so a published verdict could not name a revision a reader can reproduce. Failing closed rather than publishing an unverifiable stamp."
    exit 1
    ;;
esac
if [ "${#INTENT_DIGEST}" -ne 64 ]; then
  echo "::error::Digesting the captured description at $INTENT yielded ${#INTENT_DIGEST} hex characters rather than 64, so a published verdict could not name a revision a reader can reproduce. Failing closed rather than publishing an unverifiable stamp."
  exit 1
fi
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "description_digest=$INTENT_DIGEST" >> "$GITHUB_OUTPUT"
  echo "evidence_count=$EVIDENCE_COUNT" >> "$GITHUB_OUTPUT"
fi
if [ "$EVIDENCE_COUNT" -gt 0 ]; then
  echo "Captured the PR title and description ($(wc -c < "$INTENT") bytes) plus $EVIDENCE_COUNT evidence file(s) this verdict reads, sha256 $INTENT_DIGEST."
else
  echo "Captured the PR title and description ($(wc -c < "$INTENT") bytes), sha256 $INTENT_DIGEST."
fi
