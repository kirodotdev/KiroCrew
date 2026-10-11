---
name: dashboard
description: "Load this when somebody asks to change their crewmate's Dashboard tab: another page, one that shows something particular, keeping one, or going back. One flow -- search the catalog, stage a preview, ASK, then apply. Never swap a page nobody looked at; the page always comes from the catalog."
triggers: dashboard, my dashboard, dashboard tab, change my dashboard, another dashboard, dashboard page, show me a different, go back to the old dashboard, dashboard template
---

# The Dashboard Tab

A crewmate's Dashboard tab is ONE page, and it belongs to the person reading it. You
change it for them; you do not decide it for them.

The tab draws this page only for a reader who turned on the "Dynamic Dashboard"
Feature Preview. With it off, the default, the tab draws your `panel_publish`
record instead, so after an apply tell the person where that switch is.

Everything here is three phrases a person actually says.

## 1. "Show me another one" / "I want one that shows X"

`dashboard_templates` lists the catalog. Pass `query` and it searches the title, the
description AND the fold paths each template reads -- so `cost` finds the page that
shows a usage number even when its author never wrote the word.

Then `dashboard_preview` the one that fits. It takes a `template_id` and nothing else.
It records NOTHING: the page they are reading is untouched, no version is written. Give
them the link it hands back and ask.

If nothing in the catalog fits, say so. **You cannot write the page yourself.** A
dashboard page runs its own script against this crewmate's task titles and summaries,
inside a frame that can navigate itself, so a page nobody here has looked at could
carry them out. Only pages that shipped with the product render. A page somebody writes
themselves is a later change and needs a wrapper document this gateway mints -- until
then, "none of these fit" is the honest answer, and it is worth telling the person which
template came closest.

## 2. "Keep this one"

`dashboard_apply`. No arguments, because what it installs is the page that was staged
-- so what lands is provably what they looked at.

## 3. "Go back"

`dashboard_rollback(to_version)`. `dashboard_fields` lists the versions a rollback can
still reach -- read that list rather than guessing, because the store keeps fewer
payloads than the history has rows. A rollback moves FORWARD: restoring version 1 over
version 2 writes version 3, so going back is itself undoable.

## The values ON the page are yours to write

A template declares which of its fields you fill. Those are the ones carrying
`{"agentic": true}` -- things only you know, like a phase name or a one-line verdict --
and you write them with `dashboard_write`.

Every other field reads a fold at a path, and the template already says which:

```json
"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}
```

That is the rule behind the whole design. A number you type is true once, at the moment
you typed it, and wrong every time the page is opened afterwards. A number read from a
fold is true tomorrow.

`dashboard_fields` lists the fields of the template this crewmate is on, which of them
are yours, and which of your past writes were refused. Read it before you write.

Which folds exist, and what sits at which path, is in the `dashboard-template` skill's
`folds.json` and `FOLDS.md` -- generated from the product's own projections, so it is
the list that is true today.

## What not to do

- Do not apply a page nobody has looked at. The preview step exists so that the person
  sees it first, and skipping it makes them undo your work instead of approving it.
- Do not report what the page shows. They are looking at it. Say what you changed.
- Do not keep previewing. Stage one, ask, and wait for an answer.
