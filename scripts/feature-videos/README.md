# feature-videos

Build and check a signed release folder of feature-intro clips for the CDN.

| File | Does |
|------|------|
| `publish.py` | Turns an input directory into `dist/feature-videos/<release>/` with the media, a signed `manifest.json` and a `SHA256SUMS`. |
| `verify.py` | Re-checks a produced folder: every hash against the bytes on disk, and the signature through the dashboard's own verifier. |
| `_manifest.py` | The shared schema, the canonical byte form both signing and verification hash, and the validation rules. |

Neither script uploads anything or reaches the network. `publish.py` prints the
`aws s3 sync` and CloudFront invalidation commands and stops, so the credentials
that can write to a public origin stay with the human running them.

The runtime side that fetches a published folder is a separate change. Until it
lands, a release folder is inert: build one, verify one, upload one, and no
dashboard reads it.

## Input

Put the media and a `catalog.json` in one directory. Each entry needs an
`<id>.mp4` and an `<id>.jpg` beside it, both named after the entry's `id`.

```json
{
  "entries": [
    {
      "id": "monitor-loops",
      "feature": "monitor-loops",
      "title": "Let one session watch a pull request",
      "description": "One or two plain sentences on what the feature does.",
      "doc": "monitor-loops.md",
      "used_when": ["sel_event_seen:monitor_start"],
      "min_version": "",
      "duration_s": 22.0
    }
  ]
}
```

`duration_s` is optional when ffprobe is installed; without it, set the value or
publishing stops. The catalog fields themselves are described in
[feature-videos](../../src/kiro_crew/docs/feature-videos.md).

## Produce

```bash
python3 scripts/feature-videos/publish.py \
  --input ~/feature-videos-input \
  --cdn-host videos.example.com \
  --kms-key-arn "$RELEASE_SIGNING_KEY_ARN"
```

That writes `dist/feature-videos/<release>/` holding the media, a signed
`manifest.json` and a `SHA256SUMS`. `--release` defaults to the version in
`pyproject.toml`. The folder is assembled under a private sibling name and moved
into place in one step, so the release path holds a complete folder or nothing.

Signing reuses the CLI artifact manifest's trust root: the same offline key,
`RSASSA_PKCS1_V1_5_SHA_256`, and the same canonical JSON. One release carries one
trust root rather than two.

Keys are separated by purpose. `--kms-key-arn` is the production path — the
private half is a non-exportable AWS KMS key held by the release workflow, so it
exists on no disk — and the tool checks that key's public half against the
committed one before it signs. The manifest then records `key_id` as a hint about
which pinned key was used. `--signing-key <path>` signs with a local key for
staging and tests, omits `key_id`, and warns that the folder is not a release.
The dashboard verifies against the pinned key either way, so a staging folder
stays a staging folder wherever it is uploaded.

`signature` is base64 at the manifest's top level and covers canonical JSON of
every other top-level field, nested values included. Editing one byte of the
manifest breaks it. That canonical rule is copied into `_manifest.py` rather than
imported, so the tool runs from a bare checkout and never executes runtime code;
`test/test_feature_videos_publish.py` pins the copy by signing with it and
verifying with the runtime's own verifier.

What publishing refuses, each with its reason on stderr:

| Refused | Why |
|---------|-----|
| An `id` that is not a lowercase hyphenated slug | The id becomes the asset basename and the display-state key. |
| A missing `<id>.mp4` or `<id>.jpg` | A release folder with a hole in it is not publishable. |
| A `doc` outside `src/kiro_crew/tips_allowlist.py` | The allowlist tips use, so a clip cannot point at an internal design note. |
| A clip or poster that is a symlink, a pipe or anything but a regular file | The bytes that get hashed and the bytes that get published must be the same bytes. |
| A file over the cap, 25 MB by default and set by `--max-bytes` | Every dashboard that has not seen a clip fetches it once. |
| Video that is not H.264, or an audio track that is not AAC | Checked with ffprobe when it is installed, skipped with a warning when it is not. A silent clip passes. |
| A duration nobody knows, or one that is not a finite number | Set `duration_s`, or install ffprobe. |
| A destination that already holds anything | A release folder is immutable. Delete it and re-run, or cut a new release. |

## Verify

```bash
python3 scripts/feature-videos/verify.py dist/feature-videos/0.7.0
```

This recomputes every hash from the bytes on disk, verifies the signature against
the committed release key, and refuses a folder carrying a file nobody signed. It
only reads. Run it before every upload.

Pass `--public-key <pem>` to check a folder signed with a staging key.

## Upload

Publishing prints the commands and stops. Run them yourself:

```bash
aws s3 sync --dryrun dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws s3 sync dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws cloudfront create-invalidation --distribution-id DISTRIBUTION \
  --paths '/feature-videos/0.7.0/*'
```

A release folder is immutable. Changing a clip means cutting a new release, not
overwriting a published one. The invalidation is for `manifest.json`, the one
file a consumer re-reads.

## Size caps

These are PUBLISHER limits, and every one is deliberately stricter than what the
runtime accepts. A release that only just fits today's consumer has no headroom
for a consumer that tightens. Publishing refuses and exits non-zero on any of
them, leaving no folder behind, so the failure lands while a person is watching.

| Cap | Publisher default | Runtime accepts | Flag |
|-----|-------------------|-----------------|------|
| Signed payload — the canonical JSON the signature covers, compact | 65536 (64 KiB) | 262144 (256 KiB) | `--max-payload-bytes` |
| Manifest document — `manifest.json` as published, indented so larger than the payload | 262144 (256 KiB) | 1048576 (1 MiB) | `--max-document-bytes` |
| Entry count | 500 | 1000 | `--max-entries` |
| Per media file — one clip or poster | 26214400 (25 MB) | 67108864 (64 MiB) | `--max-bytes` |

The runtime column is `_SIGNED_PAYLOAD_MAX_BYTES`, `_MANIFEST_MAX_BYTES`,
`_MAX_ENTRIES` and `_MAX_ENTRY_BYTES` in
`src/kiro_crew/feature_videos_manifest.py`. That file arrives with the fetch
side, so on a checkout without it the column is the number this tool was
calibrated against rather than one a test can read; the test that asserts every
publisher default sits at or under it skips until the file exists, then binds
automatically.

Raise a flag when a release genuinely needs the headroom; the runtime is the hard
limit, this tool's default is the safe one. `verify.py` takes the same two
manifest flags, so a folder can be re-checked against a different ceiling without
republishing.
