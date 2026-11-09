---
title: App release notes in the "What's new" window (contributes.releaseNotes)
status: draft
author: lsgonc
created: 2026-11-09
last-audited: 2026-11-17
audited-at: 6dd5dacf8f
doc-pr:
implementation-prs: []
tracking-issues: [31784]
supersedes: []
superseded-by: []
---

# RFC: App release notes in the "What's new" window (contributes.releaseNotes)

- Status: draft. Nothing is built. This document changes no code.
- Tracking issue: [#31784](https://github.com/kirodotdev/KiroCrew/issues/31784).
  The maintainer decision there asks for this RFC first. It names four
  questions, and each one has its own section below:
  - **Q1.** How the window shows that the notes come from the app (§4).
  - **Q2.** Who may write the notes file and the seen state (§5).
  - **Q3.** The size caps (§6).
  - **Q4.** How app notes queue behind the host's (§7).
- Related:
  [rfc-app-contribution-points.md](rfc-app-contribution-points.md). This
  point has no frame, so it is proposed on its own rather than as a change to
  that document's §9. It follows that document's §2.11 manifest rules, with
  two exceptions, and §8 gives the reasons for both.
- Code references re-checked at `6dd5dacf8f`, and §5.2's two gate lists at
  `bddefa36fb`. File sizes were measured at `f41d476a21`.

## Summary

An app declares a markdown changelog in its manifest:

```json
"contributes": {
  "releaseNotes": { "file": "CHANGELOG.md" }
}
```

Kiro Crew reads the file and keys each `## ` section by its heading text. On a
later page load it shows the sections the user has not seen in one dialog
drawn by the host, "What's new in your apps", with one part per app. That
dialog is never the host's own "What's new" dialog.

The design comes down to five decisions:

1. **Attribution.** App text never shares a dialog with Kiro Crew's own notes.
   The host draws every part of the app dialog except each app's notes body:
   the per-app header, the provenance line and the buttons. That body is
   rendered with a restricted markdown profile: no raw HTML, no media, no links
   into the dashboard, and no buttons.
2. **Disclosure.** The point grants nothing, so there is no grant to approve.
   The install and update consent card and the app's Permissions card still
   say that it shows its release notes after it updates, and the first time an
   app's notes appear, its part of the dialog says so too. The switch to turn
   them off sits on the Permissions card.
3. **Seen state.** The seen state belongs to the host. It lives in a file in the
   data home, outside the app's tree, because every one of the app's own surfaces
   can write the app's tree. The routes that change the seen state refuse app
   tokens.
4. **Caps.** The host reads a bounded file, a bounded number of sections and a
   bounded section size, and shows a bounded number of sections and apps per
   dialog. Each cap is set from a measured changelog and checked on both sides.
5. **Queueing.** The app dialog waits until every other dialog that opens at
   launch has settled, Kiro Crew's own "What's new" first. It opens at most
   once per page load, with at most three apps. A section is marked seen when
   the dialog closes, not when it is fetched.

## 1. Motivation

### 1.1 Current state

The host's own "What's new" dialog is driven by `useUpdateFlow` in
`website/src/shell/updates/updateFlow.tsx`:

- It compares `localStorage['mc-last-version']` with the running version
  (`useUpdateFlow`).
- It keeps the `## [X.Y.Z]` sections in `lastSeen < v <= version` and opens
  `ChangelogDialog`.
- The notes come from `GET /api/changelog`, which returns the whole of
  `CHANGELOG.md` with no size limit
  (`_read_changelog` and `api_changelog` in `dashboard/handlers/updates_channel.py`).
- The parser in `src/kiro_crew/changelog.py` recognises only versioned headings
  (`_SECTION_RE`).

An app has no way to add to that dialog. It can tell users what changed only
through surfaces it does not control the timing of:

- **A notification.** It lands in the bell. The user reads it only if they open
  the bell.
- **An app page.** The user sees it only if they navigate to it.
- **A line in an agent's reply.** It is mixed into conversation text.

Tenet 8 says an app gets the same powers a built-in page has. The host can tell
its users what changed after an update. An app that ships far more often than
Kiro Crew cannot.

### 1.2 Who needs it

- Apps that ship on their own cadence: registry apps, and local apps installed
  with `kirocrew app install <path>`.
- The case that motivates this RFC is a local app that wraps an agent installed
  separately and updated several times a week. The app is the only part that
  knows where the agent's changelog lives. Kiro Crew cannot find it, and the
  agent's own spec cannot carry a new field.

## 2. Goals and non-goals

**Goals**
- Any installed, enabled app can show its own release notes after it changes,
  using the same window pattern the host uses.
- A user never mistakes app text for Kiro Crew's text, for another app's text,
  or for a Kiro Crew action.
- A user is told an app does this before, or at the latest when, it first
  happens.
- No app code runs, nothing is fetched from the network, and no new authority is
  granted.
- The notes are seen once per crew, not once per browser origin.

**Non-goals**
- Showing app notes to anyone but the crew's owner. All three routes are
  owner-only (§5.2), so a member of a shared crew never sees an app's notes.
  This is intended: the seen state and the mute are the owner's, and a member
  marking notes seen would retire them for the owner.
- Changing how the host's own notes are detected or shown.
- Release notes for agents, skills or MCP servers that no app declares.
- Showing notes when an app updates while the page is open. Version 1 checks on
  page load only (§11).
- A global switch for all app release notes. The per-app switch covers the
  need in version 1 (§11).
- Rich content: images, embeds, interactive elements.

## 3. Manifest and parsing

### 3.1 The field

`contributes.releaseNotes` is a single object, not a list. An app has one
changelog.

| Field | Type | Rule |
|---|---|---|
| `file` | string | Required. A path relative to the app root. Refused by `_app_path_refusal` (`apps/manifest_containment.py`) if it could leave the app root. Must end in `.md`. |

A field present with the wrong JSON type is refused, never coerced, as §2.11
requires. Any other key in the object is refused, so a later version can add
fields without older apps already using them by accident.

Where the file lives decides who refreshes it:

- **Outside `data/`** (for example `CHANGELOG.md` at the app root): the file
  ships in the package and is replaced on every update. This is the default
  for an app whose notes change only when it releases.
- **Inside `data/`**: the file survives updates. `update_app` in
  `apps/manager_install.py` moves the installed `data/` aside and restores it
  over the new tree, and a `data/` shipped in the package is used only on the
  first install. So a file here is refreshed only by the app at runtime (its
  backend, a cron or `setup.onInstall`). This suits an app whose notes come
  from something updated separately, such as an agent it wraps.

`docs/app-kit/manifest-reference.md` states both cases, so an author does not
ship notes in `data/` and find they never change. It also states the key rule
in §3.2 for authors: a section is identified by its heading, so a renamed
heading is a new section to every reader.

### 3.2 Sections and keys

- A section starts at any line that matches the host's `_H2_RE` (`^##\s+\S`)
  and runs to the next one. The reader reuses `_H2_RE` as it is. It adds no new
  regular expression.
- Text before the first heading is ignored.
- `#` headings and fenced code blocks are skipped when looking for headings, so
  a `## ` line inside a code fence does not split a section. A fence is found
  with a plain prefix test on the line (```` ``` ```` or `~~~`), not a pattern.
- The key is the heading text after normalization:
  - Leading `## ` removed.
  - Unicode NFC applied.
  - Each run of whitespace collapsed to one space, then trimmed.
  - Cut to the heading cap (§6).
- **Repeated headings.** The last copy of a repeated heading keeps the plain
  key. Each earlier copy is keyed by its heading and its body: `key#` plus the
  first 8 hex characters of the sha256 of the body. Changelogs put the newest
  section first, so a new release that repeats a heading gets a new key and
  every existing key stays the same. Appendix A gives the reasoning and the
  edge cases.
- The key is never a version. Many app changelogs do not number their releases.
  One measured app changelog has 104 sections, and every heading is
  `## [Unreleased] — <title>`. The host's version-keyed parser would collapse
  all of them into one section.
- **Consequence:** editing a heading announces that section again. This is
  documented for app authors (§3.1): fix a heading before a release reaches
  users, not after, and expect a renamed one to show again. The per-app cap
  (§6) bounds what a mass rename can cause.

## 4. Q1 — How the window shows the notes are the app's

The risk is that text the app wrote reads as Kiro Crew speaking, or as another
app speaking: "Kiro Crew needs you to re-enter your token", or a link that
looks like a host action. Kiro Crew's own notes are reviewed in this
repository. App notes are a file the app can rewrite at any time. The decisions
below follow from that difference.

### 4.1 One app dialog, separate from the host's

- App notes never appear inside `ChangelogDialog`. They use a new
  `AppReleaseNotesDialog`.
- All apps with unseen notes on one page load share that one dialog, with one
  part per app. The user dismisses it once.
- **Rejected: adding app parts under the host's notes.** An app part would sit
  directly under Kiro Crew's own heading and the host's version line, and it
  would read as part of the release.
- **Rejected: one dialog per app.** Up to three dialogs opening in a row on one
  load is a lot of interruption, and each one asks for its own dismissal.
  Attribution does not need a dialog per app. It needs a host-drawn header per
  app, and §4.2 gives each part one.

### 4.2 The host draws everything except each notes body

The dialog:

| Region | Content | Source |
|---|---|---|
| Title | "What's new in your apps" | i18n string. It never names an app, so no app text reaches the title. |
| Glyph | A fixed host glyph for apps | Never `Package`, the glyph of the host's own dialog |
| Footer | **Close** | Host button |

Each app's part, in the order of §7.2:

| Region | Content | Source |
|---|---|---|
| Header | The app glyph, then `{displayName}` | `displayName` capped at 40 UTF-16 units, truncated with an ellipsis. The glyph is the `icon` of the app's first `ui.pages` entry, if it is in the host glyph set; otherwise a fixed app glyph. Never `iconUrl`, which is an image the app controls. |
| Provenance line | "Release notes from the app `{name}`. Kiro Crew did not write them." | i18n string; `{name}` is the **install name**, a slug that is unique and cannot be chosen to match another app |
| First-time line | "{displayName} can show its release notes here after it updates. You can turn this off below or on its page." Shown only the first time this app's notes appear (§5.2, `introduced`). | i18n string |
| Body | The sections (§4.3), inside a bordered box | App |
| Actions | **Open {displayName}** (only if the app has a page), **Don't show notes from this app** | Host buttons |

- Why the provenance line uses the install name rather than `displayName`:
  `displayName` is free text. An app can set it to "Kiro Crew", and the header
  would then read "Kiro Crew". The install name cannot impersonate the host, or
  another app, in the same way.
- Why one app's body cannot pass for the next app's part: each part's header,
  provenance line and actions are drawn outside the body's box, and a body
  cannot contain a `##` heading (§4.3). A body that writes a fake header ends up
  as smaller text inside its own app's box, under its own app's provenance line.
- The dialog never shows anything that belongs to the host's dialog: the version
  line, the update button (**Update when I restart**), the auto-update toggle,
  or "View full changelog". Those are what make the host's dialog read as Kiro
  Crew's.

### 4.3 A restricted rendering profile for the body

The body is rendered by `MarkdownRenderer` with a new `appNotes` profile. The
profile is set in one place, `MarkdownRenderer.tsx`, the composition root of
the markdown pipeline, so it cannot drift per call site.

| Element | In `appNotes` | Why |
|---|---|---|
| Raw HTML | Shown as text | The app writes it at runtime. Even sanitized, it can draw boxes that look like host chrome. |
| `img`, `picture`, `video`, `audio`, `source` | Dropped; an image's alt text is kept as text | No network fetch when the dialog opens: no tracking pixel, no image of a fake button. |
| Links | Only `https:`. The destination host is shown after the link text. The link opens a new tab with `noopener,noreferrer`. | The reader sees where a link goes before clicking. |
| Relative, same-origin, `/…`, `#…` and session links | Shown as text | A link into the dashboard, such as Settings or an approval page, would be a host action wearing app text. |
| Session, file and artifact chips; link unfurls; text previews | Off | Each one dresses app text as a host object. |
| `run-yourself` cards, editable code | Off. Code is read-only with a copy button. | `runYourselfCards` is already off for text not written by the agent. |
| Mermaid, math | Shown as code | Each renders a large figure from app input, and the notes do not need it. |
| `###` and deeper headings | Kept, drawn one level smaller | `##` cannot appear in a body, because it starts a new section. |

**Precedent.** App text already reaches host chrome through notifications
(`POST /api/notifications/push`), with three protections:

- The source is set by the server as `app:<name>` and the request body cannot
  override it (`dashboard/handlers/notifications_push.py`).
- The channel must be declared in the manifest.
- The body is capped at 20,000 characters (`_MAX_BODY_LEN` in `notifications/bus.py`).

This point uses the same three rules: an identity the host draws, a declaration
in the manifest, and a cap. It adds the restricted profile, because a dialog
that opens by itself gets more attention than the bell does.

### 4.4 What the app never gets

- No signal of whether, when or how often the user saw the notes, or whether the
  user muted them.
- No code execution, frame, network fetch or navigation.
- No buttons, and no control over when the dialog opens.
- No session identity, and no access to host state.

## 5. Q2 — Who may write the notes file and the seen state

### 5.1 The notes file: the app, and only as app speech

- **Every app surface can write the file, and the host cannot narrow that:**
  - The app package: on install and update for a file outside `data/`, on
    first install only for a file inside it (§3.1).
  - `setup.onInstall` (`apps/lifecycle_scripts.py`).
  - The app's backend process and cron scripts. Both run as the gateway's user
    (`apps/backend.py`, through `wrap_argv`).
  - The app token's own routes. These write inside `data/`:
    `PUT /api/apps/{name}/config` writes `data/config.json`
    (`handle_app_config`), and `AppStorage` writes `data/kv/`
    (`apps/app_storage.py`).
  - The user, by hand.
- **The decision:** whoever wrote the file, the host shows its contents as the
  app's text, under §4's attribution and rendering rules. That gives the app no
  authority it did not already have: it can push markdown to the bell today
  (§4.3).

**Rules for reading the file**, checked every time it is read:
- The file is read through the host's existing no-link reader,
  `safe_read_file_bytes_nolink` (`hook_runtime/safe_reads.py`), with
  `within_root=realpath(app_dir(name))`, the byte cap (§6) as `max_bytes`, and
  `allow_truncate=True`. The reader adds no path check of its own.
- What that reader does, in order:
  - It resolves the path with `os.path.realpath`, so links are followed.
  - It opens the resolved path, refusing a link in the final component
    (`platform_compat.open_file_no_reparse`, which also covers Windows
    junctions). That catches a link swapped in after the path was resolved.
  - The opened descriptor must be a regular file with one link.
  - The descriptor's real path (`/proc/self/fd`, `F_GETPATH` or
    `GetFinalPathNameByHandleW`) must be inside the app root. If the real path
    cannot be found, the read fails closed.
- So a link is allowed only when it ends inside the app root. An in-tree link
  such as `CHANGELOG.md -> docs/notes.md` is read, the same as install's
  `_copy_app_tree` keeps it. A link that leaves the root is refused wherever it
  sits in the path.
- Checking the descriptor rather than the name is what closes the case of a
  symlink in the middle of the path. `data/` is written at runtime, so
  `data/notes -> /somewhere` can appear after install, and install's own
  symlink pruning never sees it. A check on the final component alone would
  follow `data/notes` and show any file the user can read. The descriptor check
  refuses it, and the same check refuses a directory swapped for a symlink
  between the resolve and the open.
- The bytes read are decoded as UTF-8, with invalid bytes replaced.
- A missing, refused or unreadable file means "no notes"; an oversized one is
  cut to the byte cap (§6). It is never an error the user sees. A refusal is
  logged at debug level.
- The parse is cached on `(path, st_mtime_ns, st_size)`, the same way
  `_read_changelog` caches the host's file. A cache hit returns a parse of
  bytes that already passed the checks above, so it cannot show a file the
  checks would refuse.

**Only enabled apps are read.** A disabled app's notes are not read, and its
seen state is kept for when it is enabled again.

**Disclosure, not consent.** The point grants no API, no events, no hosts and
no data. It does not call `register_point_grants`, so an update that adds it
does not wait for approval (see `manager_records.py` for the staged grant
families). It does give the app a new kind of attention, a dialog that opens by
itself at launch, so the user is told about it in three places:
- **The install and update consent card**, `TrustAppModal`
  (`components/appstore/TrustAppModal.tsx`), gains one line: "Shows its release
  notes in a window after it updates." It is a new `releaseNotes` flag on
  `TrustAppTarget`, drawn the way the card already lists `chatSlots` and
  `hiddenSessions`. No grant is recorded for it. The card opens only for an
  app that needs trust, so the next two places cover the rest.
- **The app's Permissions card** on its detail page shows a box with the same
  sentence and the **Show release notes** switch inside it (§5.2). It follows
  the declaration-only boxes the card already draws, such as `ChatSlotsBox` and
  `HiddenSessionsBox` (`pages/AppDetailGrantBoxes.tsx`). Today the card renders
  only when the manifest has a `permissions` block, and an app can declare this
  point with none, so the card's condition widens to include
  `releaseNotes.declared`. A registry listing shows the box before install.
- **The first time the app's notes appear**, its part of the dialog carries the
  first-time line (§4.2). This covers the paths that show neither card: a
  local install from the command line, and an update that adds the point.

The dashboard learns that an app declares the point from a host field,
`releaseNotes: {declared, muted}`, never from the manifest dict (§8). Installed
rows get it from `_installed_app_detail` (`apps/routes_listing.py`). Registry
rows get `releaseNotes: {declared}` from the listing build in
`apps/registry_pipeline/manifests.py`, whose manifest allowlist does not carry
`contributes`.

### 5.2 The seen state: the host, and only the host

**Rejected: storing it in the app's data directory.** The issue proposed this
first. But `data/` is created inside the app's own tree
(`app_data_dir` in `apps/manager_records.py`), and every surface in §5.1 can write there,
including the app's own token routes. An app could:
- clear the state to show its notes again on every load, or
- fill it in to hide notes the user should have seen.

The manifest's `approved-grants.json` sits in that tree too
(`APPROVED_GRANTS_FILENAME`). There is no host-owned area per app to reuse.

**Decision: one host file in the data home.** It is
`config_dir() / "app-release-notes.json"`, alongside the host's other state
files such as `registry_trust.json` and `denied_commands.json`. Like them, it
is added in two places that must agree:
- `_CREW_READONLY_LEAVES` (`sandbox_leaves.py`), so an app's sandboxed cron
  or `setup.onInstall` script cannot rewrite it.
- `_CREW_SECRET_LEAVES` (`security/paths_lists.py`), which
  `security.sensitive_home_dirs()` expands under each crew-home prefix, so an
  agent's file tools cannot read or write it either.

`test_sandbox_governance_mask.py` checks that every crew-home entry on the
second list has a sandbox disposition; for this file, the first list's
read-only seal. The file is written only by the handlers below, under a file
lock:

```json
{
  "version": 1,
  "apps": {
    "<install name>": {
      "baselined": true,
      "introduced": false,
      "muted": false,
      "seen": ["<16 hex chars of sha256(key)>", "..."]
    }
  }
}
```

- **Hashes, not keys.** The host's state holds no app text, and the entry size
  is fixed.
- **`introduced`** turns true the first time a `seen` post with keys, or a mute,
  lands for the app. Until then the dialog shows the app's first-time line.
- **Pruning.** On every write, `seen` is cut to the hashes of the parsed
  sections (§6) in the current file. A section the app deletes drops out, so
  `seen` is bounded by the section cap (§6). The cost: if a deleted heading
  comes back later, it is shown again. That is acceptable.
- **Uninstall** removes the app's entry. A reinstall starts with a silent first
  sight (§7.3), and its first notes carry the first-time line again.

**Routes.** They sit outside `/api/apps/{name}/`, deliberately:

| Route | Does |
|---|---|
| `GET /api/release-notes/apps` | Reads only; it never writes the state file. For each enabled, unmuted app that declares the point and is `baselined`: `{name, displayName, glyph, hasPage, introduced, sections: [{key, title, body}], omittedKeys: [key]}`, for unseen sections only. `omittedKeys` lists the unseen keys past the per-app cap (§6), so the client can acknowledge them without showing them. Apps with no `baselined` entry are listed by name in `baselinePending` and show nothing (§7.3). |
| `POST /api/release-notes/apps/{name}/seen` | Body `{keys: [...]}`: marks those keys seen, sets `introduced`, and prunes. Body `{baseline: true}`: if the app has no `baselined` entry, records every current key and sets `baselined`; otherwise does nothing (§7.3). |
| `POST /api/release-notes/apps/{name}/mute` | Body `{muted: bool}`. Muting also sets `introduced`. |

**The switch.** **Show release notes** sits in the release-notes box of the
app's Permissions card (§5.1). It reads `muted` and calls the `mute` route, so
a mute set from the dialog can always be undone in the same place the user was
told about the dialog. The card reads both from the host field in §5.1.

**Why outside `/api/apps/{name}/`:** `_app_owns_path`
(`dashboard/token_auth_app_scope.py`) gives an app's token every path
under `/api/apps/<self>/` by default. That would let an app mark its own notes
seen, or unmute itself.

**Only the dashboard owner may call them.** All three handlers, `GET`
included, start with the host's owner gate, `require_owner_dashboard_request`
(`dashboard/handlers/_shared_access.py`, the gate behind the core owner-only routes
and `AppContext.require_owner`). It answers a 403 `owner_only`, or a 401 for a
stale session. On top of that, each handler refuses a request carrying an app
identity (`request.get("app")`), the same way the lifecycle handlers do
(`apps/routes_enable.py`). The refusal holds even if an approved
`permissions.api` pattern would otherwise match. Each denial is logged the way
those handlers log theirs.

**The limit of this, stated plainly.** An app code path that runs outside the
sandbox runs as the user, and it can write any file the user can, including
this one. This is
the boundary `rfc-app-contribution-points.md` §2.1 draws: host-owned state
protects against the app's token and its declared surfaces, not against code
the user chose to trust with their account. Nothing here weakens that boundary,
and nothing pretends to go past it.

## 6. Q3 — Size caps

Every text cap is measured in UTF-16 units with `_mirrored_len`
(`apps/manifest_contributes.py`). Every cap is enforced on the server and
again on the dashboard side, as §2.11 requires.

| Cap | Value | Over the cap | Basis |
|---|---|---|---|
| File bytes read | 256 KiB | Only the first 256 KiB is read; a section cut off at the end is dropped | Kiro Crew's own `CHANGELOG.md` is 217 KB at `f41d476a21`. A busy app's file measured 116 KB. Changelogs put the newest section first, so the cut drops the oldest. |
| Sections parsed | 200 | Later sections are ignored | 104 in the busiest changelog measured. This also bounds `seen`. |
| Heading / key | 200 units | Cut for both the title and the key | Headings are one-line titles. |
| Section body | 20,000 units | Cut at a line boundary; the host adds a note: "Shortened. Open {displayName} for the full notes." | The same as a notification body (`_MAX_BODY_LEN` in `notifications/bus.py`). The largest section measured was about 10,500. |
| Sections per app | 10 newest unseen | The host adds a line to the app's part: "{n} earlier updates are not shown." All unseen sections are marked seen when the dialog closes. | Stops a long backlog, or a mass heading edit, from turning into a wall of text or a dialog that keeps coming back. |
| `displayName` in a header | 40 units | Cut with an ellipsis | The same as §2.11's `title` cap. |
| Apps per dialog | 3 | The rest wait for the next load | One dialog stays short enough to read (§7.2). |

**Response size.** The worst case per response is 3 apps × 10 sections × 20,000
units, about 600k units. That is of the same order as the host's own changelog
response. The 3-app cap is applied on the server, so a crew with many apps does
not send them all.

## 7. Q4 — Queueing behind the host's notes

### 7.1 Where it mounts, and what it waits for

The dialog mounts inside the launch-surface block in `App.tsx`, which already
says "Add a new auto-opening surface HERE" (the comment that opens the block). That keeps it
out of the look-preview frame, as that block guarantees.

It is **held** while any of these is true:
- The host's own changelog flow has not settled: its effect has not yet decided
  whether to open, or its dialog is open. This needs one new boolean from
  `useUpdateFlow`, `changelogSettled`, which is set on every branch of the
  existing effect, including fetch failure.
- The update overlay, `UpdateModal` or `UpdateFoundModal` is open.
- The keyboard shortcuts dialog is open.
- First-run chapters are not finished, or the mobile connect dialog is open.

Today only `UpdateModal` and `UpdateFoundModal` take a `held` prop, and it is
`showChangelog || shortcutsOpen` (`App.tsx`). The new dialog takes the same
kind of prop with the wider condition above, because it is the one launch
dialog that must wait for all the others. It holds nothing itself.
`RemoteUpdateOffer` is not in the hold: it is a corner card (`role="status"`)
that traps no focus and stays until dismissed, so waiting on it could hold the
app dialog for the whole page load.

### 7.2 Order within the dialog

- The host fetches `GET /api/release-notes/apps` once, after the hold is first
  released.
- If any app has sections, it opens the dialog once. It does not open again
  during that page load.
- The apps' parts are in order of install name. The order is predictable, and
  it is not affected by which app wrote last.
- At most three apps per dialog (§6). The rest wait for the next load.
- If the hold comes back after the fetch and before the dialog opens (an update
  dialog appears, say), the dialog waits with the fetched state intact.

### 7.3 When a section counts as seen

- **First sight.** For an app with no `baselined` entry, `GET` shows nothing
  and lists the app in `baselinePending`. Right after that fetch, before the
  dialog, the client posts `{baseline: true}` for each pending app, and the
  server records every current key. This matches the host's silent first run
  (`useUpdateFlow`'s first-visit branch). It applies to a new install, to an
  app that adds the point in an update, and to a reinstall.
  - The `GET` stays a pure read, so a prefetch or a health probe can never
    baseline an app by accident.
  - The baseline is written under the state file's lock and only when no
    `baselined` entry exists. Two tabs posting it at once record one baseline,
    and a late post can never re-baseline an app and swallow sections added
    since.
  - If the post never lands, the app stays pending and shows nothing until a
    later load baselines it. A pending app does not count toward the
    three-app cap (§6).
- **Seen on close, not on fetch.** Closing the dialog (with X, Escape, the
  backdrop, Close, or any app's Open) posts, for each app shown, the keys that
  were shown plus that app's `omittedKeys` from the same response (§6). One
  post goes to each app's `seen` route.
  - The host's own flow stamps the version when it reads the notes. For app
    notes that would retire sections the user never saw, if the tab closed
    while the dialog was held behind another one.
  - If a post fails, nothing is retried during this load. That app's sections
    show again on the next load. Showing them twice is better than losing them.
- **Muting from the dialog** removes that app's part at once, posts the mute,
  and marks its sections seen. Muting the last app in the dialog closes it.
- **Two tabs.** Two tabs open at once can both show the dialog. The first to
  close marks the sections seen, and the second does no harm: marking is
  idempotent.

## 8. Differences from rfc-app-contribution-points §2.11

| §2.11 rule | Here | Why |
|---|---|---|
| Each point is a list with `id`, `title`, `icon`, `entry` | A single object with `file` | An app has one changelog. There is no entry module, because no app code runs. |
| Every point entry goes into the signed manifest | `releaseNotes` is **not** added to `signing_payload` | Below. |

**Why `releaseNotes` stays out of the signature:**
- The payload is rebuilt from the parsed model (`AppManifest.signing_payload`).
- An older host drops a `contributes` key it does not know (`Contributes.from_dict`
  reads named keys only).
- So adding the point to the payload would make an app signed by a newer host
  fail verification on an older one.
- Signing it protects nothing. The field only says which file inside the app's
  own tree to read, and that file sits in unsigned, runtime-written `data/`
  anyway.

**How it is kept out.** `signing_payload` signs the whole
`Contributes.to_dict()` once any guarded point, such as `commands`, is present.
So `Contributes.to_dict()` never emits `releaseNotes`. Nothing needs it there:
the dashboard gets the notes from the routes in §5.2 and the switch's state from
the `releaseNotes` field on the app's row, never from the manifest. The same
manifest gives byte-identical signed output with and without `releaseNotes` (a
Phase 1 exit criterion).

`minHostVersion` is not needed. An older host ignores the key without an error,
so an app can declare it today.

## 9. Migration plan

Each phase is one PR, and each stays within this repository's commit rules.
Phase 2 is the named follow-up of Phase 1: its PR is opened before Phase 1
merges, Phase 1's description links it, and both are meant to land in the same
release. If Phase 2 cannot land in that release, Phase 1 is reverted before the
release is cut, so no release ships routes that nothing calls.

**Phase 1: backend.**
- The `ReleaseNotesConfig` dataclass and its validation in
  `manifest_contributes.py`.
- The reader, through `safe_read_file_bytes_nolink`, with the cap rules.
- The host state file, its `_CREW_READONLY_LEAVES` entry and its
  `_CREW_SECRET_LEAVES` entry (§5.2), the three routes, and the
  `releaseNotes` field on installed and registry rows.
- `docs/app-kit/manifest-reference.md` and the app-kit-platform spec.

Exit criteria, each covered by a test:
- A `file` outside the app root, a directory or a wrong JSON type is refused,
  or read as "no notes".
- A `file` that is a symlink leaving the app root reads as "no notes". A
  `file` that is a symlink to another file inside the app root is read.
- A `file` under a symlinked parent directory inside `data/`
  (`data/notes -> <outside the app root>`, with `file` set to
  `data/notes/CHANGELOG.md`) is read as "no notes". So is a hardlinked file.
- A `file` that differs only in case, or that goes through `..` and stays
  inside the app root, gets the same refusal or the same file that
  `_app_path_refusal` decides for any other manifest path.
- A `file` without the `.md` suffix, and a `releaseNotes` object with an
  unknown key, are refused.
- A first sight `GET` returns no sections, lists the app in
  `baselinePending` and leaves the state file unchanged. The `{baseline: true}`
  post then records every key.
- A `{baseline: true}` post for an app that is already `baselined` changes
  nothing, so a section added since still shows.
- Two concurrent `{baseline: true}` posts for the same app leave the state
  file with one baseline.
- A changed heading returns exactly that section.
- A `## ` line inside a code fence does not start a section.
- Repeated headings: the last copy gets `key` and each earlier copy
  `key#<8 hex of the body's sha256>`, and adding a new section at the top that
  repeats a heading returns exactly that section and leaves every existing key
  unchanged.
- A post to `seen` drops the hashes of sections no longer in the file.
- `introduced` is false until the first `seen` post with keys, or the first
  mute, and true after it.
- A disabled app's file is not read.
- The same manifest gives byte-identical `signing_payload` output with and
  without `releaseNotes`.
- More than 10 unseen sections returns the 10 newest plus every other unseen
  key in `omittedKeys`, and posting both empties the unseen set.
- A non-owner dashboard session gets 403 `owner_only` on all three routes.
- An app token gets 403 on all three routes, including with a
  `permissions.api` grant of `/api/release-notes/*`.
- A sandboxed app script cannot write `app-release-notes.json`.
- A registry row for an app that declares the point carries
  `releaseNotes: {declared: true}`.
- After an update, a `file` outside `data/` returns the new package's
  sections, while a `file` inside `data/` returns the preserved runtime copy.
- Uninstall removes the app's entry. Disable keeps it.
- A file over 256 KiB returns only the sections that fit whole.

**Phase 2: dashboard.**
- The `appNotes` rendering profile.
- `AppReleaseNotesDialog`, with one part per app.
- The release-notes box on the Permissions card, with the **Show release
  notes** switch, in `pages/AppDetailGrantBoxes.tsx` (the detail page itself is
  at the file line cap), and the card's render condition widened to an app
  that declares the point.
- The `releaseNotes` line in `TrustAppModal`.
- `changelogSettled` and the hold.
- i18n strings: `en.json` and `en.context.json`, plus the other locales as the
  locale gate requires.

Exit criteria, each covered by a test:
- The app dialog never opens while `ChangelogDialog`, `UpdateModal` or the
  shortcuts dialog is open.
- Three apps with unseen notes open one dialog with three parts, each with its
  own header, provenance line and mute. A fourth app waits for the next load.
- Raw HTML, an image, a `/settings` link and a session reference all render as
  text.
- An `https:` link shows its host name.
- An app's first notes carry the first-time line, and its later notes do not.
- The Permissions card shows the release-notes box for an installed app and
  for a registry listing that declares the point, including an app with no
  `permissions` block, and shows no box for an app that does not declare it.
- `TrustAppModal`, on install and on update, shows the release-notes line for
  an app that declares the point.
- Muting from the dialog removes that app's part, and turning **Show release
  notes** back on in the Permissions card shows the next new section again.
- Each app in `baselinePending` gets one `{baseline: true}` post after the
  fetch, and the dialog does not show it.
- Closing posts each shown app's keys and `omittedKeys`. A failed post shows
  that app's sections again on the next load.
- The look-preview frame never mounts the dialog.

**Phase 3: example.**
- An example app outside `src/kiro_crew/apps/builtins/` that declares the
  point, as §9.7 of the contribution-points RFC asks for each new point.

## 10. Alternatives considered

| Alternative | Why not |
|---|---|
| Seen state in the app's data directory (the issue's first proposal) | Every app surface can write it (§5.2). |
| Seen state in `localStorage`, like the host's | It is kept per browser origin. A hub's instance pane loads the dashboard at a forwarded port that changes, so the seen set resets and the next visit takes a silent baseline that swallows the notes. It is also separate in each browser. |
| Add app sections to the host's dialog | Attribution (§4.1). |
| One dialog per app | Up to three dialogs in a row on one load, each asking for its own dismissal (§4.1). A host-drawn header per app keeps attribution in one dialog. |
| Refusing a symlink only in the final component | It leaves a symlinked parent under the runtime-written `data/` open (§5.1). |
| A consent step that records a grant | Nothing is granted, so there is nothing to approve or revoke. The consent card, the Permissions card and the first-time line tell the user without one (§5.1). |
| A frame-backed point on the contribution-points model | Plain text does not need a frame, and the host can render markdown more safely than a frame can. A frame would also give the app control over the dialog's look, which is what Q1 is trying to prevent. |
| A notes URL fetched by the host | A network fetch every time the page loads; it reveals to a third party when the user opens Kiro Crew; it fails offline. |
| Recording the first-sight baseline inside `GET` | A read that writes. A prefetch or a health probe could baseline an app by accident and swallow its first notes (§7.3). |
| Keying sections by version | Many app changelogs do not version sections (§3.2). |
| Numbering repeated headings by position (`key#2`, `key#3`) | An unseen release can take a seen number (Appendix A). |
| Keying every section by heading and body | Editing any section's body would announce it again (Appendix A). |
| Notifications only | They do not reach a window the user reads. The app would also have to work out when to push. |

## 11. Answers proposed in review

The earlier draft left four questions open. Review input on `377aecfc34`
proposed these answers; they wait for the maintainer sign-off with the rest of
the design.

1. **Where the switch sits.** On the app's detail page, in its Permissions
   card, next to the line that tells the user about the dialog (§5.1, §5.2).
2. **A global switch.** None in version 1. The per-app switch covers the need.
3. **App updates while the page is open.** Deferred. Version 1 checks on page
   load, which is when the host checks its own notes too.
4. **`displayName` impersonation.** Out of scope here. Refusing a
   `displayName` that normalizes to the product name would affect every surface
   that shows `displayName`, so it goes to its own issue. This point does not
   depend on it, because its provenance line uses the install name (§4.2).

## Appendix A. Repeated headings

§3.2 states the rule: the last copy of a repeated heading keeps the plain key,
and each earlier copy is keyed `key#<8 hex of sha256(body)>`. The details:

- **What "last" counts.** It counts the heading of every complete section in
  the bytes read (§6), including sections past the section cap, so the section
  cap never changes a key. A section the byte cap cuts off is dropped, and its
  heading does not count. The body hash is taken after the body cap.
- **Identical copies.** Two earlier copies with the same heading and the same
  body get the same key. They count as one section, and only the first is
  kept.
- **Why not number copies by position.** Counted from the newest copy, adding
  one at the top shifts every number. Counted from the oldest, cutting or
  deleting an older copy shifts them. Either way an unseen release can take a
  seen number.
- **Why not key every section by heading and body.** It would close the case
  below, but editing any section's body would announce it again. With this
  rule, editing a body announces the section again only when its heading
  repeats and it is not the last copy.
- **When a key changes.** Of the caps, only the byte cap changes a key: when it
  cuts a heading's last copy. Deleting or renaming that copy does the same. The
  next-newest copy then takes the plain key, and it reads as seen if that key
  is in `seen`. Usually the user has seen that copy. If the key is not in
  `seen` (pruning, §5.2, keeps only parsed sections), that copy shows once
  more.
- **The case this misses.** One update adds a section that reuses a heading,
  and the same update cuts or deletes that heading's only older copy. The new
  section takes the plain key, and its notes do not show. A key built from the
  heading cannot tell this case from an edited body, so the RFC accepts it.
