# Feature Tips

KiroCrew occasionally surfaces a small tip above the chat composer while the
agent is working, pointing at a feature you have not used yet. Tips are
personalized: a background session reads your recent activity and memory
context to pick features that fit how you actually work.

## How It Works

- Tips appear only during a running turn, about 10 seconds in, as a single
  strip above the input box. They never cover chat content — the strip takes
  real layout space and pushes the conversation up.
- At most one tip is shown per turn, and after a tip is shown the next one
  waits for the cadence window (6 hours by default).
- Candidates come from a catalog built from these feature docs. A background
  model call ranks them against your recent activity; a small share of tips is
  picked at random so less obvious features still get a chance to surface.
- Tips never appear in temporary sessions, split view, or embedded
  composer-less views.

## Controls

Three layers, from one tip to everything:

| Action | Effect |
|--------|--------|
| Click the X on a tip | Permanently dismisses that tip (the feature, not just the wording) |
| Settings -> Chat -> Feature Tips | Per-user toggle; turns all tips off or back on |
| `dashboard.tips_enabled: false` in config | Instance-wide kill switch |

When the instance config disables tips, the Settings toggle is grayed out with
a note saying so.

## Privacy

Tip personalization uses the same memory context that a normal chat turn
already receives, sent to the same model you configured — no new data leaves
your machine beyond what a regular turn sends. The local tips state file is
written with owner-only permissions. Temporary sessions (which promise no
memory reads) never show tips.

## Configuration

```yaml
dashboard:
  tips_enabled: true        # instance-wide switch
  tips_cadence_hours: 6     # minimum gap between tips
  tips_max_count: 5         # tips generated per refresh
```

See [Configuration Reference](configuration.md) for the full list.
