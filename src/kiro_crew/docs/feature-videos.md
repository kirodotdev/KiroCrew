# Feature Videos

Kiro Crew plays a short intro clip for a feature this install has not used yet. Unlike [Feature Tips](feature-tips.md), which are written by a model, a feature video is a recorded clip picked by a fixed rule set — the same install state always selects the same video.

## How It Works

- The catalog is a static list in `feature_videos.py`. There is no generation step and no model call.
- Selection walks the catalog in order and returns the first entry that is enabled, has both its clip and poster shipped on disk, is not yet recorded as seen or dismissed, is satisfied by the running version, and is not withdrawn by a "you already use this" signal.
- An entry whose media is not shipped is withheld, not shown. The dialog opens on the JSON answer alone and fetches nothing until the user presses play, so it cannot detect a missing clip itself -- it would open around a blank player, and the verdict a user then records is permanent. Withholding keeps the entry on offer for the launch after its clip lands.
- Clips and posters are same-origin paths under `/app-assets/feature-videos/`. A path carrying a scheme, `//`, `..`, a percent sign, or a backslash is refused, so a catalog entry can never point the browser off this origin. Remote clip downloads are a separate future change.
- Seen and dismissed are both permanent. There is no snooze: a feature intro that comes back is noise.
- Temporary and incognito sessions get no video, because the state a video records is permanent and instance-wide.
- A clip whose `min_version` is above the running version is skipped, so a video recorded ahead of a release never plays on an older build.

## Controls

| Action | Effect |
|--------|--------|
| Watch a clip to the end | Records `seen`; that video is never offered again. |
| Close the modal | Records `dismissed`; same permanence. |
| `dashboard.feature_videos_enabled: true` in config | Turns the feature ON. It is OFF by default until real clips ship. |

## Adding a Catalog Entry

Append a `VideoEntry` to `CATALOG` in `src/kiro_crew/feature_videos.py`. Order is offer order, so a new entry goes where you want it shown.

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
  feature_videos_enabled: true   # instance-wide switch; DEFAULT false
```

Display state lives in `feature_videos_state.json` beside `tips_state.json`, written with owner-only permissions.

See [Configuration Reference](configuration.md) for the full list.
