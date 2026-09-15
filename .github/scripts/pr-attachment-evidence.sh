#!/usr/bin/env bash
# Fetch the review evidence a PR carries: the images and recordings the
# description attaches, and -- on the fork lanes -- the images the PR commits.
#
# Sourced (not executed) by the "Collect blind-read evidence" step of
# ux-review.yml, the "Fetch evidence (attachments + committed screenshots)"
# step of fork-ux-review.yml, and the "Collect rendered evidence" step of
# fork-design-review.yml, so every lane runs this one copy and the caller
# keeps $n (images kept), $clips (recordings listed) and $failed (transport
# failures) afterwards. The fork lanes check out the trusted base ref, so they
# always run the base tree's copy.
#
# Inputs, all environment variables:
#   REPO, PR, GH_TOKEN   which PR to read, via `gh api`
#   FETCH_DIR            scratch dir for in-flight downloads
#   DEST_DIR             where kept images land, as "$NAME_STEM-NN.<ext>"
#   NAME_STEM            "shot" (same-repo lane) or "attachment" (fork lanes)
#   SHOTS, SHOT_MAP, CLIPS  list files this appends to: kept image paths,
#                        "<name>\t<origin>" origins, recording origins
#   MAX_SHOTS, MAX_CLIPS caps on images kept and recordings listed
#   HEAD_REPO, HEAD_SHA, BASE_SHA  (fork switch, optional) when ALL THREE are
#                        set, also fetch every image this PR adds/modifies under
#                        temp-screenshots/ or .github/screenshots/, continuing
#                        the same numbering. All three are GitHub-set trusted
#                        facts and are shape-validated; the changed set comes
#                        from the compare endpoint pinned to
#                        (BASE_SHA...HEAD_SHA) -- NOT the live pulls/files
#                        endpoint, which reflects the newest head -- so the
#                        changed set and the bytes both refer to the reviewed
#                        revision. Each committed path is allowlist-validated,
#                        resolved through the git TREES API pinned to HEAD_SHA
#                        (blob + regular mode only), and fetched by blob SHA over
#                        the git BLOBS API with the raw media type -- served from
#                        api.github.com, no fork-controlled string in any
#                        request. The same-repo lane, which reads committed
#                        images off its own checkout, leaves them unset and that
#                        block is a no-op.
#
# Only GitHub's own asset URL shape is read out of the body, by a strict
# allowlist: the user-attachments form, the one `gh --attach` and the web
# editor emit, which redirects to GitHub's user-asset S3 bucket. Nothing else
# in the body is evidence. $body reaches grep as standard input and nothing
# else reads it, so nothing the description says can change what runs. The
# committed-image block likewise takes its file list and blob bytes only from
# trusted API facts (compare + git trees + git blobs APIs pinned to the event's
# BASE_SHA/HEAD_SHA), never from the body, and validates every path against a
# strict allowlist.
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
# Transport failures alone -- a download that did not complete for a reason
# a re-run can change (5xx, throttling, no answer). A file that downloaded
# but typed as a non-image, or a URL the asset host definitively rejects
# (404 and its 4xx kin: stale, deleted, or fabricated), is the author's
# evidence problem, not a fetch failure, and is counted in `skipped` only.
# The caller reads `failed` to tell "the lane could not fetch it" from
# "nothing usable was supplied".
failed=0
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
  # header would invalidate. A failed download is logged, skipped and
  # counted; the sourcing workflow decides what the count means (both
  # UX lanes fail their run on a non-zero `failed`).
  # `-w '%{http_code}'` is the one thing on stdout, so the status is
  # known even when `-f` fails the transfer: a definite 4xx (not 403, 408
  # or 429) is the URL itself -- stale, deleted, or never real -- which is
  # the author's evidence problem and counts in `skipped` only; anything
  # else (5xx, 403/408/429 -- GitHub answers a secondary rate limit with
  # 403 as well as 429 -- or a connection that never answered, code 000 or
  # empty) is transport and counts in `failed` too.
  rc=0
  code="$(curl -sSfL --proto '=https' --proto-redir '=https' --max-redirs 5 --max-time 60 --max-filesize 104857600 -w '%{http_code}' -o "$tmp" "$url")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$code" in
      403|408|429) transient=1 ;;
      4[0-9][0-9]) transient=0 ;;
      *) transient=1 ;;
    esac
    if [ "$transient" -eq 1 ]; then
      echo "::warning::SKIPPED (download failed): $url"
      failed=$((failed + 1))
    else
      echo "::warning::SKIPPED (HTTP $code, the attachment URL does not resolve to an asset): $url"
    fi
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

