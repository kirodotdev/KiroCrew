# Code intelligence in the side panel editor

## Overview

The chat side panel's code editor can jump to a symbol's definition, list its
references, show its hover text and list the file's symbols. The answers come from
real Language Server Protocol servers the gateway runs, one per (language family,
project root). A server is started only from the side panel's Language servers tab,
where the user picks the project it indexes; a file request never starts one.
Nothing is indexed by Kiro Crew itself.

All of it is the builtin app `code-intel` (`src/kiro_crew/apps/builtins/code_intel/`),
built on three App Kit extension points and nothing private to core: its shortcuts
are `contributes.editorBindings`, its tab is `contributes.panelTabs`, its routes are
`backend.routes`, and the one thing a shortcut needs beyond those, the reader's
unsaved text, is the `permissions.editor: ["content"]` grant. Disabling the app
removes the shortcuts, the tab and the routes together.

| Piece | Where |
|---|---|
| Manifest: bindings, the tab, permissions | `app.json` |
| Server families, launch commands, project-root detection | `servers.py` |
| LSP framing and result conversion | `protocol.py` |
| One server process (JSON-RPC over stdio, document sync) | `client.py` |
| The pool and the four requests | `manager.py` |
| `POST /api/apps/code-intel/binding` and `.../pool` | `backend/routes.py` |
| Lifecycle: stop every server and both pools on disable and gateway stop | `hooks.py` |
| The app's catalogs (12 languages), read by the bundle and by the backend | `locales/*.json`, `i18n.py` |
| Worker pools | `pools.py`: `request_executor()` (`mc-code-intel`) for everything that waits on a language server; `control_executor()` (`mc-code-intel-ctl`) for `servers`, `start`, `restart`, `stop`, `detect`, `status` and the path checks, so a full request pool never stalls the tab. Shut by the hook, created again on the next request |
| The Language servers tab | `website/src/apps/code-intel/panel.tsx`, built by `website/vite.app-bundles.config.ts` into `ui/dist/panel.mjs` |
| The editor side of a binding | core: `website/src/apps/editorBindingContributions.ts`, `website/src/components/editorBindings/EditorBindingCard.tsx` |

## Servers

`SERVER_SPECS` lists each family with the file extensions it serves, its launch
commands in preference order and its root markers:

| Family | Commands tried |
|---|---|
| python | `pyright-langserver --stdio`, `basedpyright-langserver --stdio`, `pylsp`, `jedi-language-server` |
| typescript (TS and JS) | `typescript-language-server --stdio` |
| go | `gopls` |
| rust | `rust-analyzer` |
| java | `jdtls -data <cache dir>` |
| kotlin | `kotlin-language-server` |
| c (C and C++) | `clangd` |
| ruby | `ruby-lsp`, `solargraph stdio` |

