# feature-videos

Sign a release folder of feature-intro clips for the CDN, and check one before upload.

| File | Does |
|------|------|
| `publish.py` | Reads the folder you assembled, hashes every clip and poster where it is, signs the result with the release KMS key, and writes `manifest.json` into that folder. |
| `verify.py` | Re-checks a signed folder: every hash against the bytes on disk, and the signature against the committed release key. |
| `_manifest.py` | The schema, the validation rules, and the runtime's limits. The signing plumbing is loaded by path from `packaging/signing/cli-manifest.py`, the CLI release signer. |

Neither script uploads. `publish.py` prints the `verify.py`, `aws s3 sync` and
CloudFront invalidation commands and stops, so the credentials that can write to a
public origin stay with the human running them. (The signer does call AWS KMS.)

## Layout

Two things, side by side:

```
catalog.json                      one entry per clip (kept OUTSIDE the folder)
dist/feature-videos/<release>/
    monitor-loops.mp4             <id>.mp4 for every entry
    monitor-loops.jpg             <id>.jpg for every entry
```

The catalog stays outside the release folder on purpose: `aws s3 sync` uploads the
whole folder, and the folder must hold nothing the manifest does not sign.

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

`duration_s` is required: nothing here inspects the media, and the dashboard shows
the length from it. `doc` must be in the tips doc allowlist
(`src/kiro_crew/tips_allowlist.py`). The fields are described in
[feature-videos](../../src/kiro_crew/docs/feature-videos.md).

Clips should be H.264 video with AAC or no audio, the formats a browser `<video>`
plays everywhere the dashboard runs. The tool does not check this.

## Sign

```bash
python3 scripts/feature-videos/publish.py \
  --catalog catalog.json \
  --cdn-host videos.example.com \
  --kms-key-arn "$RELEASE_SIGNING_KEY_ARN"
```

`--release` defaults to the version in `pyproject.toml`, and `--release-dir` to
`dist/feature-videos/<release>`. Afterwards the folder holds the media plus a
signed `manifest.json`, and nothing was moved. The manifest is the whole record.

A release folder is immutable: if it already holds a `manifest.json`, publishing
refuses. Changing a clip means cutting a new release, not re-signing this one.

Signing is the CLI artifact manifest's: the same KMS key, `RSASSA_PKCS1_V1_5_SHA_256`
and canonical JSON, through the CLI signer's own code loaded by path, so there is one
signer to audit. The tool checks the KMS key's public half against the committed one
before it signs, records `key_id`, and verifies the signature before writing. One
key means one grant: a principal allowed to sign feature videos can sign a CLI
update manifest.

Publishing refuses, with the reason on stderr and nothing written:

| Refused | Why |
|---------|-----|
| An `id` that is not a lowercase hyphenated slug, or over 92 characters | The id becomes the asset basename, which the dashboard bounds at 96 characters. |
| A missing `<id>.mp4` or `<id>.jpg`, or a symlink in their place | A release folder with a hole in it is not publishable; the bytes hashed must be the bytes uploaded. |
| A file the catalog does not name | It would be uploaded and served under a signature that never covered it. |
| A folder already holding `manifest.json` | A release is never re-signed. |
| A `doc` outside `src/kiro_crew/tips_allowlist.py` | So a clip cannot point at an internal design note. |
| An empty file, or a clip or poster over the dashboard's limit for it | The dashboard drops an entry whose clip is over its cap and refuses an oversize poster. |
| A release that is not `major.minor.patch` with no leading zeros | The only folder shape a dashboard ever asks for. |
| A `--cdn-host` that is not a bare host name or IP, with at most a port | The host goes into every signed URL; a path in it would be served from nowhere. |
| A `duration_s` that is missing, not finite, not positive, or over one hour | The dashboard shows the length, and reads a longer one as unknown. |
| A signed payload, `manifest.json` or entry count over the dashboard's limit | Every dashboard would refuse the release whole. |
| A KMS key whose public half is not the committed one | A mistyped ARN would sign with some other key and produce a folder every dashboard refuses. |

## Verify

```bash
python3 scripts/feature-videos/verify.py dist/feature-videos/0.7.0
```

Recomputes every hash from the bytes on disk, verifies the signature against the
committed release key, and refuses a folder carrying any file nobody signed. It only
reads. Run it before every upload. Pass `--public-key <pem>` to check a folder
signed with another key.

## Upload

Publishing prints the commands and stops. Run them yourself:

```bash
aws s3 sync --dryrun dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws s3 sync dist/feature-videos/0.7.0/ s3://BUCKET/feature-videos/0.7.0/
aws cloudfront create-invalidation --distribution-id DISTRIBUTION \
  --paths '/feature-videos/0.7.0/*'
```

The tool enforces immutability on the folder it signs; the bucket side is yours.
`aws s3 sync` overwrites objects freely, so put the guard where the bytes live: a
bucket policy that denies `s3:PutObject` on an existing key, or S3 Object Lock on
the `feature-videos/` prefix.

## Size limits

Every limit is the dashboard's own number: `_SIGNED_PAYLOAD_MAX_BYTES`,
`_MANIFEST_MAX_BYTES`, `_MAX_ENTRIES`, `_MAX_ENTRY_BYTES` and `_MAX_DURATION_S` in
`src/kiro_crew/feature_videos_manifest.py`, `MAX_POSTER_BYTES` in
`src/kiro_crew/feature_videos_cache.py`. `_manifest.py` carries them as
`RUNTIME_LIMITS`, a test holds them equal to the source, and there is no flag to
loosen one.
