# Feature Videos

Kiro Crew plays a short intro clip for a feature this install has not used yet. Unlike [Feature Tips](feature-tips.md), which are written by a model, a feature video is a recorded clip picked by a fixed rule set: the same install state always produces the same set of *eligible* clips, and one of them is chosen at random.

Clips are hosted, not shipped inside the package. Kiro Crew downloads them in the background and plays them from your own disk — see [Hosted Clips](#hosted-clips).

## How It Works

- The catalog is data, not generated: a signed manifest published for your release, with a small built-in list as the fallback for an install that has never fetched one. There is no model call.
- A clip is eligible when the feature is on, its media is on your machine, it is not yet recorded as seen or dismissed, the running version satisfies its floor, and no "you already use this" signal fires.
- Which eligible clip you get is random. Eligibility is not: a clip you retired, or one for a feature you already use, can never come back.
- Only clips already on your disk are offered, so playback starts immediately and costs no bandwidth. A hosted clip that has not finished downloading waits for the next launch.
- An entry whose media is not shipped is withheld, not shown. The dialog opens on the JSON answer alone and fetches nothing until the user presses play, so it cannot detect a missing clip itself -- it would open around a blank player, and the verdict a user then records is permanent. Withholding keeps the entry on offer for the launch after its clip lands.
- One function checks every clip source. A downloaded clip is served from your own machine under `/feature-videos/<release>/`; a built-in one from `/app-assets/feature-videos/`. Nothing else: your browser never fetches a clip from the CDN, because the only bytes it plays are ones this gateway downloaded and checked first.
- Seen and dismissed are both permanent. There is no snooze: a feature intro that comes back is noise.
- Temporary and incognito sessions get no video, because the state a video records is permanent and instance-wide.
- A clip whose `min_version` is above the running version is skipped, so a video recorded ahead of a release never plays on an older build.

## Controls

| Action | Effect |
|--------|--------|
| Watch a clip to the end | Records `seen`; that video is never offered again. |
| Close the modal | Records `dismissed`; same permanence. |
| `dashboard.feature_videos_enabled: true` in config | Turns the feature ON. It is OFF by default until real clips ship. |
| "Download all" in Settings | Fetches every clip for your release now, without the background rate limit. |

## Hosted Clips

Clips live on a CDN, listed by a **signed manifest** for your release. Kiro Crew
downloads them into `~/.kiro/crew/feature-videos/<release>/` (readable only by you)
and plays them from there.

### What arrives, and when

- At gateway start, a background task fetches the manifest, then downloads any clip
  you do not already have — one at a time, capped at 512 KiB/s so it stays out of
  your way. An interrupted download resumes on the next start.
- Every file is checked against the SHA-256 in the signed manifest before it is
  installed. A tampered or truncated file is discarded, never played.
- A clip that is not downloaded yet is not shown. It becomes eligible on the first
  launch after it lands on disk. Once a manifest has arrived it replaces the
  built-in list, so on a launch where nothing has landed yet, nothing is shown:
  the download runs in the background and the next launch shows what it landed.
- "Download all" in Settings does the same pass immediately, without the rate limit.

### Trust

- The manifest is signed with the same offline key that signs Kiro Crew's own
  installer. If the **signature** does not verify, the whole manifest is discarded
  and the built-in list is used: the signature covers the document, so there is no
  such thing as verifying most of it. A malformed **entry** inside a correctly
  signed manifest is a different case — that entry alone is dropped, the reason is
  logged, and the rest of the list plays.
- The cached manifest on your disk is re-verified every time it is read, so editing it
  cannot change where clips are fetched from.
- A clip and its poster are checked against the manifest's sha256 when they are
  downloaded, and a small receipt beside each file records which sha256 it passed.
  After that the check is presence, byte size and that receipt, not a re-hash: playing
  a clip does not read it twice. The receipt is also how a re-published clip reaches
  you: if a new manifest pins a different sha256 for the same file name, the receipt no
  longer matches and the file is downloaded again. A clip file edited on disk
  afterwards, by something that already has write access to your own home directory,
  is played as it stands. What it cannot do is change where anything is fetched from,
  or point Kiro Crew at a file outside the cache.
- The route that serves a cached file answers only for a plain file directly inside the
  release folder, under the canonical cache directory. A symbolic link is refused rather
  than followed, at the file and at the release folder both, because a manifest names
  files and a link is not a file a manifest can describe. The check is settled on the
  opened file, not on its name: the path the operating system reports for the open file
  must be the canonical one, so a folder swapped for a link after the name was checked
  is a 404, and a file with a second name (a hard link) is not served. The download side
  holds the release folder open the same way while it writes, and only writes through
  that open folder once it has confirmed where it is.
- If there is no manifest for your release, Kiro Crew looks for the newest lower one:
  the same minor line first, then up to three earlier minors. It never looks across a
  major version, since a clip from a different major is likely to show a UI that no
  longer exists.

### Disk use

`dashboard.feature_videos_cache_max_mb` (default 500) is the budget. When the cache
is over it, whole release folders are removed oldest first. The release you are
running is never removed, even if it alone exceeds the budget — deleting the clips
you are about to play would just download them again.

### Turning the download off

Two independent switches:

- `dashboard.feature_videos_enabled: false` (the default) turns the whole feature
  off: nothing is fetched and nothing is shown.
- A managed fleet can forbid the fetch itself with the
  `capabilities.feature_videos_download` policy scope. Then no manifest is requested,
  no clip is downloaded, and nothing streams from the CDN — clips already on disk
  still play. The settings panel reads the answer from `GET /api/feature-videos/status`
  (`download_enabled`), so the controls reflect it rather than failing when pressed.

To fetch the catalog from somewhere else, set `KIROCREW_FEATURE_VIDEOS_MANIFEST_URL`
in the gateway's environment to point at your own copy of the manifest. It must be
`https`, and the signature is still checked. What that relocates is the manifest
fetch only: the host the clips come from (`cdn_base`) is part of the signed document,
so a copied manifest still names the public CDN, and an install with no route to that
host gets no catalog. Serving the clips from your own host needs a manifest signed
with your `cdn_base` in it — a mirror can relocate what it is given, not introduce
new files. This is an environment variable and not a `config.json` key on purpose: the
gateway makes a request to whatever it names, so it belongs to whoever launches the
gateway, not to a file the agent's own tools can edit. Your mirror may answer with a
redirect to another `https` host (an artifact store fronting a bucket, say); the
public CDN may not. One manifest can serve a whole line of releases, as long as the `release` it
declares is one the running build would fall back to (this release, this minor's
`.0`, or the `.0` of up to three earlier minors); a manifest outside that window is
refused with a logged reason rather than cached where the next start cannot find it.

## Adding a Catalog Entry

This is the **fallback** list, used by an install that has never fetched a manifest; a new clip normally goes into the published manifest instead, which needs no release. Append a `VideoEntry` to `CATALOG` in `src/kiro_crew/feature_videos.py`. Order no longer decides what is shown — the pick among eligible entries is random.

```python
VideoEntry(
    id="knowledge-library",
    feature="knowledge-library",
    title="Search your own documents",
    description="One or two plain sentences on what the feature does.",
    src="/app-assets/feature-videos/knowledge-library.mp4",
    poster="/app-assets/feature-videos/knowledge-library.jpg",
    duration_s=20.0,
    doc="knowledge-library-how-it-works.md",
    used_when=("config_key_set:knowledge.enabled",),
    min_version="",
)
```

Rules the catalog enforces, each of which drops the entry with a logged warning rather than breaking the endpoint:

- `id` is a slug and doubles as the state key and the asset basename; keep `src` and `poster` as `<id>.mp4` and `<id>.jpg`.
- `doc` must be listed in `tips_allowlist.py`, the same allowlist tips use — a video cannot point at an internal design note.
- `src` and `poster` must pass `validate_asset_path`.
- `min_version` is optional and must parse as a version when present.

Place the clip and its poster in `website/public/app-assets/feature-videos/`.

## Declaring a `used_when` Signal

`used_when` names deterministic probes. Any one of them firing withdraws the video, because an intro for a feature already in use is worse than no intro. A probe that raises, or a signal nobody registered, counts as "not used" — the clip still plays, and the reason is logged.

Shipped signals:

| Signal | Fires when |
|--------|-----------|
| `tips_feedback_exists` | The user has reacted to a feature tip in any way. |
| `artifacts_nonempty` | The artifact library holds at least one artifact. |
| `sel_event_seen:<tool_name>` | A recent audit-log row names that tool, e.g. `sel_event_seen:monitor_start`. |
| `config_key_set:<dotted.path>` | The user set that key in `config.json` or `config.local.json`. Presence in the file, not the effective value, so a shipped default never fires it. |

To add one, register a function in `_PROBES` (no argument) or `_PARAM_PROBES` (the part after the first `:` is passed in). Keep it cheap: probes run on a polled route, at most once per `/api/feature-videos/next` request, and only for entries no earlier check has already ruled out.

## Configuration

```yaml
dashboard:
  feature_videos_enabled: true      # instance-wide switch; DEFAULT false
  feature_videos_cache_max_mb: 500   # disk budget; 0 = no cap
```

The manifest mirror override is the environment variable
`KIROCREW_FEATURE_VIDEOS_MANIFEST_URL` (see Governance above), not a config key.

Display state lives in `feature_videos_state.json` beside `tips_state.json`, and downloaded clips in `feature-videos/<release>/` — both written with owner-only permissions.

See [Configuration Reference](configuration.md) for the full list.
