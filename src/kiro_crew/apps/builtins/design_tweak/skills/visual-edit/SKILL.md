---
name: visual-edit
description: Interpret and act on batched visual edit requests produced by the Design Tweak app. Use when the user references a visual selection, a "design tweak" request, an edit request number, or asks you to apply pending visual edits.
always: false
---

# Visual Edit — Design Tweak request handling

The **Design Tweak** app lets a designer visually pick element(s) in a live
preview and attach natural-language comments. Comments accumulate into a
**request**, and the designer sends the whole batch at once. One request file
therefore contains **many comments as sub-items**:

```
~/.kiro/crew/apps/design-tweak/data/queue/<timestamp>-<id>.json
```

**A request is a batch. Work every comment in it, and report per comment.**

## The loop

1. **Read the request file.** The prompt names the request id; if not, read the
   newest file in the queue dir. `state` will be `"sent"` — a `"draft"` request
   has not been handed to you yet, so leave drafts alone.
2. **Work `comments[]` in order.** Each entry is an independent edit with its own
   `cid`, `comment`, `sourceFile`, and `selection`. Do not merge them into one
   change, even when two comments touch the same file.
3. **For each comment**, resolve the target, make the edit, and report against
   **that comment's `cid`** (see below).
4. When every comment is done, tell the user what changed, grouped by comment
   number.

## Reporting progress — always per comment

Each comment has its own progress bubble in the preview, so notes must be
addressed to a `cid`:

```
POST /apps/design-tweak/api/thread?id=<requestId>&cid=<commentId>
body: {"role": "agent", "text": "Editing styles.css — uppercasing .section-title"}
```

Post one note when you start a comment, and one per meaningful step. Keep each to
a single short line. When that comment is finished, post a final note carrying a
status so its dot turns green:

```
POST /apps/design-tweak/api/thread?id=<requestId>&cid=<commentId>
body: {"role": "agent", "text": "Done — added text-transform: uppercase", "status": "done"}
```

The request's own status is **derived**: it flips to `done` on its own once every
comment is `done`. Never set it directly.

> **Never write to the request file yourself.** Do not edit `state`, `status`,
> `sentAt`, or any other field by writing the JSON — the panel derives the
> request's badge from its comments, and a hand-written `state` desynchronises
> them. Progress is reported *only* through `POST /thread`. The file is the
> app's state, not a scratchpad.

- Omitting `&cid=` posts a request-level note. Use that only for something that
  spans the whole batch ("rebuilding, one moment").
- A request-level `{"status": "done"}` marks *every* comment done. It is a
  fallback, not the normal path — prefer per-comment reporting so the designer
  can see which specific edits landed.
- Do **not** clear a request unless the user asks to dismiss it — clearing
  removes the pins. (`POST /clear?id=<requestId>` archives it to History.)

## Resolving each comment's target source

Every comment carries its own project context stamped by the backend — prefer it
over searching, and note that comments in one batch can point at **different
files** (the designer may have navigated between pages mid-batch).

- `projectId` / `projectRoot` (request level) — which project, and the absolute
  path to its root.
- `sourceFile` (per comment) — absolute path to the file that was being previewed
  when that comment was made. **May be empty**, and that is not an error: it is
  only set when the preview was a served *file*. A project previewed from its own
  dev server has routes, not file paths (`/pricing` is not a path on disk), so
  there is nothing honest to put here.

**Use the element's own `source` block first — it is the most precise signal, and
the only one available when `sourceFile` is empty:**

- `confidence: "high"` — `file:line:col` from the build-time plugin. Trust it.
  Resolve `file` against `projectRoot`.
- `confidence: "medium"` — framework internal (React Fiber). Verify against `htmlSnippet`.
- `confidence: "low"` — no source map. If `sourceFile` is set, locate the node
  inside that one file by `htmlSnippet`, `classes`, `id`, or text. If it is also
  empty, search `projectRoot` for the snippet and **confirm before editing**.

If `projectRoot` is empty too, a dev-server URL (`devServer`) was used with no
registered project; fall back to the active project directory + `htmlSnippet`.

## Follow-up comments