Kiro Crew installs none of them. The first command found on the gateway's `$PATH`
that passes `validate_provider_executable` (the repo's executable-provenance check)
is launched. A server planted in the project checkout or the workspace root is
refused there, since it would otherwise run the moment the user opens a file. A
refusal is answered `server_refused` with the reason, not `no_server`.

The project root is the nearest ancestor holding one of the family's markers, then
the nearest `.git`, then the file's directory. The walk stops below the home
directory and the filesystem root. JVM servers keep their index in
`$XDG_CACHE_HOME/kirocrew/code-intel/<family>/<root hash>` (default `~/.cache`).

### Java in a Brazil workspace

jdtls prefers `build.gradle` and runs the Gradle build to learn a project's
classpath. A BrazilGradle build fails without a live Midway session, which leaves
no Java project, and every answer comes back empty except a syntax-only outline.
bemol, run at the workspace root, writes `.project` and `.classpath` for every
package, so `initialization_options` turns the Gradle and Maven importers off
whenever a root carries both files and jdtls opens the Eclipse project instead.
The data directory is keyed on that mode too, so a root that gains the files after
a failed Gradle import starts from an empty jdtls workspace rather than reloading
the failed project.

### Project references

An Eclipse `.classpath` entry `<classpathentry kind="src" path="/Name"/>` names
another project, which jdtls resolves only when a project with that `<name>` is
open in the same instance. `project_folders` follows those references from the
root, transitively and up to `MAX_PROJECT_FOLDERS`. The name is a project name,
not a folder: Eclipse maps it to a folder through its workspace, which is not
available here, so the project is looked for among the sibling folders of the one
that references it, by its `.project` `<name>`. The sibling folder called `Name`
is tried first (one file read, no listing); only when it is not that project are
the other siblings listed (hidden and sensitive folders skipped). The server opens
them all: as LSP `workspaceFolders`, and as
`initializationOptions.workspaceFolders`, which is what jdtls imports. A
reference no sibling provides is named in the setup hint. A file in a package
that a running server already opened as a reference is answered by that server
(`LspManager._covering`) instead of a second one indexing it again. `.classpath`
is a Java root marker, so a package with Eclipse files and no build file is
still its own root. Nothing here is Brazil-specific: it is how any Eclipse
workspace lays out projects that depend on each other.

### Library classes

jdtls answers a definition in a jar with a `jdt://` URI. The client declares
`extendedClientCapabilities.classFileContentsSupport`, and `_with_class_files`
fetches each such class with `java/classFileContents` (the attached source, or a
decompiled stub) and writes it read-only under
`$XDG_CACHE_HOME/kirocrew/code-intel/java-classes/<uri hash>/<Name>.java`, beside
an `origin.json` with the URI and the server root (`classfiles.py`; both files
are written whole beside their final names and moved into place, so concurrent
writers of one class never leave a partial file). Up to
`MAX_CLASS_FILES_PER_ANSWER` classes are written per answer, within
`CLASS_FILES_BUDGET_SECS` for them all; the rest, and any class without source,
stay counted in `external`. A request on a written file goes to the server
running for the recorded root (it starts none) with the `jdt://` URI as its document,
so definition, hover and outline keep working inside library code, across
gateway restarts too. `origin` accepts a file only when its folder and name are
the ones its URI hashes to.

`setup_hint` names what to run when the server cannot see the project: bemol (and
the workspace to run it in, the nearest `packageInfo` ancestor) when the files are
missing, or when `.project` lacks `org.eclipse.jdt.core.javanature`. The Gradle
importer rewrites `.project` without the nature, so a package opened in Gradle
mode once needs bemol run again. When `.classpath` names jars that are not on disk (Eclipse files written on
another machine, or before the package cache was cleaned), the hint counts them
and names one: jdtls then resolves none of their classes, and a simple name such
as `Component` can land on an unrelated JDK class.

Each server is spawned through `wrap_argv(mode="strict")`, `cgroup_scope_argv` and
`popen_limited` with the `build` rlimit profile, with the project root as its working
directory. Strict mode keeps the credential homes out of the server's view.

## Pool lifecycle

- A server starts from the LSP tab (`start`, `restart`, `workspace_symbols`), never
  from a file request: Cmd-click and F12 are answered by the server
  already running for the file's root, or by one that opened the root as a
  referenced project, and otherwise refused with `not_running` (409), which the
  viewer turns into a pointer at the LSP tab. A start that failed within the
  backoff is reported as `server_failed` instead, with its reason. `initialize`
  runs on a background thread. A request waits up to `READY_WAIT_SECS` for it,
  then answers `server_starting` (503) while the handshake continues.
- At most `MAX_SERVERS` run at once; the least recently used is stopped to make room.
  A server idle for `IDLE_SHUTDOWN_SECS` is stopped on the next request.
- A server that fails to start is not retried for `FAILED_START_BACKOFF_SECS`. The
  failure keeps the error and the server's stderr tail for the LSP tab.
- A start failure, or a server that exits on its own after starting, is logged at
  WARNING with the last `_STDERR_TAIL_BYTES` of its stderr, so the log names the
  cause rather than only "language server exited".
- `LspClient._note` keeps the server's last `_MAX_NOTICES` problems:
  `window/showMessage` errors and warnings, `language/status` errors, and, when
  jdtls's `ProjectStatus` is `ERROR`, "The project did not load" with the last
  error from `window/logMessage` (the log itself is too noisy to show). A
  `WARNING` status means the project loaded; it becomes a warning only when the
  log named a reason.
- The dashboard's `on_cleanup` hook stops every server
  (`register_code_intel_lifecycle`). From then on `code_intel_manager()` refuses
  with `shutting_down` instead of building a fresh pool, so a request that lands
  during cleanup starts no server nothing would stop.
- A stop that reaches a client while its start thread is still spawning finds no
  process; the start thread sees the stop once the spawn returns and kills the
  child itself (`_state_lock` guards `_proc` and `_closing` together), so an
  eviction at the server cap, or a Stop during startup, never orphans a server.

## Requests

`POST /api/apps/code-intel/binding` is what core sends when one of the app's
`contributes.editorBindings` fires (see `docs/app-kit/manifest-reference.md`):
`{"item_id", "path", "line", "character", "dirty", "sha256", "trigger", "text"?}`.
`item_id` names the binding (`definition`, `definition-click`, `references`,
`hover`, `symbols`), `line` and `character` are 0-based with `character` in UTF-16
code units (the browser's string indexing and LSP's default encoding), `sha256`
is the hash of the editor's buffer. The manager uses the copy the server already
holds, or the file on disk, when the hash matches; otherwise the route answers
`{"stale": true}` and core, because the app holds `permissions.editor:
["content"]`, re-posts once with `text`. On the server side the client sends
`didOpen` the first time and a full-text `didChange` whenever the text moved, so
positions match unsaved edits; past `MAX_OPEN_DOCUMENTS` open on one server the
oldest is closed with `didClose` and reopened on its next request. A lone
surrogate in `text` goes out as `?` (one UTF-16 unit for one). The file read from
disk is capped at `MAX_DOCUMENT_BYTES`.

The route is owner-only and 403 `app_disabled` while the app is off. The path goes
through `_validate_dashboard_path`, `realpath` and `is_sensitive_path`; a
credential store is 403 `sensitive_path`. Allowed and denied requests are
SEL-audited (`operation="code_intel"`). The body cap is `_MAX_REQUEST_BYTES`.

The answer is the host's `EditorBindingResult`, built by `_binding_answer`:

- `definition` / `references`: one location is `open: {path, line, character,
  label, detail}`, which core follows at once; several are `open: [...]`, which core
  offers in its picker, each row labelled `basename:line` with the source line as
  `detail`. Lines are 1-based. A non-`file:` location (a `jdt://` class file) is
  counted and reported in `markdown` ("N results outside this machine's files"), as
  is a truncated list; one inside a credential store is dropped silently.
- `hover`: `markdown` with the server's text.
- `symbols`: always a list, one row per symbol in document order, the label indented
  by depth with figure spaces, `detail` the kind and signature.
- Nothing to list: `markdown` with the server's notices and the setup hint under
  "The language server reported a problem", or "Nothing found". Both pass through
  `redact_credentials` on the way out of the manager.
- `title` is the card's heading ("Definitions", "References", "Symbol info",
  "Outline") in the dashboard's language, from the app's catalogs.

