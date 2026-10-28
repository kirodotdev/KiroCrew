---
title: "App file renderers: an installed app can render a file type the file viewer has no renderer for"
status: draft
author: flamingquaks
created: 2026-10-28
last-audited: 2026-10-28
audited-at: 0cf608a17
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App File Renderers

> **Status:** `draft`. Acceptance is requested from a maintainer who owns App Kit
> and the file viewer. Open question 1 also asks for a ruling against
> `rfc-everything-is-an-app` Phase 3. Nothing is on main. The baseline was
> verified at `0cf608a17` (2026-10-28):
> - `FileType` in `website/src/components/FileRenderers.tsx` is a closed union.
> - `detectFileType()` in the same file maps extensions from hardcoded sets and returns
>   `'code'` for anything it does not know.
> - `Contributes` in `src/kiro_crew/apps/manifest_contributes.py` holds only
>   `commands`, `sessionControls`, `panelTabs` and `fileMenuItems`.
>
> No issue, PR or RFC in this repository proposes an app-supplied file renderer.

## Summary

Add a manifest contribution, `contributes.fileRenderers`, with which an installed
app renders a file type that core has no renderer for. The host mounts the
app's ESM entry inside the shared file viewer (`ContentRenderer`), through the
same in-process `AppHost` that `contributes.panelTabs` uses. It hands the entry the
file's content as read through the host's own redacted read route, `/api/file-read`.

The contribution is deliberately narrow:
- It is additive. An app may claim only extensions that today fall through to the
  generic `code` view, never a type core already renders.
- It is opt-in. The renderer starts as an "Open with" choice, and becomes the
  default for an extension only when the owner picks it.
- It runs only on click. No automatic open path mounts it.
- It is read-only. It renders content and does not write.

## Motivation

### Current state

- **The file viewer's renderer set is closed.** Each supported format was added to
  core by hand: Excalidraw in #1064 (issue #1062), spreadsheets in #3875, and
  inline media in #4027. #9563 made `ContentRenderer` plus `detectFileType` the
  one dispatcher behind the side panel, the file explorer and the artifact view,
  so a renderer added at that layer reaches every one of those surfaces.
