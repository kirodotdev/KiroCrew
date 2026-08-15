---
name: computer-use
description: Read and drive native desktop applications through the accessibility layer — list on-screen apps, snapshot one window as a numbered element tree, then click / type / set a value / scroll / drag / run a named action, by element index or by screen coordinates. Use for work in a desktop app rather than a web page. macOS only; off unless the user enabled it in Settings.
triggers: desktop, desktop app, native app, app window, on screen, click button, type into, accessibility, a11y, AXUIElement, computer use, drive the app, Finder, Preview, TextEdit, Excel, Word, System Events, !browser, !web page, !playwright
---

# Computer Use — driving native desktop apps

You have MCP tools that read and operate the **user's real applications** through
the operating system's accessibility layer. This is not a browser: use it when the
work lives in a desktop app (a spreadsheet, a PDF viewer, a native internal tool,
a dialog box), and use `playwright-cli` (the `web-browse` skill) for web pages.

Two things to internalise before your first call:

- **Address elements by index, from a snapshot you were just shown.** That is the
  path to prefer for everything: it activates the control directly, it is checked
  against drift, and the mouse pointer does not move. Coordinates exist as a
  fallback for canvases, maps and custom-drawn UI that expose no element — see
  [Coordinates and dragging](#coordinates-and-dragging-the-fallback-not-the-default).
- **It is off unless the user turned it on** (Settings → Computer Use, macOS only).
  A refusal saying so is a real configuration answer, not a transient error —
  relay it and stop; do not retry.

## The loop

**1. Find the app.**

```
computer_list_apps()
```

Returns the on-screen applications with their bundle ids, pids and window titles.
Skip this if the user named an app you can pass straight through — `app` accepts a
display name (`"Finder"`, `"Preview"`) or a bundle id
(`"com.apple.finder"`), matched case-insensitively.

**2. Snapshot the window. Do this FIRST, every turn — with a screenshot.**

```
computer_get_state(app="Finder", screenshot=True)
```

**Do NOT pass `screenshot=false` here.** Omitting the argument already captures one
(the operator's Settings default), and that capture is what opens the user's **live
view**: the dashboard mirrors the JPEG into a floating panel, which only appears once
a frame exists. Turning it off on the first call leaves the user watching a blank
space while you drive their machine. Passing `screenshot=True` explicitly is fine and
harmless if you want to be sure.

The capture is not for your benefit — you get a file PATH, not an image, and you
should keep reading the outline. One frame opens the panel; it stays open for the rest
of the task.

Optional: `text_limit` (per-element text cap), `max_tree_nodes`, `max_tree_depth`,
`screenshot` (bool). You get a numbered outline:

```
App=com.apple.finder (pid 1041)
Window: "Documents", App: Finder.

0 window "Documents" @ x=0,y=0 900x600
  1 splitgroup
    2 scrollarea
      3 button "Back" [AXPress] @ x=18,y=12 28x24
      4 textfield "report" (editable) <focused> @ x=120,y=52 300x24
      5 row "Q3 numbers.xlsx" (selected) @ x=8,y=90 880x20
      7 textfield <secure>

Window origin on screen: x=220,y=118 (900x600). Element frames above are
relative to it — add the origin for a screen point.
Focus: element 4 (AXTextField "report").
Selected text: [Q3]
```

Reading one line: the number at the start is the `element_index` you pass to
every action. `[AXPress]` and friends are the actions that element advertises.
The indentation is containment, so you can tell a toolbar button from a table
cell. Then, in order:

- **`(editable)`, `(selected)`, `(expanded)`, `(disabled)`** — state the role
  cannot carry. `(editable)` is the one to check before typing: a read-only
  field looks identical to a writable one otherwise, and typing into it
  succeeds while the text goes nowhere. If a text field has no `(editable)`,
  it will not accept input — find the one that does instead of retrying.
- **`<focused>`** — the caret is here. "Type this in" usually means this element,
  so pass ITS index — there is no indexless form (see the note under the action
  table).
- **`@ x=…,y=… WxH`** — position and size, **relative to the window**, in
  pixels. Absent when the element exposes no geometry (ordinary) or the window
  rect could not be read.

The trailing lines: the **window origin** is what converts a frame to a screen
point (`computer_click(x=…, y=…)` takes SCREEN coordinates — add the origin, do
not pass a frame straight through); **Focus** names the focused element; and
**Selected text** is what the user has highlighted, which is what a request like
"rewrite what I selected" refers to.

A `<secure>` element shows no title, no value, no traits and no frame — only
that it exists.

**3. Act by index.**

| Tool | Use it for |
|---|---|
| `computer_click(app, element_index)` | press a button, checkbox, menu item, link, row |
| `computer_type_text(app, element_index, text)` | type text into that element. `element_index` is **required** — see the note below |
| `computer_set_value(app, element_index, value)` | replace a field's whole contents in one step |
| `computer_press_key(app, element_index, key)` | a key or chord — `"return"`, `"tab"`, `"escape"`, `"cmd+s"`, `"cmd+shift+a"`. **Paste (`cmd+v`) is refused** — the clipboard cannot be inspected, so use `computer_type_text` with the literal text |
| `computer_scroll(app, element_index, direction, pages?)` | scroll a scrollable area (`up`/`down`/`left`/`right`) |
| `computer_perform_action(app, element_index, action)` | run one of the element's own advertised actions when nothing above fits |
| `computer_click(app, x, y)` | click a point when the target has no element — see below |
| `computer_drag(app, from_x, from_y, to_x, to_y)` | a canvas stroke, a slider sweep, a range selection, a reorder |

**Every one of these needs an `element_index`, and the keyboard tools are the ones
to remember.** There is no "type into whatever is focused" form: an unnamed target
has no role or subrole, so the secure-field check cannot inspect it, and an indexless
keystroke would land in a focused password box. That applies to `computer_press_key`
too — `press_key("tab")` can *move* focus onto a password field, and the next
keystroke would go there. So name the field you mean; if you want to tab through a
form, address each field by index instead of tabbing blind. `computer_click` is the
only exception, and only because it takes coordinates as the alternative.

Every action returns a **refreshed** tree, so after a click you already have the
new indices — do not call `computer_get_state` again just to re-read them. The same
goes for window position and size: they are in the snapshot header you were already
shown. **Re-probing something the last response already told you is the most common
wasted turn.**

**Refresh the user's view as you go.** Action results carry the structure but no
pixels, so the live-view panel freezes on your last screenshot while you work. After
a step that visibly changes the screen — a window opened, a dialog appeared, a file
saved, a page navigated — call `computer_get_state(app=…, screenshot=True)` once to
push a fresh frame. Use judgement rather than doing it after every keystroke: typing
five fields is ONE visible change, not five, and each screenshot costs time and
tokens. The test is "would the user see something different now?", not "did I just
call a tool?".

### Coordinates and dragging: the fallback, not the default

`computer_click` takes **either** `element_index` **or** both `x` and `y` — never
both and never neither; supplying both is refused, because the two name different
targets and there is no rule for which should win.

Reach for coordinates only when the outline has nothing to address: a drawing
canvas, a map, a timeline, a chart, a custom-drawn control. An element index is
better whenever one exists — it is verified against UI drift, and a coordinate is
delivered to whatever happens to be at that point *now*, so a window that reflowed
after your snapshot will take the click somewhere you did not mean. Re-snapshot
before a coordinate click if anything has changed.

Optional on both: `mouse_button` (`left`/`right`/`middle`), `click_count` (1-3 for
single/double/triple), and `click_method`:

- **`auto`** (the default) — element index → the accessibility press; coordinates →
  an app-scoped mouse event. Correct almost always; do not override it without a
  reason. `auto` will **never** pick the pointer-moving path, so leaving it alone is
  always the pointer-safe choice.
- **`app_post`** — send the click to the target app at that point *without moving the
  user's mouse*. This is what makes clicking a background window safe.
- **`accessibility`** — force the press path; requires `element_index`.
- **`global`** — **moves the user's real mouse pointer** and clicks there. You have
  to ask for it by name; nothing resolves to it implicitly — that naming
  requirement is the only thing between an ordinary click and the user's cursor.
  Every use is separately recorded in the audit log. Ask for it only
  when a click has to be physically real (a Dock item, a menu-bar extra, UI that
  ignores posted events) — say so in your reply *before* you use it, because the
  user's cursor will jump out from under their hand, and never use it as a
  first attempt or a retry after an ordinary click failed for some other reason. If
  it is refused, do not retry: use `app_post` or find another route.

- **`sky_click`** — clicks a window that is **behind other windows**, without
  raising it and without moving the pointer. Ask for it by name when `app_post`
  reached the app but nothing happened. That is the signature of an app whose
  renderer does its own hit-testing and so ignores a posted click while another
  window is in front — browser-based and iPad-ported apps behave this way (Chrome,
  VS Code, Slack, Freeform are the ones you will meet). It is the
  one method built on a private Apple API, so it can stop working on a future macOS —
  when it is unavailable the refusal says so and names `app_post`. Do not reach for it
  first; reach for it when a covered window is the reason a click did nothing.

`computer_drag` is coordinate-only — no accessibility action expresses a sweep
between two points — and it takes the same `mouse_button` / `click_method` options.

**4. Release when you are finished with the app.**

```
computer_end_turn()
```

Drops the cached snapshots. Call it when the desktop part of the task is done. It
is cheap and it prevents a stale-index refusal later in the conversation.

## A screenshot has TWO purposes — keep them straight

This trips models up, so be explicit about which one you are serving:

1. **The user's live view (usually why you want one).** Capturing a screenshot is
   what makes the floating panel appear and update, so the user can watch you work.
   This costs you almost nothing: you get a file PATH, not an image, and you do not
   read it. **Ask for it on your first snapshot and after each visible change.**
2. **Your own perception (rarely).** Actually READING the file costs ~8,000 tokens
   and is a last resort — the outline is your channel.

So "take a screenshot" and "look at a screenshot" are different acts. Do the first
liberally; do the second only when the tree genuinely cannot answer the question.

## When something does not work: change the MECHANISM, not the arguments

This is the rule that separates a two-call fix from a twenty-call loop. When an
action fails or nothing visibly changed, ask yourself one question before the next
call:

> **Am I varying arguments on the same mechanism, or switching to a genuinely
> different one?**

Nudging a coordinate by 20px, retrying the same `element_index`, or re-issuing the
same `click_method` are all the SAME mechanism. **Two failures on one mechanism is
the signal to switch — not a reason to try a third variation.**

The ladder, in order. Go down one rung per failure; never repeat a rung:

1. **`element_index`** on the control itself (the default, and right ~90% of the time)
2. **A different element** — the row or cell that CONTAINS your target, or the
   control's parent; icon-only buttons often only respond one level up
3. **`computer_perform_action`** with an action the element actually advertises —
   read the `[AXPress]`-style list in the outline rather than assuming
4. **Keyboard** — `computer_press_key`. Menus, dropdowns, date pickers and
   scrollbars are frequently keyboard-reachable when they are click-hostile
5. **Coordinates** — `x`/`y` from the element's own position, not from the screenshot
6. **`click_method: "sky_click"`** if the window is covered by another window
7. **Stop and tell the user what you tried.** Two sentences, naming the rungs. That
   is a better outcome than a twentieth call

**Never** re-run a rung that already failed, and never escalate preemptively — do not
reason "this is Electron, so clicking will fail, so I will start at coordinates."
React to an observed failure, do not predict one.

## Do not report success you have not observed

An action that returned without an error is **not** proof it landed. The refreshed
tree that comes back with every action is your evidence — read it and name what
changed: a new value, a dialog that appeared, a menu that closed, a button that
became disabled.

- **If nothing in the tree changed, the action probably did nothing.** Say so and go
  down the ladder. Do not report success.
- **A target that VANISHED is usually success, not failure.** A button that is gone
  after you clicked it, a dialog that closed, a row that disappeared after delete —
  the element being unfindable is the expected outcome. Do not "retry" it.
- Do not go looking for evidence that cannot exist: the Cursor Motion overlay is
  invisible to screenshots, so its absence proves nothing.

The most common failure in desktop automation is reporting success on a silently
dropped action. The second most common is retrying an action that already worked.

## Prefer the outline; read the screenshot only if you must

The tree is the primary channel and it is usually sufficient. When a screenshot is
attached you get a **path**, not an image:

```
Screenshot: /var/folders/.../kirocrew-computer-shots/shot-1769472013411.jpeg
  (1280x604 jpeg, 24.2 KB) — read it with the fs_read tool only if the tree is
  insufficient.
```

Open it with the file-read tool **only** when the outline genuinely cannot answer
the question — a chart, a rendered document, a layout problem, or a control the
accessibility layer did not expose. Reading it costs roughly 8,000 tokens. If the
user asked "show me", just give them the path; the dashboard renders it.

Pass `screenshot=false` only when nobody is watching and you purely need
structure — a long mechanical loop over many elements, for instance. Prefer leaving
it on: the cost of capturing (not reading) one is small, and it is what keeps the
user's live view alive.

## Reading the refusals correctly

These are **answers**, not failures. Relay them and adapt; do not loop.

| You see | What it means | What to do |
|---|---|---|
| a value ending in `…` | the text was cut at `text_limit`, not truncated by the app | re-snapshot with a larger `text_limit`; do NOT read the screenshot to recover it, and do not tell the user the content is missing |
| `[tree truncated at N nodes]` | the window has more controls than the budget | raise `max_tree_nodes`, or scroll to bring your target into range — the rest of the window is real, you just have not been shown it |
| `Screenshot suppressed: the accessibility tree was truncated …` | a cut-off walk cannot prove the window holds no password field, so no pixels were captured. Routine for a browser or an Electron app at the default budget | if you actually need the image, re-snapshot with a higher `max_tree_nodes` / `max_tree_depth`; otherwise work from the tree and do not re-request the screenshot |
| `no state for 'X'. Call computer_get_state first.` | you acted without a snapshot | snapshot, then act |
| `state for 'X' is 214s old. Call computer_get_state again.` | the snapshot expired (90s) | snapshot again |
| `element_index 7 changed since the last computer_get_state (was 'AXButton "Save"', now 'AXButton "Delete"')` | the UI moved under you — this refusal is what stopped you clicking the wrong thing | snapshot again and re-locate the element by its label, not its old number |
| `'…' is a blocked target for computer use (…)` | KiroCrew's own dashboard is permanently refused — driving it would let you change your own security settings | do the task another way; tell the user why |
| `refusing to type this text into 'X': …` | the text looked like a sensitive command or credential | do not rephrase to get around it; explain and stop |
| `refusing to … a secure text field` | the target is a password field | ask the user to type it themselves |
| `computer use is disabled …` | the primary switch is off | tell the user to enable it in Settings → Computer Use; do not retry |
| `computer use is not supported on this platform (…)` | not macOS | say so once |
| `moving the real mouse pointer is switched off for this caller` | you asked for `click_method: "global"` on a leg that refuses it | use `app_post` (or an element index) — neither moves the pointer |
| `give either element_index or both x and y, not both forms` | you supplied two different targets in one call | pick one — the element index if the outline has the control |

## When the tree is lying to you

Only about a third of macOS apps implement accessibility well, so a tree that looks
authoritative can be wrong. Recognising this is what stops you clicking the same
wrong index four times:

- **Repeated or empty labels.** Three rows all reading `row` with no title, or a
  blank `textfield` where you expected "Search" — the tree cannot disambiguate them.
  Use position, or use the containing element.
- **A near-empty tree from an app that clearly has content.** Electron apps (Slack,
  VS Code, Obsidian, Freeform) need an accessibility opt-in that takes ~2s; the first
  snapshot can be a 3-node stub. Snapshot once more before concluding anything.
- **The tree disagrees with the screenshot.** Trust the screenshot about what EXISTS
  and the tree about what is ADDRESSABLE. If a control is visible in the image but
  absent from the outline, it is a coordinate target, not a missing feature.
- **A stale index.** Indices belong to the snapshot that produced them and expire
  after 90s. A drift refusal naming a changed element is the system catching a
  mis-click for you — re-snapshot and locate by LABEL, not by the old number.

## Things that will bite you if you do not know them

- **Electron apps are slow on the first snapshot.** Slack, VS Code, Obsidian and
  KiroCrew's own desktop app need an accessibility opt-in that takes ~2 seconds to
  take effect. The first `computer_get_state` on one of them looks like a hang and
  is not. Wait for it; do not fire a second call.
- **A password field can look ordinary.** It renders as `<secure>` and its value
  is never shown to you. A window containing one gets **no screenshot at all** —
  that is deliberate, not a bug.
- **Screenshots and trees contain real user data** — file paths, window titles,
  volume names, open document names. Do not echo more of a tree back to the user
  than the answer needs, and never paste one into an external system.
- **Every action is real and mostly irreversible.** A click can send an email or
  change a setting. Read the label before you press it, and when the outcome is
  consequential say what you are about to click before you do it.
- **`element_index` is not a list position.** It is the number printed in the
  outline. Use it exactly as shown.
- **Empty containers are elided** from the outline, so numbers are not always
  contiguous. That is expected.
- **The user may be watching, and you cannot see what they see.** Two optional
  views exist for them, not for you: a live panel that mirrors the screenshots you
  take, and a "Cursor Motion" overlay that draws a moving cursor on their real
  desktop along the path your next click will take. Neither is a tool and neither
  changes what you get back. The overlay is deliberately **invisible to
  screenshots**, including yours — so if a user says "I can see the cursor moving"
  and your screenshot shows no cursor, both are correct and nothing is wrong. Do
  not go looking for a cursor in a screenshot, and do not describe the overlay as
  proof an action landed; the refreshed tree is the evidence.

## Worked example

> "Rename the top file in my Documents Finder window to notes-2026.md"

```
computer_get_state(app="Finder", screenshot=True)    # screenshot=True opens the live view
  → 0 window "Documents"
      12 row "draft.md"
      ...
computer_click(app="Finder", element_index=12)       # select the row
computer_press_key(app="Finder", element_index=12, key="return")   # rename shortcut
computer_get_state(app="Finder", screenshot=True)     # rename mode = a visible change
  → 13 textfield "draft.md"
computer_set_value(app="Finder", element_index=13, value="notes-2026.md")
computer_press_key(app="Finder", element_index=13, key="return")   # commit
computer_get_state(app="Finder", screenshot=True)     # show the user the renamed file
computer_end_turn()
```

Two things to copy from this:

- **The re-snapshot after the keypress is mandatory**, not stylistic: entering rename
  mode changed the tree, so the old index for the row is no longer the index of the
  field. Never carry an index across a UI change.
- **Three screenshots, not seven.** One to open the live view, one when the row turned
  into a field, one to show the result. The `click`, `set_value` and the first
  `press_key` produced no separate frame — they are steps toward one visible change,
  and their refreshed trees already told me what I needed.
