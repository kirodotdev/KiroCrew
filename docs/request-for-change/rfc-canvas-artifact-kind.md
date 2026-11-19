---
title: Canvas artifact kind — a static whiteboard saved as JSON Canvas
status: draft
author: dodgeblaster
created: 2026-11-16
last-audited: 2026-11-16
audited-at: ff01b2edfb      # main; the implementation was read at #35618 head 0f92a3a4ca
doc-pr: 38136
implementation-prs: [35618]
tracking-issues: [36836]
supersedes: []
superseded-by: []
---

# RFC: Canvas Artifact Kind — a Static Whiteboard Saved as JSON Canvas

**Status:** draft. Nothing is on main. The whole implementation is open as
[#35618](https://github.com/kirodotdev/KiroCrew/pull/35618), which passes every
check and waits on this document: its review asked for a maintainer's design
sign-off because the change adds a concept and a file format people will keep
on disk. Acceptance is recorded in this document; #35618 merges after it.

## 1. Summary

Add a `canvas` artifact kind: a FigJam-style whiteboard of stickies, shapes,
text, sections and connectors, drawn and edited on the artifact's detail page.
It has no AI features. A canvas is stored as a [JSON Canvas 1.0](https://jsoncanvas.org/spec/1.0/)
document with Kiro's own fields added, saved with the `.canvas` extension, so
Obsidian and other JSON Canvas readers open it and Kiro opens theirs. The
editor saves as you go, like FigJam, without adding a numbered version per
change.

This document asks a maintainer to decide four things: the file format (§5.1),
autosave (§5.2), the kind's place in the artifact model (§5.3), and the
review overrides the author recorded on #35618 (§9).

## 2. Motivation

Artifacts hold documents, pages, widgets, images and dashboards, but nothing a
person can lay out spatially: a sprint board, a flow, a map of ideas. Today
that work leaves Kiro for FigJam or Miro, and an agent can neither read nor
write it there.

A board format that is plain, documented JSON also gives later work a stable
base. An agent can write a valid board using only the standard JSON Canvas
fields, which are small and widely known. Any AI feature on top of the canvas
is a separate proposal (§4).

Main already has two Excalidraw pieces, neither of which is a saved artifact:
the composer's sketch pad (`website/src/components/SketchDialog.tsx`), which
inserts a PNG plus `.excalidraw` JSON as a chat attachment, and a read-only
scene renderer (`website/src/lib/excalidrawScene.ts`). §10 explains why the
canvas does not build on them.

## 3. Goals

- A person creates a canvas from the Artifacts page (**New canvas**), or
  imports a `.canvas` file through **Add Artifact**, and edits it in place.
- The file opens in Obsidian unchanged, and a board made in Obsidian opens in
  Kiro, keeping every field Kiro does not use.
- Edits never silently overwrite a change made elsewhere (another tab, an
  agent, a file on disk).
- A `.canvas` file from anywhere is treated as untrusted input.

## 4. Non-goals

- AI features in the canvas (generating boards, an agent cursor, smart
  layout). The canvas is static.
- Live multi-person editing.
- Rendering Obsidian `file` and `link` nodes. They are kept and written back,
  not drawn.
- A hand-drawn look. The canvas aims for FigJam's clean, bold style.
- Letting a person pick `canvas` in the generic kind selector
  (`USER_SELECTABLE_KINDS`).

## 5. Design

The full behaviour is specified in the **Canvases (`kind="canvas"`)** section
that #35618 adds to `docs/system-specs/modules/artifacts.md`. This section
records the decisions and why.

### 5.1 File format: a JSON Canvas superset

```json
{
  "kirocanvas": 1,
  "nodes": [
    { "id": "s1", "type": "text", "x": 0, "y": 0, "width": 200, "height": 200,
      "text": "Ship it", "color": "#DCD0FA",
      "shape": "sticky", "swatch": "lavender", "bold": true }
  ],
  "edges": [
    { "id": "c1", "fromNode": "s1", "toNode": "s2", "toEnd": "arrow",
      "line": "elbow", "dashed": true }
  ]
}
```

- Every shape is a standard `text` node and a section is a `group` with its
  title in `label`. Each carries the standard id, position, whole-number size,
  text and one resolved `color`, so any JSON Canvas reader can draw the board.
- What JSON Canvas has no field for goes in added fields: `shape`, `swatch`,
  `hex`, `fill`, `weight`, `border`, `borderColor`, `borderHex`, `ink`,
  `inkHex`, `fontSize`, `bold`, `strike`, `align` and `locked` on nodes, and
  `swatch`, `line`, `dashed` and `route` on edges. Array order is stacking
  order, back to front.
- `"kirocanvas": 1` marks a file Kiro created and versions Kiro's added
  fields. JSON Canvas itself has no version field.
- The extension is `.canvas`, the one Obsidian opens. Kind inference maps it to
  `canvas` (the extension table in `src/kiro_crew/artifact_store/rules.py` on #35618).

**Tested against Obsidian.** Before choosing the format, the author opened a
sample board in Obsidian, edited it and diffed what Obsidian saved. Obsidian
kept every added field, including the list-valued `route` and the top-level
marker. It changed only what was edited, plus four things the reader handles:

| What Obsidian writes | How Kiro reads it |
|---|---|
| A preset colour `"1"`-`"6"` while our `swatch` stays stale | `color` wins; presets map to red, orange, yellow, green, teal, violet (`PRESETS` in `website/src/components/canvas/jsonCanvas.ts`) |
| A new card with no `shape` | A rectangle with default styling |
| `toEnd` left out, since `"arrow"` is the spec's default | An arrow |
| The edited card moved to the end of `nodes` | The new stacking order |

A line with only a start arrow, which another editor may write, is read the
other way round so its route stays attached to the right ends.

### 5.2 Saving: autosave, written back against the source

**Autosave, not Edit and Save.** The canvas has no edit mode. It saves a moment
after the last change as a live save (`snapshot: false`), so moving shapes
never adds a numbered version; the page's Snapshot control still takes one.
Whiteboards people already use (FigJam, Miro, Excalidraw, Obsidian's canvas)
all work this way, and dragging a shape does not read as editing a document
that needs saving.

**Never overwrite another writer.** Every save sends the `expected_token` of the
content it was based on, so a change made elsewhere is refused with a 409,
autosave stops, and the person chooses Reload or Keep mine. A canvas that
mirrors a file through `source_path` has no `content_token` and opens
read-only, because a save could not be checked against the file.

**Write back only what changed.** An imported canvas keeps a frozen copy of the
file as read. On save, each node and edge starts from its original object and
only keys the person changed are replaced, so exact colours, presets, omitted
defaults and unknown fields from Obsidian or its plugins stay as they were.
Foreign `file` and `link` nodes keep their place in the stacking order.

The cost of autosave is that a save can arrive while the page is mid-edit:
updates from elsewhere, echoes of the page's own saves, undo past a newer
version, and leaving while a save is pending all need handling. Most of
#35618's review rounds were in this area, and the spec pins each case. §10
weighs explicit save as the alternative.

### 5.3 The kind in the artifact model

- `canvas` joins `ALLOWED_KINDS` and stays out of `USER_SELECTABLE_KINDS`
  (both in `rules.py`). Inference returns it only for a `.canvas` path, so an
  ordinary JSON save is never re-typed as a canvas.
- **New canvas** stores an empty document with `kind="canvas"` named, so the
  first save is not re-typed as `json`.
- The editor opens read-only for a past version, a file-backed canvas, a canvas
  whose shown text was masked by the detail read's redaction (an autosave would
  write the placeholders back), and a document it cannot parse. The masking
  case has one exception: content masked only in bytes this page itself saved
  stays editable. Letting the owner edit masked text in general is
  [#36836](https://github.com/kirodotdev/KiroCrew/issues/36836).

## 6. Security considerations

A `.canvas` file can come from any app, so the reader treats it as untrusted:

- Colours must match strict hex or preset forms; preset lookups use
  `Object.hasOwn`, so a colour like `"constructor"` is not a preset.
- Coordinates and sizes must be finite numbers.
- Source nested deeper than 100 levels (`MAX_SOURCE_DEPTH` in
  `jsonCanvas.ts`) reads as unreadable rather than exhausting the stack.
- `file` and `link` nodes are never rendered or followed; Kiro only writes
  them back.
- Text is drawn as plain text, never HTML or Markdown.

The canvas adds no endpoint and no new trust boundary: it reads and writes
through the existing artifact routes, with their redaction and token check.

## 7. Migration plan

There is nothing to migrate: no canvas has been saved on main. The plan is one
step.

- **Phase 1 — #35618 merges** once this document is `accepted`.
  Exit criteria: **New canvas** on the Artifacts page creates a `canvas`
  artifact; its download opens in Obsidian; a `.canvas` file saved by Obsidian
  imports as a canvas and saves back with Obsidian's fields unchanged
  (`website/src/test/canvasFormat.test.tsx` reads the file Obsidian saved).

## 8. Backward compatibility

- Existing kinds read and save as before.
- One visible change outside the canvas: the artifact detail page now asks the
  leave guard before **Open full chat** and **Back to library** for every
  kind (`sendNav` in `useArtifactDetailPageCompanion.ts`), so an edited
  markdown or text artifact shows the discard prompt there too. Before, those
  two exits left without asking.
- `"kirocanvas": 1` is the format's first version. A later change to Kiro's
  added fields bumps it, and the reader keeps reading version 1.

## 9. Decisions to ratify

The design, UX and First Principles review lanes pass on #35618 only through
overrides the author recorded, and the Opus lane's two-buttons-per-row rule
through an owner decision. A maintainer accepting this document ratifies them,
or records the toolbar decision under `docs/decisions/`:

| Lane | Decision | Recorded at |
|---|---|---|
| Design | The visual design (sticker look, toolbars, sticky notes, dock) is worked out with the design team, not in this lane | [#35618 comment 6809138138](https://github.com/kirodotdev/KiroCrew/pull/35618#issuecomment-6809138138) |
| UX | Same decision as Design | [#35618 comment 6809138138](https://github.com/kirodotdev/KiroCrew/pull/35618#issuecomment-6809138138) |
| First Principles | A FigJam-style board on a format the product owns, deliberately not built on Excalidraw (§10) | [#35618 comment 6836361848](https://github.com/kirodotdev/KiroCrew/pull/35618#issuecomment-6836361848) |
| Opus (`max-two-buttons-per-row`) | The selection toolbars keep FigJam-style formatting controls: six on the text and shape toolbars, three on the line toolbar | [#35618 comment 6889143154](https://github.com/kirodotdev/KiroCrew/pull/35618#issuecomment-6889143154) |

## 10. Alternatives considered

**Kiro's own format (`.kirocanvas`).** The first version of #35618 used
`{"kind":"kirocrew-canvas","schema":1,"shapes":[],"connectors":[]}`. It was
simpler to read, but no other tool opens it. Rejected for the superset, which
costs only a converter at the file boundary (`jsonCanvas.ts`; the editor's
model is unchanged).

**Build on Excalidraw**, which main already ships for the sketch pad. Rejected
by the owner's decision on #35618
([comment 6836361848](https://github.com/kirodotdev/KiroCrew/pull/35618#issuecomment-6836361848)):

- It is a different product. The canvas is a FigJam-style board agreed with
  the design team, and it is the base for later canvas work with first-party
  UI for driving sessions, which needs an editor and document model Kiro
  controls rather than a third-party whiteboard.
- Excalidraw's look is hand-drawn (rough.js), the opposite of the FigJam style
  the canvas aims for. It has no sticky notes.
- Its scene format is defined by Excalidraw's code, not an open spec, and
  Obsidian's canvas does not read it.
- `@excalidraw/excalidraw` is about 1 MB loaded lazily, and its fonts expect a
  network asset path (see the comment at the top of `excalidrawScene.ts`).
- The sketch pad answers a different need: a quick drawing attached to a chat
  message, not a saved board. It stays as it is.

**tldraw, draw.io, Miro.** tldraw's `.tldr` is a snapshot of tldraw's internal
store, tied to its code. draw.io is XML for diagrams rather than a whiteboard.
Miro and FigJam have no file format to adopt.

**Explicit save, like markdown** (Edit, then Save or Cancel). Far less code:
edits go to a private copy, and a change made elsewhere only matters when the
person presses Save, where the existing token check refuses it. Rejected
because it breaks the whiteboard feel the canvas copies, and a forgotten Save
loses a layout. The leave guard would cover most exits, but not a closed tab.
Switching later is possible without a format change.

## 11. Open questions

- Obsidian renders card text as Markdown; Kiro shows it as plain text. Should
  Kiro render a safe Markdown subset in cards? Not needed for Phase 1.