- **Apps can attach to a file, but they can't render it.**
  - `contributes.fileMenuItems` (#7955) adds a menu row that POSTs the file's
    *path* to the app.
  - `contributes.panelTabs` (#7975) mounts an app bundle in the chat side panel.

  Together they let an app show a file in a separate tab, but only after a menu
  action, and the host has no way to bring that tab forward.
- **Other formats are hitting the same limit.** #17936 (PlantUML) and #12959
  (local HTML that loads scripts) each ask for one more format, and each is being
  treated as its own decision about core. #21494 asks that clicking a file in chat
  open a preview, which raises the cost of every format the preview cannot draw.
- **The question has been raised once and left open.**
  `rfc-chat-core-extraction` §7 Q2 asked whether apps should register renderers
  (there, for chat roles), and answered "internal first; plugin API is a separate
  RFC". That RFC was never written.

### Problems

1. **A format core does not know is shown as raw source.** A `.drawio` diagram
   opens as XML, even when the user has a draw.io app installed and enabled.
2. **The workaround takes two steps and gives no feedback.** The user opens the
   file, picks a menu row, then switches to a separate side-panel tab. The menu
   row can show a toast, but it cannot focus that tab.
3. **Built-in renderers don't scale and are limited by size.** #1064 rejected the
   upstream Excalidraw package because it unpacks to 46.8 MB and fetches fonts from
   a CDN. Many formats have renderers in that weight class, and the root page
   promises to make no network request.
4. **It is the defect Tenet 8 describes.** "Anything that renders or interprets is
   an app", and "an app gets the same powers a built-in page has". Today the
   Excalidraw renderer holds a power no app can hold.

## Goals

- An enabled app can render a file type core has no renderer for, in the same
  place the user already looks at files.
- The user stays in control. The built-in view is the default until the owner
  chooses otherwise, and it is always one click away.
- No new trust tier and no new privilege. The renderer runs in the same host as
  `panelTabs`, under the same permission-scoped API, and receives only content the
  user opened, after redaction.
- A manifest that does not declare `fileRenderers` behaves exactly as it does today.

## Non-goals

- **Replacing a renderer core already has** (Markdown, HTML, JSON, CSV, images,
  PDF, Office, Excalidraw, media, or highlighted source code). That is a
  third-party app owning a core surface, which `rfc-everything-is-an-app` Phase 3
  blocks.
- **Editing.** A renderer cannot write the file. Saving needs its own design for
  conflicts, approvals and the file-change card.
- **Chat transcript rendering.** File chips in the transcript keep opening the side
  panel, which then uses this mechanism. Rendering inline in the transcript is out
  of scope.
- **The aws-control drive view.** It keeps its own transport, per the boundaries
  recorded in #9563.
- **Isolating app code.** This RFC uses the existing in-process host and does not
  change it. See Security considerations.

## Design

### 1. Manifest field

```json
{
  "contributes": {
    "fileRenderers": [
      {
        "id": "diagram",
        "label": "Diagram",
        "icon": "Layers",
        "entry": "renderer.mjs",
        "extensions": ["drawio", "dio"]
      }
    ]
  }
}
```

| Field | Type | Rule |
|---|---|---|
| `id` | string | Slug, `[a-z][a-z0-9_-]{0,63}`, unique within the app |
| `label` | string | Shown in "Open with" and in Settings, max 60 characters |
| `icon` | string | One of the `panelTabs` icon names; others fall back to a generic glyph |
| `entry` | string | ESM bundle relative to `ui/`, the same base as `panelTabs[].entry` |
| `extensions` | string[] | Lowercase, no dot, 1–8 entries, each claimable (§2) |

An app may declare at most 4 renderers. The field is covered by the admission
signature, like the other `contributes` fields. A signed manifest that declares it
must set `minKiroCrewVersion` to the release that adds it.

### 2. Which extensions an app may claim

The host owns the eligibility rule; a manifest cannot widen it. An extension is
claimable only if both of these hold:

- `detectFileType()` reaches its final `return 'code'` fallback for it, and
- it is not in the host's reserved set. That set contains every extension the
  code viewer highlights as a language, plus `txt`, `md`, `html`, `svg`, `json`,
  `xml`, `yaml` and `yml`.

The backend keeps its own copy of the reserved set for install-time validation.
A parity test pins the two copies together, the same way `MAX_COMMANDS_PER_APP`
is mirrored today.

A manifest that claims an unclaimable extension is **refused at install**, naming
the extension, rather than installed with that extension silently dropped.

If core later adds a built-in renderer for an extension an app has claimed, the
built-in one wins. The app's renderer stays available under "Open with".

### 3. Consent and permission

- The install and trust prompt lists each renderer as *"Can read the content of
  `.drawio`, `.dio` files you open with it"*. This is the install-time
  declaration that the `fileMenuItems` contract ("the PATH is sent, never the
  CONTENT") says any content handoff needs.
- The app receives content only when the user opens a matching file with its
  renderer. The grant does not add `/api/file-read`, or any other file route, to
  the app's token scope.

### 4. Activation: an option first, a default only by choice

- When a renderer matches the open file, the file viewer header gains an **Open
  with** control. It lists the built-in view (`Source`) first, then each matching
  app renderer.
- **Settings → Files → Default viewer** lists each claimed extension and who
  renders it. Only the owner can change it, and the default is `Source`. Choosing
  an app there means a user click on that file type opens straight into the app's
  renderer, which solves Problem 1.
- **Only a user click mounts an app renderer.** The agent open-file bridge (#19411)
  and every other automatic open path always use the built-in view. This keeps
  #19411's rule that an automatic open mounts no viewer able to make a request of
  its own.
- If two enabled apps claim the same extension, both appear under "Open with". If
  the app chosen as default is disabled or uninstalled, that extension's default
  goes back to `Source`.

### 5. Mount contract

- `ContentRenderer` resolves a renderer for the file. When the file opens in an app
  renderer, it reads the content through `/api/file-read`, so the owner-view
  redaction applies as it does for every core view. It then mounts the entry
  through `AppHost` with these props:

  ```ts
  interface FileRendererProps {
    path: string        // display path, as the viewer already shows it
    content: string     // redacted UTF-8 content
    truncated: boolean  // true when the read hit the viewer's size bound
    theme: 'light' | 'dark'
  }
  ```

- The mount sits inside an `ErrorBoundary` with a first-render timeout. If the
  renderer throws or times out, the viewer falls back to `Source` and shows a
  notice naming the app.
- `Source` is always available in the header, so a broken or misleading renderer
  never hides the file.
- When the file changes on disk, the viewer passes fresh content through the same
  path it already uses to re-read a file. The app does not poll.

## Migration plan

| Phase | Change | Exit criteria |
|---|---|---|
| 1 | Backend: parse and validate `fileRenderers`, the reserved set and its parity test, signing, and listing it on the install prompt | A manifest claiming `drawio` installs and its renderer shows in `GET /api/apps`. One claiming `md` or `py` is refused with the extension named. A manifest without the field installs byte-identically. |
| 2 | Frontend: the **Open with** control and the `AppHost` mount in `ContentRenderer`, with `Source` as the only default | Opening a claimed file with the app renderer mounts the entry with redacted content. A throwing entry falls back to `Source` with a notice. The open-file bridge never mounts an app renderer. |
| 3 | Settings: a per-extension default viewer, owner-only | With an app chosen as default, a user click opens into it. Disabling that app resets the default to `Source`. A non-owner cannot change it. |
| 4 | Docs: `manifest-reference.md`, and a reference app (a draw.io viewer) listed in the App Store | The reference app renders `.drawio` from a chat file chip in one click once the owner has chosen it as default. |

Phases 1–2 ship value on their own (an "Open with" choice), and phase 3 can be
dropped without breaking them.

## Backward compatibility

- **Additive and off by default.** A gateway without this change ignores the field
  through `AppManifest.from_dict`'s unknown-field handling, so an app that declares
  it still installs and simply gets no renderer.
- **Existing defaults are untouched.** No current extension changes how it renders,
  because claimable extensions today show only as `code`.

## Security considerations

- **No new code-execution exposure.** As #7975 records, `AppHost` is same-realm and
  same-origin, with no sandbox, so app pages and panel tabs already run app code in
  the dashboard's origin. A file renderer is one more mount of the same kind.
- **The new thing is a data flow:** the content of a file the user opened reaches
  app code. It is bounded by:
  - the install-time declaration (§3);
  - click-only activation (§4);
  - extensions limited to formats core doesn't render (§2);
  - redaction applied by the host before the handoff (§5);
  - a read-only contract.
- **These bounds are not containment.** Once the content is mounted, the app can
  send it to its own backend. This RFC does not claim otherwise, and it does not
  move any control into an app.
- **Relation to `rfc-everything-is-an-app` Phase 3.** That phase gates a third-party
  app *owning a core surface* until `rfc-app-sandbox-isolation` Phases 1–3 land.
  This RFC argues that it stays clear of that gate:
  - the file viewer remains core's;
  - an app fills only a slot core leaves empty;
  - it does so only by the owner's explicit choice;
  - `Source` is never removed.

  Open question 1 asks a maintainer to rule on that.
- **No new network request from the root page.** An app renderer's entry loads only
  when it is mounted, not at page load.

## Alternatives considered

- **Keep adding built-in renderers per format.** This is the route Excalidraw
  proved. It doesn't scale, is held back by bundle size (#1064), and puts every
  format decision on core maintainers.
- **Let a `fileMenuItems` reply focus a panel tab** (for example
  `{ "openPanelTab": "<id>" }`). This is the smallest possible change and it fixes
  the silent click. But it is still two steps, and the file stays raw in the viewer.
  It is worth doing on its own, and it does not conflict with this RFC.
- **A sandboxed iframe renderer** (`srcDoc`, as `McpAppFrame` uses). It would give
  real isolation, but it differs from how every other app surface mounts, and it
  belongs to the app-isolation roadmap, not this RFC.
- **An edition extension seam** (`website/docs/extension-seams.md`). It would reach
  only build-time editions, not apps installed at runtime.

## Open questions

1. **Phase 3 ruling.** Is an additive, owner-chosen, unclaimed-extension-only
   renderer clear of `rfc-everything-is-an-app` Phase 3, or must it wait for
   `rfc-app-sandbox-isolation` Phases 1–3?
2. **Reserved set.** Should the reserved set be the code viewer's language map
   (it grows on its own) or a fixed list reviewed in its own pull requests?
3. **Size bound.** Should the renderer get the viewer's current read bound with
   `truncated`, or should it refuse files above the bound and show `Source`?
4. **Remote crews.** Should a renderer mount for a file opened from a remote crew
   pane, or only for files on the local gateway?
