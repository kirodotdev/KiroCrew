# Notes Module

## Overview

Notes is a builtin App Store app (`kiro_crew/apps/builtins/md_notebook/`) for keeping a
markdown notebook inside a git repository. It runs as a managed app backend SUBPROCESS: an
aiohttp server on the backend-assigned port, reached only through the gateway proxy. Every
proxied request carries an HMAC signature (`X-KiroCrew-Proxy: <ts>:<hmac>` over
`<ts>:<METHOD>:<path>[?q]:<sha256(body)>`, +/-60s window) verified fail-closed by
`proxy_auth.verify_proxy_request` in the backend's middleware. A failed verification is
SEL-audited before the 401 goes out (`operation=proxy_auth_failed`, `outcome=denied`,
path only — no query string, which can carry note names; emitted off-loop via
`asyncio.to_thread`, following the file-explorer builtin's convention). The bare `/health` path is
the single exemption, because the gateway's own liveness poll hits it unsigned. Gateway
session auth gates the proxy entrance as with all builtin apps.

The app id is `md-notebook`; the display name is "Notes". `defaultEnabled` is false, so it
appears in the Apps library ready to be switched on rather than enabling itself.

## Responsibilities

1. **Vaults** — clone a remote repo into `<home>/vaults/<id>/`, or attach an existing local
   working tree in place (no second copy on disk)
2. **Notes** — list, read, save, create, move and delete markdown files within a vault
3. **Links** — parse `[[wikilinks]]`, resolve them by title then filename, and build the
   reverse map so a note can show what links back to it
4. **Search** — full-text index over titles and bodies, with a title boost
5. **Sync** — commit, fetch, merge and push in one call, reporting conflicts without
   overwriting anything
6. **Knowledge** — persist a per-vault flag recording that the folder is registered as a
   Knowledge source (registration itself happens in the UI, which holds the user session)

## State Layout

Rooted at `MD_NOTEBOOK_HOME`, defaulting to `~/.kiro/crew/workspace/md-notebook/`:

| Path | Contents |
| --- | --- |
| `vaults.json` | Vault descriptors. No secrets, but `localPath` is what sync runs git against, so it is in `_SENSITIVE_HOME_DIRS`. Written via a temp file + `os.replace`. |
| `pat` | GitHub token, chmod 0600, never echoed back to the UI (only a boolean is). Also listed in `_SENSITIVE_HOME_DIRS`, so agent file tools cannot read it through the shared gate — 0600 alone does not isolate another process running as the same user. |
| `vaults/<id>/` | Vaults this app cloned itself. Attached vaults stay where the user has them. |

A vault descriptor carries `id`, `name`, `repo`, `localPath`, `branch`, `readOnly`, an
optional `subfolder` scope, plus `knowledge` and `knowledgeSourceId`. The `external` field
returned by `GET /api/vaults` is COMPUTED on read (`localPath` is outside `vaults/`) and
never persisted.

## Routes

The gateway proxy preserves the `/api/` prefix, so the backend sees exactly the paths the
UI calls. All vault-scoped routes accept `?vault=<id>` and fall back to the first vault.

### Read (GET)

| Route | Returns |
| --- | --- |
| `/health`, `/api/health` | `{ok, features[]}` — the capability probe |
| `/api/vaults` | `{vaults[], hasPat, hasGhAuth}` |
| `/api/notes` | `{notes[]}` with title, `modifiedAt`, `createdAt`, `syncStatus` |
| `/api/note?path=` | `{path, content, mtime, meta, backlinks[]}` |
| `/api/search?q=` | `{results[]}`; an empty query returns nothing, not everything |
| `/api/changes?since=` | `{rev, changed[], watching}` — external-edit poll |

### Write (POST/PUT/DELETE)

| Route | Effect |
| --- | --- |
| `POST /api/vaults` | Clone a remote vault |
| `POST /api/vaults/attach` | Adopt an existing checkout, with or without a git remote (no remote → a `localOnly` vault); 409 if already attached; 403 if the folder resolves into a protected location OR contains one (e.g. the home directory — sync's `git add -A` from such a root would stage `~/.ssh`/`~/.aws` wholesale; checked list-based via `security.path_contains_sensitive`, no tree walk) |
| `DELETE /api/vaults` | Forget the descriptor. FILES ARE NEVER DELETED. |
| `PUT /api/vaults/knowledge` | Persist the knowledge flag and source id |
| `PUT /api/pat` | Store or clear the token |
| `PUT /api/note` | Save, guarded by `baseMtime` |
| `DELETE /api/note?path=` | Move a note into the vault's local `.trash` (never unlinked) |
| `POST /api/note/new` | Create a uniquely named note |
| `POST /api/note/duplicate` | Copy a note beside itself as `<name> copy[.n].md` |
| `POST /api/note/move` | Move or rename; 409 rather than overwrite |
| `POST /api/sync` | Commit, fetch, merge, push |
| `POST /api/commit` | Commit to LOCAL history only — the periodic autosave. Never pushes, so it skips the remote-identity checks (`require_writable` and the trusted-gitdir check still apply) |
| `POST /api/pick-folder` | Native folder chooser; 501 when unsupported |

### Capability probe

Both health routes return a `features` list: `createdAt`, `attach`, `changes`,
`saveGuard`, `forget`, `pat`, `newNote`, `move`, `duplicate`, `trash`, `localOnly`,
`autoCommit`, `trashOpen`, `knowledge`, `pickFolder`.
The gateway keeps an app's backend alive across UI reloads, so a process running older code than
the page would otherwise surface as confusing "no route" errors; the UI compares this list
and names the missing capabilities instead. `trash` is listed even though it adds no route,
because the delete dialog's copy *promises* the note is recoverable — an older backend would
hard-unlink while the UI said otherwise, so the capability has to be detectable.

Detectable is not sufficient, because the stale-backend banner only *warns*: `trash` is also
**enforced**. The row's `onDelete` is passed only when health positively reports `trash`
(`canTrash`), and `NoteRow` omits the delete button when it is absent — not disabled, absent,
since a disabled control still advertises an action the backend cannot honour. `trash` and
`trashOpen` are in `REQUIRED_FEATURES` too, so the banner names them as the reason the button
is gone.

## Local Trash

`DELETE /api/note` **moves** the note into `<content root>/.trash/` and never unlinks it. A hard
delete is unrecoverable for a note the user never committed — the common case for something
written and deleted the same day — and git can only restore what it already has.

* The folder is dotted, so `_list_note_files_sync`'s dotted-directory prune keeps trashed notes
  out of the listing and out of search for free.
* The destination name is the note's basename, with ` 2`, ` 3`, … on collision, so two
  same-named notes from different folders both survive. The name is reserved with an
  `O_EXCL` create and then `os.replace`d onto — `replace` alone silently overwrites, and a
  bare `exists()` check would be a TOCTOU window.
* **No git ignore rule is written for the trash — per-path staging is the whole mechanism.**
  Nothing touches `.git/info/exclude` or the vault's `.gitignore`; `status()` filters `.trash`
  paths and staging names only what `status()` reported, so the folder cannot enter a commit.
  Two consequences to keep in mind: `git status` in the user's own terminal **does** list
  `.trash/` as untracked, and a hand-run `git add -A` there **would** stage it. Writing a
  per-clone exclude rule is deferred to a follow-up PR (its full hardening — `O_NOFOLLOW`, the
  hardlink refusal, the symlinked-parent refusal, the Windows fallback and the bytes-not-text
  read — is preserved on the `wip/notes-trash-exclude` branch); it is defence in depth for
  third-party git use, not part of this app's guarantee, and it was the single largest source
  of review findings on this PR.
  An earlier revision instead passed `:(exclude,literal).trash` to `git add`, which **broke
  sync**: naming the folder in a pathspec makes git treat it as an EXPLICITLY named ignored
  path and fail the whole add (`use -f if you really want to add them`, exit 1) in any vault
  that already ignores it — which is Obsidian's own convention, so the guard broke the common
  case while passing a fixture repo that has no ignore file. Do not reintroduce a pathspec
  exclusion.
* The cache is rebuilt rather than having one index entry dropped: the deleted note's own
  `[[wikilinks]]` go with it, so its targets would otherwise keep a backlink to a note that no
  longer exists.
* **Nothing empties it.** There is no retention policy, no purge on start, no age limit — a
  trashed note stays until the user removes it (Obsidian behaves the same). Time-based
  retention is deliberately NOT in this module: it is the only behaviour that would delete
  user data unattended, so it is being landed separately, where a reviewer can look at it in
  isolation. That is also why the delete dialog's copy promises only recoverability, never a
  deadline — copy that states a window a backend does not enforce is worse than no copy.
* **`POST /api/trash/open` reveals it in the OS file manager**, surfaced as an underlined link
  in the delete dialog (whose copy promises the note is restorable from there) and per vault in
  Settings. Without it the promise was unactionable: the folder is dotted, so it is hidden in
  this app's listing AND in Finder's default view.
  The route takes **no path** — the directory comes from the vault descriptor via
  `vault_mutation_path`, so a caller cannot aim it elsewhere — and the binary is an absolute
  platform constant (`/usr/bin/open`, `/usr/bin/xdg-open`, `os.startfile` on Windows),
  existence-checked, never resolved from the agent-writable front of `PATH`. A trusted binary
  is not sufficient on Linux, because **`xdg-open` is a shell script that dispatches to
  whichever helper it finds on `PATH`** (`gio`, `gvfs-open`, `exo-open`, …) — so every POSIX
  spawn in `server.py` passes `_trusted_env()`, which replaces `PATH` with
  `git_ops.TRUSTED_PATH` and keeps the rest (the desktop session needs `DISPLAY` /
  `DBUS_SESSION_BUS_ADDRESS` / `XDG_*`). Otherwise a planted `~/.local/bin/gio` would run on a
  click the user has every reason to trust. The same pin covers `gh auth token` and the
  `osascript` folder chooser, and a test asserts the count of `subprocess.run(` calls equals the
  count of `env=_trusted_env(),` so a new spawn cannot silently omit it. An absent trash
  returns `{empty: true}` rather than being created; a host with no file manager returns 501
  `folder_open_unsupported`, which the UI explains. Allowlisted in `test_spawn_audit.py`.

## Saving: three layers

The word "save" means three different things here, and only the last needs a button.

1. **Disk.** A note edit is written to the file after `SAVE_DEBOUNCE_MS` (1s) of quiet,
   and flushed unconditionally before anything that could lose it — opening another note,
   switching vaults, moving/renaming/deleting, syncing, and `beforeunload`. The user never
   saves the file by hand.
2. **Local git history (autosave).** `POST /api/commit` runs every `AUTO_COMMIT_MINS`
   (5) while `LS.autoCommit` is on — **default ON** (`DEFAULT_AUTO_COMMIT`). It commits and
   stops; it cannot reach a remote. It stages **exactly** the changed `.md` paths it saves,
   named individually, and excludes any path the user has **staged** in git — a staged path is
   a commit they are composing by hand, and an unattended `git add` would overwrite a partial
   `add -p` boundary, while `git commit -- <path>` would lift a fully staged file out of the
   multi-file commit being assembled. A deliberate Sync stages everything, because the user
   chose the moment. Skipped for a read-only vault, while the block editor
   holds an uncommitted draft, and while a save is unreconciled (`dirty` after a failed
   write), and it is silent by design: a tick with nothing pending makes no commit, touches
   no listing, and a failure raises no banner over the user's writing — an explicit sync
   reports errors normally.
3. **The remote (push).** The Sync button, the sync shortcut, and auto-sync
   (`LS.autoSync`, **default OFF**). This is the only layer that leaves the machine.

The row's `pending` badge belongs to layer 3, not layer 1: it is `git status`
reporting the file differs from the last commit, so it appears AFTER the disk write
that caused it. `rowBadge()` therefore suppresses it on a local-only vault
(`showSyncBadge` false) — with no remote there is nowhere to be pending to, autosave
clears it within `AUTO_COMMIT_MINS`, and left visible it reads as "not saved", the
opposite of what it means. An in-flight `deleting` still wins the slot either way.

Autosave defaults on and pushing does not, because a local commit is private and
reversible while a push is neither. That asymmetry is the whole design: doing layer 2
unasked costs the user nothing, and doing layer 3 unasked would be a decision made on
their behalf.

## Save Guard

`PUT /api/note` accepts the `baseMtime` the client received from its read. If the file's
current mtime differs by more than 1ms, the write is refused with 409 and
`{code: "ESTALE", mtime, disk}` — the response carries what is actually on disk so the UI
can offer a merge. This exists because an attached vault is a folder the user also edits
with Obsidian, an editor, or the git CLI, and a blind write would silently clobber that
work. The 1ms tolerance absorbs filesystem mtime rounding.

## External Change Detection

`GET /api/changes` compares an mtime snapshot of the vault's markdown files against the
previous snapshot, bumping a monotonic revision when anything differs and accumulating the
changed paths. A write the app made itself is suppressed for
`SELF_WRITE_GRACE_SEC` (1.5s) so saving a note does not report itself as an external edit.
Detecting a change also drops the search/backlink cache, so the next read rebuilds from
disk.

This replaces the Node original's recursive `fs.watch`. Snapshot comparison needs no extra
dependency and no background thread, and since the UI is the only consumer and it polls,
the observable behaviour is the same.

## Git Behavior

Git runs as the real `git` binary via `asyncio.create_subprocess_exec`, never a shell.

* **Local remotes** need no special handling — real git speaks `file://` and bare paths
  natively. (The TypeScript original carried a hand-written transport module purely
  because isomorphic-git's HTTP client could not.)
* **Clone is FULL, not shallow.** The original defaulted to `depth: 1`, but most servers
  refuse a push from a shallow clone, which would break the app's own sync. Note vaults are
  text, so full history is cheap.
* **Status** compares the working tree directly against HEAD, treating untracked files as
  additions and reporting a rename as a delete plus an add. A repo with no commits reports
  everything as added.
* **Staging is always per-path, never directory-wide.** `auto_commit` names each changed
  path from `status()` as a `:(literal)` pathspec (capped at `MAX_STAGED_PATHS`), for the
  explicit Sync as well as the autosave. `status()` filters `.trash`, and that filter is the
  ONLY thing keeping the trash — including a PRE-EXISTING Obsidian one in a freshly attached
  vault — out of history, since no git ignore rule is written for it. A scope-wide `add -A`
  would sweep it up and push notes the user deleted elsewhere.
  `notes_only` (autosave) narrows the same list further, to `.md` only.
* **Only the autosave truncates at `MAX_STAGED_PATHS` (500); an explicit Sync refuses.** The cap
  exists so a first sweep over a large vault cannot build an argv past the OS limit, and for the
  autosave the remainder is a delay rather than a loss — it pushes nothing, and the next tick
  picks the rest up. A user-initiated Sync cannot truncate, because `status()` reports a rename
  as **two** entries (old path deleted, new path added) and the slice sorts by path: a cutoff
  falling between them would push the deletion half alone, so the note reads as deleted in every
  other clone while the UI reported success. Sync therefore raises with the changed-file count
  and tells the user the autosave is draining them.
* **Sync** commits pending work, fetches, and merges. On conflict the merge is ABORTED so
  the working tree keeps local content, and the result lists each conflicted path with both
  the local and remote versions. Nothing is overwritten.
* **Local-only vaults.** Attaching a repo with no `remote.origin.url` is supported: the
  descriptor records `localOnly: true` with `remoteUrl: null`, and sync stops after the
  commit (`{localOnly: true, pushed: false}`) instead of failing on a remote that does not
  exist. Everything else — listing, search, backlinks, trash, the save guard — needs no
  remote. The UI labels the button "Save locally" and shows "No remote · &lt;branch&gt;"
  in Settings rather than a repo slug.
  `localOnly` is stored EXPLICITLY rather than inferred from a null `remoteUrl`, and sync
  REFUSES a local-only vault that has since gained an origin: `.git/config` is
  agent-writable, so an origin appearing after attach is not a user decision and pushing
  note history to it is the same exfiltration the trusted-remote check prevents.

### Credential handling

A token reaches git through `GIT_CONFIG_COUNT`/`KEY`/`VALUE` carrying an
`http.extraHeader: Authorization: Basic <b64>` for that invocation only. It is deliberately
NOT interpolated into the remote URL, which would persist it in `.git/config` and leak it
into any error that echoes the remote, and NOT passed as a command-line argument, which
would expose it in the process table. Unlike `git -c`, these environment variables are not
copied into a newly cloned repository's config. `GIT_TERMINAL_PROMPT=0` keeps a credential
prompt from hanging the request.

Auth resolution order: the stored PAT, else a token minted on demand from the user's `gh`
CLI login (cached 300s, never written to disk).

## Input Validation

* Note paths resolve through `safe_join`, which rejects anything landing outside the vault
  root after symlink resolution (400).
* **`.trash` is refused when it is a symlink at all** (`trash_dir_path`), not only when it
  escapes the vault. A clone can carry `.trash -> public`, whose target is *contained*, so the
  escape check passes — then `mkdir(exist_ok=True)` follows the link and the note lands at
  `public/One.md`, a path `status()` does not filter (it filters the `.trash/` prefix), so the
  next sync pushes the deleted note. `is_symlink()` (`lstat`) is the only test that sees this;
  `exists()` and `is_dir()` both follow. Both the delete route and the reveal route go through
  the same resolver.
* **Containment is not sufficient, so every caller-supplied path also passes
  `require_note_path()`** (`require_folder_path()` for the new-note `folder`): each component
  must be undotted and the file must end in `.md`. A vault holds far more than notes, and
  `.git/config` is *contained* — an unvalidated delete moved it to `.trash/config.md`, breaking
  the vault and its remote binding, and save / move / duplicate reach the same places. The two
  rules are exactly the ones the note walk applies, so **the addressable surface equals the
  listed surface**: `.git/`, `.trash/` and dotfiles are excluded by construction rather than by
  being named, and a future dotted directory needs no further edit. Applied at all six
  entry points — read, save, delete, duplicate, new-note `folder`, and both ends of a move
  (the message names `from` / `to`). Server-derived paths (the `.trash` reveal) do not go
  through it; they were never caller input.
* `readOnly` vaults refuse every mutating note route (403).
* `POST /api/note/new` decides the name server-side and creates the file with `O_EXCL`, so
  two quick clicks cannot collide or overwrite a file the UI's cached listing did not know
  about.
* `POST /api/note/duplicate` follows the same rule for the copy's name, reads the source
  through the central sensitive-path gate (`read_note_text`) rather than `open()`, and
  resolves the destination directory from the *validated* source folder — so a copy can only
  ever land beside its source inside the vault, and a `.md` symlink aimed at a private key
  cannot be laundered into vault content that search would serve and sync would push.
* `POST /api/note/move` refuses to overwrite an existing file (409).
* Request bodies are capped by the Application's `client_max_size`.

## Folder Picker

`POST /api/pick-folder` opens the macOS folder chooser via `osascript` and returns the
POSIX path. The UI cannot produce an absolute path itself — browser file APIs
(`showDirectoryPicker`, `input[webkitdirectory]`) deliberately withhold real filesystem
paths, and the attach flow needs one. `activate` makes the dialog frontmost rather than
leaving it behind the browser; cancelling raises AppleScript error -128 and is reported as
a plain cancellation. Non-macOS hosts get 501 so the UI falls back to a typed path.
`MD_NOTEBOOK_NO_PICKER` suppresses it, which is how the test suite guarantees no GUI dialog
can open during a run.

## Frontend

The UI is a compiled builtin surface at `website/src/apps/md-notebook/`, routed at
`/md-notebook`. It is NOT a dynamic `ui.entry` bundle: the gateway serves app UI bundles
only from `apps_dir()/<name>/ui/`, and builtin registration writes metadata without
copying files there, so a package-resident builtin must compile into the frontend.

Knowledge-sync calls go to the HOST API (`/api/knowledge/*`) rather than the app namespace,
because registration needs the user's dashboard session. `/api/knowledge` is therefore
declared in the manifest's `permissions.api`.

### Notes panel affordances

* **New note** lives in the panel header (top right, beside the vault selector) and creates
  the note at the vault's top level — outside every folder. It is there rather than beside
  the document controls because "outside all folders" only reads as a location next to the
  tree it applies to. The file is created **empty**: `note_title` already falls back to the
  basename, so a seeded `# Untitled` heading was a duplicate of the title the user then had to
  delete, and it did not follow a later rename. Preview's trailing click-to-append region
  (80px) is what keeps an empty note clickable.
* **Drag to file.** A note row is draggable; the drop targets are a folder row (files into
  that folder), another note row (files into *that* note's folder, so a drop inside a folder
  does not fall through), and the list background (files at the vault root). All of it goes
  through the same `POST /api/note/move`, which refuses to overwrite.
### Delete flow

Delete is the one destructive row action, and it is staged rather than immediate:

1. **Confirmation is an in-app dialog** (`ConfirmDialog.tsx`), not `window.confirm`. The native
   dialog rendered as an OS sheet in the desktop app — the only surface that left the app's own
   design language, and unthemeable by construction. The dialog scrims the Notes pane (the app
   root is `position: relative`, so the dashboard chrome stays visible), focuses the destructive
   button so Enter confirms, and closes on Escape or a scrim click. The scrim carries
   `role="presentation"` *before* its `onClick` — the `accessible-interactive-elements` AUTOSDE
   rule matches `<div … onClick` only when no `role=` precedes it.
2. **The row goes 50% opacity while the request is in flight**, with the sync-badge slot showing
   `deleting` instead of `pending` — the badge slot already means "state of this file", so a
   second indicator would be noise. The row stops responding to clicks and drags, and its action
   bar is hidden, for the duration.
3. **On success the panel lands on the adjacent note** — the next one DOWN in the visible order,
   falling back to the one above when the deleted note was last, and to the empty state when it
   was the only one. "Visible order" is resolved from `visibleNotePaths`, which mirrors whichever
   mode is rendering (search results / flat list / `flattenVisibleNotes` for the tree) and omits
   collapsed folders, because a note the user cannot see is not a place to land.
4. **On failure nothing is lost.** The clear + neighbour hand-off run only after the server
   confirms, so a read-only vault (403) or a network error leaves the note open, in the list, with
   any unsaved text intact. This is why the editor is not cleared optimistically.

* **Hover action bar** — pin/unpin, duplicate, rename, delete — revealed on row hover,
  using the chat session list's recipe (card surface, thin border, only the hovered row's
  bar visible). The keyboard path is `.mdnb-row-actions:has(:focus-visible)` — on the BAR
  and scoped to keyboard focus, and both halves are load-bearing. `.mdnb-row:focus-within`
  also matches the row holding focus itself (`Clickable` is tabbable), so it left the bar lit
  on the note you last **selected**; and plain `:focus-within` on the bar keeps matching after
  a MOUSE click, so it left the bar lit on the note you last **pinned**. Delete is the sole
  destructive action, tinted `--danger` and gated on the confirmation dialog above; it also cancels
  the debounced save first, because a pending flush would write the open note straight back
  and resurrect the deleted file.
* **Rename** is inline on the row, seeded from the displayed title. It reuses the same
  `relocate` path as the inline document title, so the folder is preserved and separators /
  filename-illegal characters are stripped — a rename can never move the note.
* **Pins are local and per-vault** (`mdnb-pinned-<vaultId>` in localStorage). Deliberately
  NOT frontmatter: a pin is a per-device reading aid for this sidebar, and writing it into
  the note would commit a UI preference into the user's git history on the next sync. A
  pinned note sorts first *within its own container* — it is not hoisted out of its folder,
  which would misreport where the file lives. Pins follow a move/rename and are dropped on
  delete.

## Tests

`test/test_md_notebook.py` drives the aiohttp app through a signed test client, so the
proxy-HMAC middleware is exercised on every call rather than bypassed. Coverage includes
the save guard, path traversal, unique note naming, duplicate-note naming and containment,
move-without-overwrite, external-change
detection, self-write suppression, token file permissions, the knowledge flag round-trip,
and a real sync against git fixtures including the conflict path.
