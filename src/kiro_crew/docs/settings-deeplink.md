# Settings deep links

Ask Kiro Crew where a setting lives and it can answer with a link that opens the
right Settings tab and flashes the control, instead of describing a path through
the UI for you to retrace. Every control the dashboard renders is enumerated in
one generated file, so the answer names a switch that exists rather than one that
used to.

## The registry

`settings-registry.generated.json`, in this directory, is generated from the
dashboard's settings panels. It holds one object per control under a `settings`
key:

| Field | Meaning |
|---|---|
| `id` | Stable identity, `<tab>.<kebab-label>` |
| `label` | English label the panel renders |
| `tab` | Settings tab the control lives on, e.g. `display` |
| `route` | The finished link that opens and flashes the control |
| `description` | The in-panel help text, when the control has one |
| `configKey` | The config key the control writes, when it maps to exactly one |

Many controls carry no `description`, so treat it as a bonus signal and never as
a required field.

## Using a route

Copy `route` verbatim. It is already complete — tab, any sub-selection, and the
highlight parameter — so there is nothing to assemble and nothing to guess. For
the "Highlight recent sessions" entry, the route is:

```
/settings/display?highlight=display.highlight-recent-sessions
```

Present it as a link labelled with the control name — a markdown link whose
target is the route, introduced by the tab it lives on ("Settings → Display").
Naming the tab keeps the answer useful even where the link is not clickable.

Two things about a route are easy to get wrong by hand, which is why it is
shipped prebuilt rather than described:

- **A sub-selection is a path segment, not a query parameter.** Every per-channel
  control lives behind one — `/settings/channels/teams?highlight=…`. Drop the
  `teams` segment and the panel holding the control never opens, so the link
  lands on an empty tab and highlights nothing.
- **`%3A` in a route is an encoded `:`** and belongs there. Leave it alone.

A route is a dashboard path, so it is clickable only where a dashboard path
resolves. On a chat channel (Slack, Telegram, Teams, …) prefix it with the
gateway's own address — `http://localhost:5476/settings/display?highlight=…` for
a default local install — or name the tab and the control in words if you do not
know the address.

## Matching a request to a control

A user describes a setting the way it reads on screen, not the way it is keyed.
Match their phrasing against `label` first, then `description`, and prefer the
entry whose `tab` fits what they were talking about — "the sidebar looks wrong"
points at `display` long before it points at `chat`.

Two habits matter more than the matching itself:

- Answer with **one** control. A list of five plausible routes moves the search
  back onto the user.
- If nothing matches, say so. Never invent an `id` or edit a `route`: an id that
  is not in the registry opens the tab and silently flashes nothing, which reads
  as a broken link rather than as a missing feature.

## Why the highlight value is not always the id

`highlight=` accepts two forms, and the registry has already picked the better
one for each control:

- `highlight=key:<configKey>` finds the control by the config key it writes. It
  works in every language.
- `highlight=<id>` finds it by its English label, which is all that is available
  for a control with no single config key. On a dashboard running in another
  language the tab still opens and the flash is simply skipped.

Both are already baked into `route`. The reason to know the difference is
diagnostic: a link that opens the right tab without flashing anything is the
second form meeting a translated dashboard, not a stale registry.

## What the registry does not cover

- Controls whose label is computed at runtime rather than written in the panel.
  They are skipped by generation, so a control missing from the file is not
  necessarily missing from the UI.
- Everything outside Settings — the Developer page, Agent Capabilities, and the
  App Store each have their own routes and are not enumerated here.
- Config keys with no control at all. Those are set in `config.json` or with
  `kirocrew config`; see [configuration.md](configuration.md).

## Regenerating

The file is a build artifact of the dashboard, refreshed by `npm run gen:settings`
in `website/` together with the UI's own registry. A checked-in copy that no
longer matches the panels fails the frontend test suite, so the two cannot drift
apart across a release.