A comment with a non-empty `followUpTo` refines an **earlier** comment, whose
`cid` it names. That original comment usually lives in a *different, already-sent*
request — look for it in the queue dir, then in `../handled/`.

Read the origin comment and its `thread` before editing: the follow-up is
phrased as a delta ("actually make it 32px"), so it only makes sense against what
was already done. Report the follow-up against its **own** `cid`, not the
original's — the original request is finished and must not be mutated.

## Comments that ADD or DELETE an element — stamp `data-kiro-cid`

A comment's pin is anchored to the element it was filed against. Two kinds of
comment break that anchor, and both need one extra step from you.

**When your change CREATES the element the comment asked for**, put the comment's
`cid` on it:

```html
<!-- comment 4.2: "add a Cancel button next to Save" -->
<button data-kiro-cid="c-8f2a91" class="btn-secondary">Cancel</button>
```

The overlay looks for `[data-kiro-cid="<cid>"]` **before** anything else, so the
pin leaves the placeholder position it was floating at and re-homes onto the real
element as soon as the preview reloads. Without the attribute the bubble stays
stranded where the user clicked, pointing at nothing.

Stamp exactly **one** element per cid — the outermost node you created for it. If
a comment asked for several elements, stamp the container.

**When your change DELETES an element**, do nothing extra. The pin falls back to
that element's former parent on its own. Do not stamp a sibling to "keep the pin
alive" — a bubble on the wrong element is worse than one on the parent.

Leave the attribute in place. It is a durable link between the source and the
comment that produced it, the same role `data-kiro-source` plays, and it is what
lets the pin survive later reloads. Only remove one if the user asks.

## Request schema

```json
{
  "type": "visual_edit_batch",
  "id": "1720560000000-a1b2c3",
  "number": 3,
  "state": "draft | sent",
  "projectId": "e4b4aa4c",
  "projectRoot": "/Users/me/Developer/my-site",
  "createdAt": "2026-07-29T23:00:00Z",
  "sentAt": "2026-07-29T23:04:00Z",
  "thread": [],
  "comments": [
    {
      "cid": "1720560000111-d4e5f6",
      "index": 1,
      "status": "new | sent | done",
      "comment": "increase spacing between these cards",
      "createdAt": "2026-07-29T23:00:00Z",
      "projectId": "e4b4aa4c",
      "sourceFile": "/Users/me/Developer/my-site/pricing.html",
      "previewUrl": "http://127.0.0.1:52431/e4b4aa4c/pricing.html",
      "followUpTo": "",
      "selection": {
        "mode": "single | multi",
        "elements": [
          {
            "tag": "div",
            "id": "",
            "classes": ["card", "card--pricing"],
            "locator": "main > section:nth-of-type(2) > div:nth-of-type(3)",
            "boundingRect": { "x": 120, "y": 340, "width": 280, "height": 180 },
            "source": {
              "file": "src/components/PricingCard.tsx",
              "line": 42,
              "column": 6,
              "confidence": "high | medium | low"
            },
            "htmlSnippet": "<div class=\"card card--pricing\">…</div>",
            "relevantStyles": { "display": "flex", "gap": "12px" }
          }
        ]
      },
      "thread": [
        { "role": "user", "text": "increase spacing between these cards", "ts": "2026-07-29T23:00:00Z" }
      ]
    }
  ]
}
```

Comment numbering shown to the designer is `<request number>.<index>` — comment
`index: 1` of request `number: 3` is **3.1**. Use that form when talking to them.

## Editing guidance

- Scope each edit to the selected element(s). Do not refactor surrounding code.
- For `mode: "multi"`, apply that comment to every element in `elements` — they
  were selected as a set (e.g. "increase spacing between these cards" applies to
  the shared container or gap).
- The `file` path inside `source` is relative to the **project root**, not the
  Kiro Crew workspace. Combine it with `projectRoot`.
- Prefer editing the exact `line:col`; use `htmlSnippet`, `classes`, and `id` to
  disambiguate when a component renders in a `.map()` loop (same source line,
  many instances).
- Never guess a file when confidence is `low` — locate it first, then confirm.
- Keep edits minimal and reversible. The panel reloads the preview automatically
  each time a comment flips to `done`, so the designer sees results as you go.