For `WARMUP_RETRY_SECS` after a server starts, an empty definition or references
answer, and a ContentModified or ServerCancelled error, is retried once a second
inside the request timeout: servers answer that way while their first index builds.

Errors carry `{"error", "code", "detail", "server", "root", "install", "refused"}`:
`no_server` and `server_refused` (422), `unsupported` (422), `too_large` (413),
`not_found` (404), `not_running` (409), `server_starting` (503), `timeout` (504),
anything else from the server (502). `error` is the reader's sentence from the
app's catalogs in the dashboard's language; `detail` is the
server's own text (a start error, a JSON-RPC error message), passed through
`redact_credentials`, as is the stderr tail in the log line that reports a start
failure or an exit. Core shows `error` in the file panel's notice.

The language is the request body's `lang`, which core adds to every binding
request and the tab adds to every pool call: the dashboard's resolved UI language.
It passes `normalize_ui_language_tag`, the gate `dashboard.language` passes; a
request without a usable `lang` uses `ui_language_tag(config)`, then English. The
app does not pick a language itself.

## Pool and project operations

`POST /api/apps/code-intel/pool` takes `{"op", ...}`: the Language servers tab's
operations. All are owner-only and 403 while the app is disabled; those with a
directory validate it like a file (`_resolve_dir`: dashboard path check,
`realpath`, `is_sensitive_path`, must be a directory) and are SEL-audited.