# --- Committed evidence, fork lanes only ---------------------------------
# The same-repo lanes read committed screenshots straight off the checked-out
# head. The fork lanes never check out the fork head, so a committed image
# reaches this runner only over the API. This block runs when HEAD_REPO,
# HEAD_SHA and BASE_SHA are all set (the fork switch); the same-repo caller
# leaves them unset and this whole section is a no-op, so both lanes run one copy.
#
# Trust model: every decision here is made from a TRUSTED GitHub FACT, and the
# changed set and the bytes are BOTH pinned to the event's SHAs -- nothing read
# live can move under the review. The changed-file list comes from the compare
# endpoint pinned to (BASE_SHA...HEAD_SHA), NOT the live pulls/files endpoint
# (which reflects the newest head, so a push landing after the event could list
# a newer revision's paths while the tree lookup stays pinned to HEAD_SHA,
# admitting an unchanged old screenshot as this revision's evidence). No
# fork-controlled string is ever interpolated into an API path or a shell word
# before it has passed a strict allowlist. A filename is validated against a
# fixed character class (no newline, no `..`, `#`, `?`, space, control char, or
# leading dot) BEFORE it is used anywhere; the object is resolved through the
# git TREES API pinned to HEAD_SHA (not the contents API, whose `?ref=` a `#`
# in the path could strip, and whose `type=file` a symlink satisfies); the tree
# entry must be a real blob with a regular file mode (100644/100755) -- a
# 120000 symlink, a 160000 gitlink and a tree are refused by mode, so no in-repo
# symlink can redirect the reader to other bytes; size is checked from the
# trusted tree entry BEFORE download; and the bytes are fetched by the entry's
# own blob SHA over the git blobs API with the raw media type, typed by file(1),
# and capped again on disk. The description is never consulted to decide what is
# evidence.
if [ -n "${HEAD_REPO:-}" ] && [ -n "${HEAD_SHA:-}" ] && [ -n "${BASE_SHA:-}" ]; then
  # HEAD_REPO, HEAD_SHA and BASE_SHA are GitHub-set trusted facts, but validate
  # their SHAPE anyway (defence in depth): a malformed value means the caller
  # wired the switch wrong, and failing closed is safer than interpolating it.
  if ! printf '%s' "$HEAD_REPO" | grep -qE '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
    echo "::error::HEAD_REPO '$HEAD_REPO' is not owner/name shaped; skipping committed evidence."
  elif ! printf '%s' "$HEAD_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    echo "::error::HEAD_SHA '$HEAD_SHA' is not a 40-hex commit sha; skipping committed evidence."
  elif ! printf '%s' "$BASE_SHA" | grep -qE '^[0-9a-f]{40}$'; then
    echo "::error::BASE_SHA '$BASE_SHA' is not a 40-hex commit sha; skipping committed evidence."
  else
    # The per-file byte cap the attachment path uses, reused here both against
    # the trusted tree entry's .size (before download) and against the bytes
    # on disk (after download).
    committed_cap=104857600
    short="$(printf '%s' "$HEAD_SHA" | cut -c1-7)"

    # 1) The changed-file set, pinned to BOTH event SHAs. The live
    # `pulls/$PR/files` endpoint reflects the NEWEST head, so a fork push that
    # lands after the event but before this step would list paths from a newer
    # revision -- and an UNCHANGED old screenshot present in the HEAD_SHA tree
    # would then be admitted as evidence for the reviewed revision. The compare
    # endpoint is pinned to (BASE_SHA...HEAD_SHA), the exact event SHAs, so the
    # changed set and the bytes below both refer to the reviewed revision;
    # nothing read live can move under the review. Records are emitted ONE
    # compact base64 per added/modified file (so no filename can split into
    # two) and the control-character gate lives jq-side (a shell `$(...)` would
    # strip a trailing newline before a shell check could see it).
    #
    # The compare API caps `.files` at 300 entries: past that the list is
    # partial and a needed path could be silently absent, so committed evidence
    # is skipped entirely (fail closed, same shape as the truncated tree).
    compare=""
    compare_ok=""
    for attempt in 1 2 3; do
      if compare="$(gh api "repos/$REPO/compare/$BASE_SHA...$HEAD_SHA")"; then
        compare_ok=1
        break
      fi
      echo "Comparing $BASE_SHA...$HEAD_SHA failed on attempt $attempt."
      if [ "$attempt" -lt 3 ]; then
        sleep "$attempt"
      fi
    done
    if [ -z "$compare_ok" ]; then
      echo "::error::Could not compare $BASE_SHA...$HEAD_SHA after 3 attempts, so committed screenshot evidence cannot be collected; re-run the workflow."
      exit 1
    fi
    files_count="$(printf '%s' "$compare" | jq -r '(.files // []) | length')"
    if ! printf '%s' "$files_count" | grep -qE '^[0-9]+$' || [ "$files_count" -ge 300 ]; then
      echo "::warning::the compare listing has ${files_count} files (the API caps at 300, so it may be partial); skipping committed evidence for this revision."
      files_b64=""
    else
      files_b64="$(printf '%s' "$compare" | jq -r '(.files // [])[] | select(.status == "added" or .status == "modified") | select((.filename | type) == "string") | select(.filename | test("[\u0000-\u001f\u007f]") | not) | {filename, status} | @base64')"
    fi

    # 2) The git TREES API pinned to the head commit -- the trusted, complete
    # map of (path -> type/mode/size/sha) at HEAD_SHA. Fetched ONCE. A
    # truncated tree cannot be trusted to contain the entry we look up, so
    # committed evidence is skipped entirely (fail closed).
    tree=""
    tree_ok=""
    for attempt in 1 2 3; do
      if tree="$(gh api "repos/$HEAD_REPO/git/trees/$HEAD_SHA?recursive=1")"; then
        tree_ok=1
        break
      fi
      echo "Reading the head tree failed on attempt $attempt."
      if [ "$attempt" -lt 3 ]; then
        sleep "$attempt"
      fi
    done
    committed_seen=0
    if [ -z "$tree_ok" ]; then
      echo "::error::Could not read the head tree after 3 attempts; skipping committed evidence (re-run the workflow)."
    elif [ "$(printf '%s' "$tree" | jq -r '.truncated // false')" = "true" ]; then
      echo "::warning::the head tree listing is truncated, so a committed path cannot be resolved reliably; skipping committed evidence for this revision."
    else
      allow_re='^(temp-screenshots|\.github/screenshots)/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*\.(png|jpe?g|gif|webp)$'
      while IFS= read -r rec; do
        [ -n "$rec" ] || continue
        # Decode ONE record; the filename cannot leak into a sibling record.
        path="$(printf '%s' "$rec" | base64 -d 2>/dev/null | jq -r '.filename // ""')"
        [ -n "$path" ] || continue
        committed_seen=$((committed_seen + 1))
        # 3) STRICT allowlist BEFORE the path is used anywhere. Anything with a
        # newline, `..`, `#`, `?`, space, control char, leading dot, or a
        # non-image extension is refused, and never interpolated. The
        # AUTHORITATIVE control-character gate is in the jq filter above
        # (`test("[\u0000-\u001f\u007f]")`), NOT here: a shell `$(...)` STRIPS
        # trailing newlines, so a changed `state.png\n` would arrive here as a
        # clean `state.png`, pass this check, and the exact tree lookup would
        # then select the UNCHANGED sibling `state.png` -- stale bytes admitted
        # as this revision's evidence. jq sees the raw string before any shell
        # normalization, so a record with any control byte is dropped before it
        # is ever decoded. The shell strip below stays as defence in depth for
        # an embedded (non-trailing) control byte, but it cannot be the gate.
        stripped="$(printf '%s' "$path" | tr -d '\000-\037')"
        if [ "$stripped" != "$path" ]; then
          echo "::warning::SKIPPED (path not on the committed-evidence allowlist -- control character): $path"
          continue
        fi
        if ! printf '%s' "$path" | grep -qE "$allow_re"; then
          echo "::warning::SKIPPED (path not on the committed-evidence allowlist): $path"
          continue
        fi
        if [ "$fetched" -ge $((MAX_SHOTS + MAX_CLIPS)) ]; then
          echo "TRUNCATED: image and recording cap reached; committed file not fetched: $path"
          continue
        fi
        # Select the tree entry whose path equals the validated filename
        # EXACTLY. Require a real blob with a regular file mode: 120000
        # (symlink), 160000 (gitlink) and any tree are refused, so an in-repo
        # symlink cannot make unrelated bytes look like this PR's evidence.
        entry="$(printf '%s' "$tree" | jq -c --arg p "$path" '.tree[] | select(.path == $p)' | head -n1)"
        if [ -z "$entry" ]; then
          echo "::warning::SKIPPED (no blob at that path in the head tree): $path"
          continue
        fi
        etype="$(printf '%s' "$entry" | jq -r '.type // ""')"
        emode="$(printf '%s' "$entry" | jq -r '.mode // ""')"
        if [ "$etype" != "blob" ] || { [ "$emode" != "100644" ] && [ "$emode" != "100755" ]; }; then
          echo "::warning::SKIPPED (not a regular file blob -- type '$etype' mode '$emode'): $path"
          continue
        fi
        esize="$(printf '%s' "$entry" | jq -r '.size // 0')"
        if ! printf '%s' "$esize" | grep -qE '^[0-9]+$' || [ "$esize" -gt "$committed_cap" ]; then
          echo "::warning::SKIPPED (blob size ${esize} exceeds the per-file cap): $path"
          continue
        fi
        blob_sha="$(printf '%s' "$entry" | jq -r '.sha // ""')"
        if ! printf '%s' "$blob_sha" | grep -qE '^[0-9a-f]{40}$'; then
          echo "::warning::SKIPPED (blob sha not 40-hex): $path"
          continue
        fi
        # 4) Fetch the bytes by the trusted blob SHA over the git blobs API
        # with the raw media type -- served from api.github.com, no redirect
        # and no fork-controlled string in the request. The blob sha rides in
        # the path already validated as 40-hex.
        fetched=$((fetched + 1))
        tmp="$(mktemp "$FETCH_DIR/committed.XXXXXX")"
        if ! gh api "repos/$HEAD_REPO/git/blobs/$blob_sha" \
          -H "Accept: application/vnd.github.raw" > "$tmp" 2>/dev/null; then
          echo "::warning::SKIPPED (blob download failed): $path"
          skipped=$((skipped + 1))
          failed=$((failed + 1))
          rm -f -- "$tmp"
          continue
        fi
        # Re-cap on the bytes actually written, then type by bytes -- the same
        # rule the attachment path uses; a name never decides the type.
        if [ "$(wc -c < "$tmp" 2>/dev/null || echo 0)" -gt "$committed_cap" ]; then
          echo "::warning::SKIPPED (downloaded bytes exceed the per-file cap): $path"
          skipped=$((skipped + 1))
          rm -f -- "$tmp"
          continue
        fi
        mime="$(file --mime-type -b -- "$tmp")"
        case "$mime" in
          image/png) ext=png ;;
          image/jpeg) ext=jpg ;;
          image/webp) ext=webp ;;
          image/gif) ext=gif ;;
          *)
            echo "::warning::SKIPPED (mime $mime): $path"
            skipped=$((skipped + 1))
            rm -f -- "$tmp"
            continue ;;
        esac
        origin="repository path $path @ $short"
        # A committed GIF is a still whose existence the continuity lens also
        # asks about, exactly as in the attachment path.
        if [ "$ext" = "gif" ]; then
          if [ "$clips" -lt "$MAX_CLIPS" ]; then
            clips=$((clips + 1))
            printf '%s\n' "$origin" >> "$CLIPS"
          else
            echo "TRUNCATED: more than $MAX_CLIPS recordings; one committed file was not listed" >> "$CLIPS"
          fi
        fi
        if [ "$n" -lt "$MAX_SHOTS" ]; then
          n=$((n + 1))
          name="$(printf '%s-%02d.%s' "$NAME_STEM" "$n" "$ext")"
          mv -- "$tmp" "$DEST_DIR/$name"
          printf '%s\n' "$DEST_DIR/$name" >> "$SHOTS"
          printf '%s\t%s\n' "$name" "$origin" >> "$SHOT_MAP"
        else
          echo "TRUNCATED: more than $MAX_SHOTS images; one committed file was not listed" >> "$SHOTS"
          printf 'TRUNCATED\t%s\n' "$origin" >> "$SHOT_MAP"
          rm -f -- "$tmp"
        fi
      done <<< "$files_b64"
    fi
    echo "Committed evidence at $HEAD_SHA: $committed_seen added/modified path(s) seen, $n image(s) kept in total, $clips recording(s) listed."
  fi
fi
