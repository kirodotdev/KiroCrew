---
title: App widgets that any chat can draw (`contributes.widgets`)
status: draft
author: pearcekieser, with kirocrew-contributor
created: 2026-11-25
last-audited: 2026-11-25
audited-at: 8dc1f33cab
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App Widgets

> **Status:** `draft`. Nothing in this document is on main. The baseline was
> read at `8dc1f33cab`. This document specifies the "message cards" item of
> section 8 of
> [rfc-app-contribution-points.md](rfc-app-contribution-points.md) in full, as
> section 9 of that document asks of a new point. A prototype of Phase 1
> follows as its own pull request against the same issue.

## Summary

An installed app can declare widgets in its manifest. A widget is a small view
that the app's own code draws from a JSON payload. Any session, in any chat,
can then draw that view inline in an answer with one tag:

```html
<mcwidget use="issue-radar:issue-card" title="Issue #40452">
{"number": 40452, "title": "Render chart fences", "state": "open", "labels": ["tier:T3"]}
</mcwidget>
```

The agent writes only the payload. The app's code runs in the shared sandboxed
app frame (`src/kiro_crew/apps/app_frame.py`), so it gets the same isolation as
a link-preview card or a message action. When the app is missing or the payload
is wrong, the host draws a fallback card instead of an empty frame.

## Motivation

### Current state

Inline UI in chat has three sources today, and none of them lets an app ship a
view that other sessions reuse.

| Source | Who writes the view | Where it draws | Limit |
|---|---|---|---|
| `<mcwidget>` with an HTML body | The agent, every time | Inline in the answer | The whole page costs output tokens on every draw, and each copy drifts |
| MCP Apps (`ui://` resources) | The MCP server | In the tool call row | Needs the server's Share switch first (`src/kiro_crew/docs/mcp-apps.md`), never in the answer text, no channel fallback |
| App contribution points | The app | Hover card, popover, side panel, composer | No point draws inside an answer (`docs/app-kit/manifest-reference.md`) |

Verified at `8dc1f33cab`:

- The widget tag parsers read only `title` and `slug`. See `parseWidgetAttrs` in
  `website/src/hooks/useBlockAssembler.ts` and `_parse_attrs` in
  `src/kiro_crew/widget_parse.py`. A `use` attribute is ignored.
- Every complete widget block renders as agent HTML through `WidgetFrame`, or
  as markdown through `MarkdownWidget`, in the `widget` case of `BlockRenderer`
  in `website/src/components/MarkdownRenderer.tsx`.
- Every widget body is registered as an artifact by `parse_widgets` and the
  registration loop in `src/kiro_crew/widget_artifacts.py`.
- `Contributes` in `src/kiro_crew/apps/manifest_contributes.py` has no widget
  list. The `register_frame_point` calls at the end of
  `src/kiro_crew/apps/app_frame.py` register `linkPreviews`, `messageActions`,
  `linkOpen`, `composerActions` and `selectionActions`.

### Problems

1. **Cost.** An agent that shows the same kind of view many times, such as an
   issue card in a daily digest, writes the full HTML each time. The data the
   view shows is a small part of that HTML.
2. **Drift.** Each session invents its own version of the view. A fix to the
   look reaches no earlier or parallel session.
3. **No reuse across sessions.** An app that knows its data best, such as Issue
   Radar for an issue, cannot give other sessions a view of that data. Its
   pages and panel tabs live outside the conversation.

## Goals

- An app declares widgets in `app.json`. Installing and enabling the app makes
  them available to every session on the host.
- An agent draws an app widget inline with one tag and a JSON payload.
- The app's code runs in the shared app frame with no capability and no
  network image source in Phase 1.
- A missing app, an unknown widget, a bad payload or a frame that fails draws a
  fallback card that says why.
- The tag is the existing `<mcwidget>` tag, so the streaming, fence and parity
  rules of both parsers stay as they are.

## Non-goals

- Live data in a widget. A widget that fetches from its app's backend needs a
  second grant entry, and Phase 3 designs it.
- Actions from a widget that change state, such as "close this issue". The
  roadmap's `card.action` stays out of this document.
- Replacing agent-written HTML widgets. They keep working unchanged.
- Replacing MCP Apps. A tool result that carries a `ui://` resource keeps
  rendering in its tool row.

## Design

### Manifest