| `op` | Body | Answer |
|---|---|---|
| `status` | `path` (a file) | `{"server", "root", "available", "command", "install", "refused"}`: which server would answer for the file. A probe: it creates no data directory. |
| `symbols` | `path` (a file) | `{"symbols": [{name, kind, detail, container, depth, line, character, endLine}], "truncated", "server"}`: the file's outline, from disk, flattened in document order. The tab's tree uses it. |
| `servers` | none | `{"servers": [...], "maxServers", "idleShutdownSecs"}`; each entry has `server`, `root`, `state` (`starting`, `ready`, `failed`, `exited`), `error`, `stderr`, `pid`, `command`, `uptimeSecs`, `ageSecs`, `idleSecs`, `shutdownInSecs`, `retryInSecs`. Failed starts within the backoff are listed too. `error` and `stderr` pass through `redact_credentials`. |
| `detect` | `path` (a directory) | `{"directory", "projects": [{server, root, files, available, command, install, refused, state}], "truncated"}`. Walks breadth-first, at most `DETECT_MAX_ENTRIES` entries, `DETECT_MAX_DEPTH` levels and `DETECT_MAX_PROJECTS` (server, root) pairs, following no link and skipping hidden directories and `DETECT_SKIP_DIRS`. Sensitive directories are skipped too; each is checked inline (`is_sensitive_resolved_path`) only when `path_contains_sensitive` says one lies under the directory at all, since a per-directory pool check made a 14,000-directory workspace take 16 s. Starts nothing and creates no data directory (`resolve_argv(create=False)`). |
| `workspace_symbols` | `server`, `root`, `query` (at most 256 characters) | `{"symbols": [{name, kind, container, path, line, character}], "truncated", "external", "hidden"}`, capped at `MAX_WORKSPACE_SYMBOLS`. Starts the server if none runs. |
| `workspace_symbols_all` | `query` (1 to 256 characters) | `{"symbols": [... with server, root], "truncated", "external", "hidden", "servers", "errors": [{server, root, error}]}`: every running server, merged. Starts nothing. |
| `open_class` | `server`, `root`, `uri` (`jdt://`) | `{"path"}`: the library class written out from the running server at `root`. 409 `not_running`, 404 `no_source`. |
| `start` | `server`, `root` | Starts the server for the root; nothing when one is running. The tab's Start button. |
| `restart` | `server`, `root` | Stops the server and forgets a failed start, then starts a fresh one. |
| `stop` | `server`, `root` | `{"ok": bool}`: whether there was a server or a failed start to clear. |

`start`, `restart` and `workspace_symbols` start a server, so their `root` must be the root
`find_project_root` gives for a file directly in it (`_spec_at_root`). A caller
cannot start a server over an arbitrary directory such as a home or `/` by naming it.

## Dashboard

The shortcuts are the app's `contributes.editorBindings`, dispatched by core's
editor exactly as any app's (`website/src/apps/editorBindingContributions.ts`):
core matches the event, POSTs the path, caret and hash to the app's `binding`
route, and acts on the answer. The app declares them for the extensions
`SERVER_SPECS` covers, with these default chords; the reader rebinds or unbinds
each in Settings → Shortcuts → Editor shortcuts, like every app's bindings:

| Binding | `item_id` |
|---|---|
| Cmd-click (macOS) / Ctrl-click | `definition-click` |
| F12 | `definition` |
| Shift+F12 | `references` |
| Alt+F12 | `hover` |
| Cmd/Ctrl+Shift+O | `symbols` |

Hover uses Alt+F12 rather than VS Code's Cmd/Ctrl+K chord because Cmd/Ctrl+K is
the dashboard's quick search and refused at install. Pierre turns a
primary-modifier click into an extra caret; the editor collapses back to the new
caret before asking. A primary-modifier press that moves more than `CLICK_DRAG_PX`
before release, or whose new selection is a range, is Pierre's multi-caret
selection and fires nothing.

