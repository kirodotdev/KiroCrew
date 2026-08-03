# Notes Module

Last Updated: 2026-07-31

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
| `vaults.json` | Vault descriptors. No secrets. Written via a temp file + `os.replace`. |
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
| `POST /api/vaults/attach` | Adopt an existing checkout; 409 if already attached; 403 if the folder resolves into a protected location OR contains one (e.g. the home directory — sync's `git add -A` from such a root would stage `~/.ssh`/`~/.aws` wholesale; checked list-based via `security.path_contains_sensitive`, no tree walk) |
| `DELETE /api/vaults` | Forget the descriptor. FILES ARE NEVER DELETED. |
| `PUT /api/vaults/knowledge` | Persist the knowledge flag and source id |
| `PUT /api/pat` | Store or clear the token |
| `PUT /api/note` | Save, guarded by `baseMtime` |
| `DELETE /api/note?path=` | Delete a note |
| `POST /api/note/new` | Create a uniquely named note |
| `POST /api/note/move` | Move or rename; 409 rather than overwrite |
| `POST /api/sync` | Commit, fetch, merge, push |
| `POST /api/pick-folder` | Native folder chooser; 501 when unsupported |

### Capability probe

Both health routes return a `features` list: `createdAt`, `attach`, `changes`,
`saveGuard`, `forget`, `pat`, `newNote`, `move`, `knowledge`, `pickFolder`. The gateway
keeps an app's backend alive across UI reloads, so a process running older code than the
page would otherwise surface as confusing "no route" errors; the UI compares this list and
names the missing capabilities instead.

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
* **Sync** commits pending work, fetches, and merges. On conflict the merge is ABORTED so
  the working tree keeps local content, and the result lists each conflicted path with both
  the local and remote versions. Nothing is overwritten.

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
* `readOnly` vaults refuse every mutating note route (403).
* `POST /api/note/new` decides the name server-side and creates the file with `O_EXCL`, so
  two quick clicks cannot collide or overwrite a file the UI's cached listing did not know
  about.
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

## Tests

`test/test_md_notebook.py` drives the aiohttp app through a signed test client, so the
proxy-HMAC middleware is exercised on every call rather than bypassed. Coverage includes
the save guard, path traversal, unique note naming, move-without-overwrite, external-change
detection, self-write suppression, token file permissions, the knowledge flag round-trip,
and a real sync against git fixtures including the conflict path.