```json
"contributes": {
  "widgets": [
    {
      "id": "issue-card",
      "title": "Issue card",
      "description": "One GitHub issue: number, title, state and labels.",
      "entry": "widgets/issue-card.mjs"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `id` | Storage-safe slug, unique within the list (`_PANEL_TAB_ID_RE`, section 2.11 of the contribution-points RFC) |
| `title` | 1 to 40 UTF-16 units, measured with `_mirrored_len` |
| `description` | Optional, at most 200 UTF-16 units. Tells an agent what the payload holds |
| `entry` | ESM module relative to the app's `ui/` directory, refused by `_app_path_refusal` |

An app declares at most 16 widgets. A field with the wrong JSON type is
refused, never coerced. The list is signed with the rest of `contributes`, and
`widgets` joins `BROWSER_ONLY_CONTRIBUTIONS` in
`src/kiro_crew/apps/startup_modules.py`.

### Markup

```html
<mcwidget use="<app name>:<widget id>" title="<optional title>">
<one JSON object>
</mcwidget>
```

- `use` names an installed app and one of its widget ids. The app part is a
  kebab-case app name, and the id part matches the frame point id rule
  `[a-z][a-z0-9_-]{0,63}`, so neither part can carry a path.
- The body is one JSON object of at most 16,384 bytes after trimming. The host
  parses it once, and the frame gets the parsed object.
- The payload lives in the body, not in an attribute. Both parsers accept only
  `name="value"` attributes with no `"` inside the value, so a JSON attribute
  would break on its first string. The body also keeps the existing streaming
  placeholder: an incomplete widget draws `WidgetPlaceholder` until its close
  tag arrives.

### Frame

`widgets` is a new frame point, registered with `register_frame_point` like
`composerActions` is. Its resolver returns the declared entry of an enabled
app, and only for a widget whose grant the owner approved (see Grant). Its
script defines this module contract:

```js
export function activate(ctx) {
  // ctx = { appName, point: 'widgets', theme() }
  return {
    render(props, view) {
      // props: the parsed payload. view: { widgetId, theme, resize({ height }) }
      document.body.textContent = `#${props.number} ${props.title}`;
      view.resize({ height: document.body.scrollHeight });
    },
    theme(next) {},
    dispose() {},
  };
}
```

The host side runs this sequence:

1. The block parser sees `use` and hands the block to `AppWidget` instead of
   `WidgetFrame`.
2. `AppWidget` resolves `use` against the app list the dashboard already loads
   (`api.listApps()`, the source every frame point reads).
3. It mints a frame for `widgets/<id>` and loads it with `loadMintedFrame`. The
   `init` message carries `capability: null`, and the host never renews one.
4. On `ready`, the host sends `widget.render { props, theme, widgetId }` once.
   A theme change sends `theme`, and unmount sends `dispose`.
5. The frame reports its height with `widget.resize`. The host clamps it to
   between 40 and 800 CSS pixels.

### What the frame gets, and never gets

| Given | Never given |
|---|---|
| The payload of its own tag, at most 16 KB | Other messages, the transcript, the session key |
| The dashboard theme | A frame capability, so no `ctx.fetch` and no app backend |
| Its own app's UI files, by GET (the frame relay rule) | The dashboard token or cookies (opaque origin, section 2.2) |
| The host's link bridge on a user click | Network images: the `widgets` point's policy is `img-src 'self' data: blob:` |

The narrower image rule matters. Every other point allows `img-src https:`
(`frame_csp` in `src/kiro_crew/apps/app_frame.py`), because a card shows
avatars. A widget's payload comes from the agent and can hold text from the
conversation. An image URL that the app builds from it would carry that text to
any host. So the `widgets` point serves the frame with no network image source.

### Grant

The image rule does not make the frame sealed. The module can still navigate
its own frame to another host with the payload in the address. No frame policy
that a browser enforces refuses that, and the host's `onNavigatedAway` closes
the frame only after the new document loads. So the payload can reach any host
that the app's code names.

Every other point that is handed conversation-derived text (`linkPreviews`,
`messageActions`, `composerActions`, `selectionActions`, `linkOpen`) is gated
by an owner grant. A widget is gated the same way:

- Each declared widget is one entry, `<id>:render`, in a new staged family,
  `widgets`, in the app's approved-grants file. It is registered in
  `apps/point_grants.py` like the other point families, but explicit-only, like
  `frameSources`.
- An install approves the widgets the manifest declares. A host from before this
  change installs an app without reading `contributes.widgets`, so its owner never
  sees them. A set stored then approves no widget, and each one waits on the app's
  page. An update that adds an
  id holds that widget back until the owner approves it on the app's page,
  under "Chat widgets". A title, description or entry change keeps the
  approval.
- The resolver mints no frame for a widget that is not approved, so its module
  never runs. The chat draws the fallback card, which links to the app's page.
- The approval text says what the grant gives: the widget gets the data the
  agent writes for it, which can include conversation text, and the app can send
  that data to other sites.

### Fallback card

The host draws a fallback card, never a blank frame, in each of these cases:

- No enabled app has the name in `use`.
- The owner has not approved the widget. The card links to the app's page.
- The app has no widget with that id.
- The body is not a JSON object, or is over 16 KB.
- The mint is refused, or the frame does not say `ready` within 10 seconds.

The card shows the title, the reason in one line, and the payload in a closed
`<details>` block. The reader keeps the data even when the view is gone.

### Artifacts

A widget with `use` is not registered as an artifact in Phase 1. Its body is a
payload, not a page, so the artifact page would show raw JSON. Phase 2 stores
the reference, the payload, the app version and a static snapshot together.

This is an accepted Phase 1 gap. Section 8 of the contribution-points RFC says
each message card stores a static fallback, so history stays readable after the
app is uninstalled. Phase 1 stores none. A transcript that holds a `use` widget
from an app that was later uninstalled draws only the fallback card, with the
raw payload under its `<details>` block. Phase 2's snapshot closes the gap.

### Discovery

An agent learns an app's widgets from the app's own skill. Apps already ship
skills, and every session finds them through the skills index. The `widgets`
skill documents the `use` form and says to read the app's skill first. Phase 3
adds a generated index of installed widgets, so an agent can find a widget with
no app skill.

### Channels

A channel such as Slack has no frame. Phase 1 changes no channel code. Phase 2
adds an optional `fallback` template per widget, which the host fills from the
payload and sends as text.

## Migration plan

### Phase 1: static widgets (the prototype pull request)

- Manifest field, validation, signing, entry path refusal.
- The `widgets` frame point with the image policy above.
- The `widgets` grant family, one `<id>:render` entry per widget, held back on
  an update that adds one.
- `use` in both tag parsers. A `use` widget is skipped by artifact
  registration.
- `AppWidget` with the fallback card, theme changes and height reports.
- Documentation in `docs/app-kit/manifest-reference.md`, the app-kit platform
  spec and the `widgets` skill. An example app under
  `docs/app-kit/examples/`.

Exit criteria:

- An app with a valid `widgets` entry installs, and a manifest with a bad
  entry is refused with a message that names the field.
- `<mcwidget use="app:id">{...}</mcwidget>` draws the app's module in a frame
  whose `sandbox` is `allow-scripts` and whose `init` carries no capability.
- The mint for an unknown widget id, a widget the owner has not approved or a
  disabled app answers 404.
- Each fallback case draws the fallback card with its reason.
- `parse_widgets` and `parseBlocks` agree on `use` for the shared fixtures.

### Phase 2: payload schema, artifacts, channels

- Optional `propsSchema`, a bounded JSON Schema subset that the host checks
  before `widget.render`. A payload that fails gets the fallback card with the
  first failing path.
- Artifacts that store `{use, props, appVersion}` and a static HTML snapshot
  that the frame sends after its first render.
- The optional `fallback` text template for channels.

Exit criteria: a payload that breaks the schema never reaches the frame; an
artifact page for a `use` widget draws the live widget, or the snapshot when
the app is gone; a Slack reply carries the filled template.

### Phase 3: discovery index and live data

Blocked on open question 3.

- A generated index of installed widgets with their descriptions, read through
  the `widgets` skill.
- `ctx.fetch` to the app's own backend behind a second entry in the `widgets`
  family, `<id>:fetch` per widget, and the widening rule of section 2.7 of the
  contribution-points RFC.

## Backward compatibility

Additive. A manifest with no `widgets` key loads and signs as today. A
`<mcwidget>` with no `use` attribute renders as today. Before Phase 1, a host
that sees `use` ignores it and draws the JSON body as markdown, which is the
fallback a reader can still read.

## Security considerations

- **Trigger.** The host's block parser detects the tag in agent output. The
  user does not click anything, so the data given is kept small: one payload,
  bounded, with no reach out of the frame.
- **Untrusted payload.** The payload is agent output and can hold text that a
  tool result put there. The app's module must treat it as untrusted and set
  text with `textContent`, never `innerHTML`. The example app does this.
- **Isolation.** The frame has an opaque origin, no capability and no network
  image source. Its `connect-src 'self'` keeps fetches on the gateway, and the
  frame holds no capability and no cookie for the gateway's authenticated
  routes. The module can still navigate its own frame away with the payload.
- **Disclosure.** Because of that navigation, a widget draws only with the
  owner's `<id>:render` grant (see Grant). An update that adds a widget waits
  for the owner, as every other point handed conversation text does.
- **Admission.** A widget renders only for an app that is installed, enabled
  and admitted by Allow Third-Party Apps or `apps_trusted`. Disabling the app
  makes every later mint fail.
- **Volume.** Each widget is a frame. `AppWidget` does not mount its frame
  until the block comes near the viewport, so a long transcript does not start
  dozens of frames at load.

## Alternatives considered

1. **Use MCP Apps.** It renders in the tool row, not where the agent puts it in
   the answer. It needs the Share switch for each server, and a channel gets
   nothing. It stays the right tool for a view that is a tool's result.
2. **Inline the app's HTML into the agent widget frame.** It is less code, but it
   runs app code under the agent widget's policy, which allows script CDNs, with
   no per-app identity. The contribution-points RFC puts every frame-backed app
   point on one frame model.
3. **The agent copies HTML from the app's skill.** It works today, but it keeps
   problems 1 and 2.
4. **A host fence per view type, such as ```chart (#40452).** It is right for a
   few core types. It does not let an app ship its own view. Host-owned views
   stay host fences. Shipping them as widgets of a built-in app is ruled out by
   the `no-new-builtin-apps` rule in `AUTOSDE.yaml`.

## Open questions

1. **Name.** Section 8 of the contribution-points RFC calls this point
   `messageCards`. This document uses `widgets`, because the tag and the
   artifact kind already say widget. Which name do the maintainers want?
2. **Payload cap.** Is 16 KB the right cap, given a digest can hold many
   widgets?
3. **Live data.** Is a per-widget `fetch` grant the right shape, or does live
   data belong to the app's MCP tools only?