A single definition navigates at once: inside the file through the editor's
`jumpToLine`, otherwise by opening the file at that line through `onFileOpen`.
Every other answer fills core's `EditorBindingCard`, a card over the editor shown
as loading from the moment the chord fires: a searchable list with arrow-key
selection, Enter to open and Escape to close, or the hover's markdown. The card
takes focus in every state, so Escape closes it rather than reaching the editor
behind it; the list is a `listbox` (the filter a `combobox`) naming the active row
through `aria-activedescendant`. An answer that lands after the reader closed the
card, or after another binding fired, is dropped. On a file with unsaved changes
the app answers `stale` to the hash and core sends the text once; the reader is
told to save only if the app lacked the editor grant, which the shipped manifest
does not.

The **Language servers** tab is the app's `contributes.panelTabs` entry, an ESM
bundle core mounts through `AppHost`. It reaches the dashboard through
`@kirocrew/app-sdk` only: the scoped API for its own `pool` route and the host's
`/api/browse-files` (both in `permissions.api`), and `useSidePanelHost()` for the
chat's project directory and to open a file in the panel beside it. It shows, in
this order:

- **Symbol search** sends `workspace_symbols_all`: `workspace/symbol` on every
  server that has finished starting, in parallel, merged and deduplicated, exact
  and prefix matches first, at most 500. It starts nothing; a server that fails
  is listed under the results without hiding the others'. A result opens the
  file at its line; a library class (`uri`, no `path`) is written out first with
  `open_class`.
- **Outline** works on one running (`ready` or `starting`) server, chosen in a
  picker when several run; failed and stopped servers are not offered. It is a
  lazy file tree (`/api/browse-files`, hidden folders left out) of that server's
  root, followed by the projects it opened beside it. A file
  `codeIntelSupported` accepts expands into its `symbols` outline; a symbol
  opens the file at its line.
- **Running language servers**: the `ready` and `starting` entries of the pool
  from `servers`, polled every 5 seconds while the tab is on screen. While the tab
  is hidden (`useAppInfo().active`) none of its queries fetches or refetches on
  window focus; a start, restart, stop or Refresh invalidates the pool and
  detection answers only, not every expanded outline. A folder that could not be
  listed shows the reason. Each row has the state, uptime and time until the idle
  shutdown, the pid, restart and stop buttons, and a Details disclosure with the
  command, the error and the stderr tail.
- **Other language servers**: the failed and exited pool entries, then the
  `detect` answer for the chat's project directory, for languages with no running
  server: a Start button where one is installed, the install hint or the
  provenance refusal otherwise.
- **Shortcuts**: where the chords are listed and changed (Settings → Shortcuts →
  Editor shortcuts), which shows the ones in force rather than the defaults.

Each pool entry also lists its `notices` (the server's reported problems and the
setup hint). The tab stays mounted while another is selected, so the tree keeps
what was expanded.

The tab's strings are the app's own catalogs (`locales/*.json`), chosen by the
dashboard's `activeLocale()`; the backend reads the same files for the sentences
it answers a binding with. The store copy (`displayName`, `description`,
highlights) is in the core catalogs under `apps.codeIntel.manifest`, as for every
builtin.

## Tests

- `tests/test_code_intel.py`: framing, conversions, root detection, and the pool
  against a real child (`tests/fake_lsp_server.py`).
- `tests/test_code_intel_api.py`: both routes' gate, validation, error mapping and
  the binding answer shapes; the sentence language from `lang`, then config.
- `tests/test_manifest.py`: the manifest validates, is discovered, and names the
  operations the route runs.
- `website/src/apps/code-intel/panel.test.tsx`: the tab bundle, through a stand-in
  for the SDK.
- `website/src/test/editorBindingContributions.test.ts`,
  `EditorBindingCard.test.tsx` and the `app bindings` block of
  `PierreEditorImpl.test.tsx`: core's side of a binding.
- `website/src/test/appBundles.test.ts`: the bundle build lists this app and emits
  where its manifest mounts it.
