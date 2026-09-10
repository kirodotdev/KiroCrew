#!/usr/bin/env bash
# Fetch the review evidence a PR description carries as GitHub attachments.
#
# Sourced (not executed) by the "Collect blind-read evidence" step of
# ux-review.yml and the "Fetch attachment evidence from the PR description"
# step of fork-ux-review.yml, so both lanes run this one copy and the caller
# keeps $n (images kept) and $clips (recordings listed) afterwards. The fork
# lane checks out the trusted base ref, so it always runs the base tree's copy.
#
# Inputs, all environment variables:
#   REPO, PR, GH_TOKEN   which PR's description to read, via `gh api`
#   FETCH_DIR            scratch dir for in-flight downloads
#   DEST_DIR             where kept images land, as "$NAME_STEM-NN.<ext>"
#   NAME_STEM            "shot" (same-repo lane) or "attachment" (fork lane)
#   SHOTS, SHOT_MAP, CLIPS  list files this appends to: kept image paths,
#                        "<name>\t<url>" origins, recording URLs
#   MAX_SHOTS, MAX_CLIPS caps on images kept and recordings listed
#
# Only GitHub's own asset URL shape is read out of the body, by a strict
# allowlist: the user-attachments form, the one `gh --attach` and the web
# editor emit, which redirects to GitHub's user-asset S3 bucket. Nothing else
# in the body is evidence. $body reaches grep as standard input and nothing
# else reads it, so nothing the description says can change what runs.
set -euo pipefail
mkdir -p "$FETCH_DIR" "$DEST_DIR"
# The description is read from the API, not the event payload, so a re-run
# after a late attachment judges the current text. One transient API failure
# (a 5xx, a rate limit) must not end the lane, so the read gets three
# attempts, the same shape as the lanes' other gh api reads, and then fails
# closed: an empty body would read as "no evidence", which is worse than a
# red step the author can re-run.
body=""
read_ok=""
for attempt in 1 2 3; do
  if body="$(gh api "repos/$REPO/pulls/$PR" --jq '.body // ""')"; then
    read_ok=1
    break
  fi
  echo "Reading the PR description failed on attempt $attempt."
  if [ "$attempt" -lt 3 ]; then
    sleep "$attempt"
  fi
done
if [ -z "$read_ok" ]; then
  echo "::error::Could not read this PR's description after 3 attempts, so the attachment evidence cannot be collected; re-run the workflow."
  exit 1
fi
allow="https://github\.com/user-attachments/assets/[0-9A-Za-z-]+"
urls="$(grep -oE "$allow" <<< "$body" | awk '!seen[$0]++' || true)"
n=0
found=0
fetched=0
skipped=0
clips=0
while IFS= read -r url; do
  [ -n "$url" ] || continue
  found=$((found + 1))
  # A URL's type is only known after the download, so the two caps
  # together bound how many downloads a description can cost.
  if [ "$fetched" -ge $((MAX_SHOTS + MAX_CLIPS)) ]; then
    echo "TRUNCATED: more than $((MAX_SHOTS + MAX_CLIPS)) attachment URLs in the PR description; not fetched: $url"
    continue
  fi
  fetched=$((fetched + 1))
  tmp="$(mktemp "$FETCH_DIR/attachment.XXXXXX")"
  # No Authorization header, deliberately: the asset host serves a
  # public repository's attachments anonymously and answers with a
  # redirect to a pre-signed object URL, which a forwarded credential
  # header would invalidate. A failed download is logged and skipped,
  # never fatal -- the image is then an evidence gap for the reviewer.
  if ! curl -sSfL --proto '=https' --proto-redir '=https' --max-redirs 5 --max-time 60 --max-filesize 104857600 -o "$tmp" "$url"; then
    echo "::warning::SKIPPED (download failed): $url"
    skipped=$((skipped + 1))
    rm -f -- "$tmp"
    continue
  fi
  # Type by bytes, never by URL: the asset URLs carry no extension, and a
  # name proves nothing. The extension the bytes earn is what the Read tool
  # and the extension-keyed handling below go by. SVG is left out on
  # purpose: the Read tool opens it as markup, not as pixels, so it is text
  # a blind reader would be asked to look at.
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
      echo "::warning::SKIPPED (mime $mime): $url"
      skipped=$((skipped + 1))
      rm -f -- "$tmp"
      continue ;;
  esac
  case "$ext" in
    mp4|mov|webm|gif)
      if [ "$clips" -lt "$MAX_CLIPS" ]; then
        clips=$((clips + 1))
        printf '%s\n' "$url" >> "$CLIPS"
      else
        echo "TRUNCATED: more than $MAX_CLIPS recordings in the PR description; one was not listed" >> "$CLIPS"
      fi ;;
  esac
  case "$ext" in
    png|jpg|webp|gif)
      # A GIF is both: the model can open its first frame, and its
      # existence is what the continuity lens asks about. The copy gets an
      # opaque, order-numbered name that carries nothing but the format.
      if [ "$n" -lt "$MAX_SHOTS" ]; then
        n=$((n + 1))
        name="$(printf '%s-%02d.%s' "$NAME_STEM" "$n" "$ext")"
        mv -- "$tmp" "$DEST_DIR/$name"
        printf '%s\n' "$DEST_DIR/$name" >> "$SHOTS"
        printf '%s\t%s\n' "$name" "$url" >> "$SHOT_MAP"
        continue
      else
        echo "TRUNCATED: more than $MAX_SHOTS images; one was not listed" >> "$SHOTS"
        printf 'TRUNCATED\t%s\n' "$url" >> "$SHOT_MAP"
      fi ;;
  esac
  rm -f -- "$tmp"
done <<< "$urls"
# Every attempted download skipped is a different event from one bad URL:
# the reviewer is about to judge with no evidence at all. The usual cause is
# the host github.com redirects user-attachments to having moved -- in the
# fork lane that is the egress allowlist no longer naming it -- so it is an
# error annotation on the run, not a warning per URL.
if [ "$fetched" -gt 0 ] && [ "$skipped" -eq "$fetched" ]; then
  echo "::error::Every one of the $fetched attachment download(s) was skipped; no evidence from the PR description reached the reviewer. If the SKIPPED lines above say download failed, check that the host github.com redirects user-attachments to is reachable (currently github-production-user-asset-6210df.s3.amazonaws.com; in fork-ux-review.yml it must be in the egress allowlist)."
fi
echo "PR description: $found attachment URL(s) matched the allowlist, $fetched download(s) attempted, $skipped skipped, $n image(s) kept, $clips recording(s) listed (bytes not kept; a GIF counts as both)."
