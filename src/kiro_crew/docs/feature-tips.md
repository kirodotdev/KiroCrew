# Feature Tips

Kiro Crew surfaces a compact feature tip above the chat composer while a turn is running, pointing at a feature you have not used yet. Tips are personalized from local preferences, active projects, and up to two days of recent history.

## How It Works

- The client waits 10 seconds into a running turn before requesting a tip and shows at most one tip per turn.
- A tip is an in-flow card in the composer's width wrapper: it takes real layout space and pushes the conversation upward instead of covering chat content.
- Tips do not fetch or display in temporary sessions, split view, or the embedded sessions view, and they yield to functional above-composer surfaces such as question cards, queued messages, folder suggestions, and active subagent or workflow progress.
- Candidates include hand-authored actionable tips, generated tips, and a catalog fallback built from allowlisted feature docs. A background model call uses the local context to generate candidates; selection blends random exploration with weighted, newer-biased selection.
- At most one tip is offered at a time. The same offered tip is returned until feedback clears it.
- Displaying, acknowledging, dismissing, or snoozing a tip starts the cadence gate; the default minimum gap is six hours. The client also applies a 20-minute local display gate unless a shorter configured cadence applies.

## Controls

| Action | Effect |
|--------|--------|
| Click the X on a tip | Permanently dismisses that tip and its associated feature documentation when available. |
| Settings → Chat → Feature Tips | Turns Feature Tips off or back on. The toggle is disabled with an instance-config hint when `dashboard.tips_enabled` is false. |
| `dashboard.tips_enabled: false` in config | Instance-wide kill switch. |

## Privacy

Tip generation reads local preferences, projects, and recent history, then uses the configured `dashboard.tips_model` (default `auto`) to generate candidates. The local `tips_state.json` file is written with owner-only permissions and contains generated tips plus their feedback state. Temporary sessions do not fetch or display memory-personalized tips.

## Configuration

```yaml
dashboard:
  tips_enabled: true          # instance-wide switch
  tips_cadence_hours: 6       # minimum gap after tip feedback
  tips_snooze_hours: 48       # delay before a snoozed tip is eligible again
  tips_recency_decay: 0.6     # newer-tip weighting
  tips_model: auto            # model for candidate generation
  tips_explore_ratio: 0.2     # random-selection probability
```

See [Configuration Reference](configuration.md) for the full list.
