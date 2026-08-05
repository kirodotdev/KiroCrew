# Papyrus Module

## Overview

Papyrus is an opt-in (`defaultEnabled: false`) built-in app: a LaTeX paper editor
with a live PDF preview and an AI co-author. The user writes in a split-pane
workspace, presses Cmd/Ctrl+S to compile with `pdflatex` or `tectonic`, and reads
the rendered PDF beside the source. A machine with **no TeX installation at all**
is a supported starting point: the app offers a one-click, digest-pinned Tectonic
install (see Managed compiler), so `pip install kirocrew` does not ship a
broken-by-default editor. Compiler output is parsed into a clickable
diagnostics list that jumps the editor to the offending line. A paper can be
cloned from any git remote and committed/pulled/pushed from the toolbar. The
co-author panel is a real KiroCrew chat session scoped to the paper, so the agent
edits the LaTeX while the user watches the PDF update.

Ported from a standalone app by **tricatte**; see
`src/kiro_crew/apps/builtins/papyrus/ATTRIBUTION.md` for what was kept and what
changed.

## Routes

All routes live under `/api/apps/papyrus/` and are registered by
`apps/builtins/papyrus/backend/routes.py:register_routes`. Every handler is
wrapped in `_require_enabled` (403 while the app is disabled) — pinned by
`test_routes.py::TestRouteRegistration`, since routes are registered once at
gateway startup and an unwrapped one would answer regardless of the opt-in.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Whether a compiler and `git` exist on the host, so the UI can explain "Cmd+S does nothing" before the first compile rather than as a failure. Also carries `managed` — whether this platform has a pinned Tectonic build, whether one is installed, and the provisioning job's live state — so ONE poll drives both the warning banner and the install progress |
| POST | `/compiler/provision` | Install the managed Tectonic compiler. **202 + poll `/health`**; idempotent (200 when already installed), 422 `compiler_unsupported_platform` where no pinned build exists. See Managed compiler |
| GET | `/projects` | List papers (name, main-document mtime, whether a PDF exists) |
| POST | `/projects` | Create a paper from the standard `article` template (or a supplied `template`) |
| POST | `/projects/clone` | Shallow-clone a git remote as a paper. A repo with no `.tex` is rejected **and removed**, so the name is not held hostage by an unopenable project |
| GET | `/project` | Resolved main document + file list + PDF presence |
| DELETE | `/project` | Delete a paper and its tree. **500 `project_delete_incomplete`** when the tree survives the removal — the old `ignore_errors` path answered `ok: true` over a partial delete, so the name stayed taken with no explanation |
| GET | `/files` | The paper's file list |
| GET | `/file` | Read one file as UTF-8 text |
| PUT | `/file` | Save one file (atomic, `newline=""`) |
| POST | `/file` | Create a file, refusing to clobber |
| DELETE | `/file` | Delete a file; the main document is refused |
| PUT | `/main` | Choose which `.tex` is the main document |
| POST | `/compile` | Compile and return `{ok, log, errors[], duration_ms}`. **422 on a failed compile, and the log/diagnostics still ride the response** — that payload is exactly what the user needs to fix the document. **422 `compiler_sandbox_unavailable`** is the distinct case where this host could not build an OS-level sandbox, so the compiler never ran (see Platform) |
| GET | `/pdf` | Serve the compiled PDF (see Security) |
| GET | `/git` | Branch, dirtiness, ahead/behind, recent commits — or `{is_git: false}` |
| POST | `/git/commit` | Stage all + commit. "Nothing to commit" is a **success** |
| POST | `/git/push` | Push. **401** on an auth failure (distinct from 422) so the UI can say "log in" |
| — | (all git routes) | **422 `git_sandbox_unavailable`** when the host has no sandbox backend, so git never ran (see Platform) |
| POST | `/git/pull` | Rebase-pull with autostash. **409** on a conflict |

## Storage Schema

All data under `app_data_dir("papyrus")` (typically
`~/.kiro/crew/apps/papyrus/data/`):

```
vendor/tectonic/tectonic    # the managed compiler (see Managed compiler)
projects/<name>/
  main.tex                  # or whatever .papyrus.json names
  references.bib
  sections/…                # nested source is supported
  .papyrus.json             # {"main_file": "..."} — see Untrusted Config below
  main.pdf, main.aux, …     # compiler output, filtered from the file list
```

A project name is normalized (trim, spaces→hyphens, lowercase) and then validated
as **one slug segment** (`PROJECT_NAME_RE`), so it can never contribute a path
separator, a `..`, a drive letter, or a leading dash that a later `git`/`pdflatex`
argv could read as an option. Normalization never launders a traversal into an
accepted name — pinned by
`test_store.py::TestSafeProjectDir::test_normalize_does_not_make_a_traversal_safe`.

## Security model

### Path containment (`store.py` is the gate)

A LaTeX editor writes user-controlled relative paths and hands a directory to an
external compiler, so two functions own every filesystem decision:

- **`safe_project_dir(name)`** — the name must match `PROJECT_NAME_RE`; a
  **symlink OR Windows junction at the project entry is refused outright** (like
  `_config_path`: this is a directory KiroCrew creates, so a link there is
  illegitimate wherever it points); and the resolved directory must be a **strict
  child** of `projects_dir()`.

  Junctions matter because `is_symlink()` does not report them — they are reparse
  points — and they are the link type a Windows user can create WITHOUT elevation.
  A symlink-only guard was therefore bypassable on exactly the platform this app
  now supports. `store.is_reparse_link` is the shared answer (used here and by
  `gitops`'s attributes guard) so the two cannot drift on which link types they
  cover; `os.path.isjunction` is 3.12+ and always `False` off Windows, resolved
  once via `getattr` the same way `apps/manager.py` does.

  The strictness is load-bearing. The check previously read
  `resolved != base_resolved and base_resolved not in resolved.parents`, and
  `projects/<name> -> .` satisfied the first disjunct exactly — resolving the
  "project" to the projects ROOT. Every other paper then counted as a child, so
  `safe_child` accepted `other-paper/main.tex` as an in-project path (cross-project
  read **and** write), and `DELETE /project` ran `rmtree` on the projects root,
  destroying every paper. `projects_dir` itself is never a project, so nothing
  legitimate needed the equality case.
- **`safe_child(project, relative)`** — rejects empty/over-long paths, absolute
  POSIX **and** Windows/UNC paths, **backslashes anywhere** (a separator on
  Windows, so a `/`-only check would be a bypass), NUL bytes, any `..`/`.`/empty
  segment, **anything under `.git`** (see below), and — after `resolve()` —
  anything landing outside the project. That last check is what catches a
  **symlink escape**, where every segment looks innocent but a link points out of
  the tree; a cloned repo can ship one. Both the file-link and the mid-path
  directory-link variants are pinned.

  **`.git` is refused at any depth, case-insensitively, on the RESOLVED path.**
  Containment cannot cover it: `.git/config`, `.git/info/attributes` and
  `.git/hooks/*` are all legitimately INSIDE the project, so every other rule above
  passes them — but they are not document content, they decide what `git` EXECUTES
  (`filter.<x>.clean`, `core.*Command`, hooks run directly). Without the rule,
  `PUT /file` turned "edit a file in my paper" into code execution on the next
  commit or push — i.e. on the path that deliberately runs in `standard` sandbox
  mode with `~/.ssh` readable.

  **Requested AND resolved** — neither alone is enough, because a symlink moves
  `.git` in either direction. Requested-only misses `meta -> .git` + `meta/config`
  (no `.git` in the request, resolves straight into the machinery).
  Resolved-only misses `.git/config -> ../repo-config` (the request names `.git`,
  resolution leaves it, and git still READS that file as its config through its own
  path). The rule is not "where do the bytes live" but "never write anything git
  treats as config", so both are checked. The resolved comparison is against the
  project-RELATIVE part, so a `.git` component in an absolute path *above* the
  project (a data home that itself sits inside a checkout) cannot refuse every
  legitimate file. Case-insensitive because macOS and
  Windows resolve `.GIT` to the same directory; at any depth because a submodule's
  `.git` has the same execution surface. The app's own git work goes through
  `gitops`, and `list_files` already hides dotfiles, so nothing legitimate is lost.

Both also consult the shared `security.is_sensitive_path()`, so a project that
somehow sat beside a credential store still could not read it.

`list_files` skips hidden entries and **skips symlinks entirely** (following one
is how a tree walk leaks containment) and is bounded by `MAX_PROJECT_FILES`.

### Untrusted `.papyrus.json`

The per-project config arrives **inside a cloned repository**, so it is untrusted
input. `get_main_file` re-validates the configured value through `safe_child` on
**every read** and ignores it on failure — without that, a hostile repo naming
`../../etc/passwd.tex` would pivot through the PDF-serving route. Pinned by
`test_store.py::TestMainFile`.

### The compiler is never given shell escape

`pdflatex` is invoked with **`-no-shell-escape` explicitly**. With shell escape
enabled, a `\write18{...}` inside a `.tex` file is arbitrary command execution —
and a `.tex` here is untrusted by construction (the agent writes it; a cloned repo
supplies it wholesale). Tectonic keeps shell escape off unless `-Z shell-escape`
is passed, and we never pass it. The document is passed after `--` so a
dash-leading filename cannot become an option. Pinned by
`test_latex.py::TestCompilerArgv`.

### Spawn discipline

Every compiler and git invocation routes through
`sandbox.sandboxed_spawn_argv` (OS-level sandbox + credential-scrubbed env) and
carries `sandbox.resource_limit_preexec()`, so a runaway macro expansion or a
hostile repo hook hits a kernel ceiling rather than the host's memory. The
environment handed to a LaTeX child is `apps.registry.minimal_env` plus only the
TeX-specific variables (`TEXMFHOME`, `TEXINPUTS`, …) — never the gateway's own,
so Slack/AWS credentials cannot reach a child running untrusted document content.
`GIT_TERMINAL_PROMPT=0` is set because the gateway has no terminal, so a
credential prompt would hang until the timeout. Every spawn is SEL-audited.

### PDF serving

The PDF is content the agent or a cloned repo produced, so
`GET /pdf` responds with `Content-Disposition: inline`,
`Content-Security-Policy: sandbox; default-src 'none'; object-src 'none'`,
`X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. It therefore
cannot script the dashboard's origin. Pinned by `test_routes.py::TestPdf`.

That **per-response** header is the containment, and it does not depend on which
element embeds the document — which matters, because the pane embeds the PDF in an
**`<iframe>`, not an `<object>`**. The dashboard's own base CSP
(`dashboard/server.py`) sets `object-src 'none'`, so Chromium and Firefox refused
the plugin document and the app's headline feature rendered its
"cannot display a PDF inline" fallback; it went unnoticed because WebKit does not
enforce the directive for this case, so it worked in Safari. `frame-src 'self'`
already permits a same-origin frame, so no CSP was widened. The download
affordance is now persistent chrome rather than replaced content, since an
`<iframe>` has no fallback children. Pinned by
`website/src/test/PapyrusPdfPreview.test.tsx`.

## Event-loop discipline

The gateway runs everything on one asyncio loop, and this app's two headline
operations are a multi-second compile and a network git call — so
`no-blocking-call-on-event-loop` is the rule that shaped the port. The upstream
app was a stdlib `ThreadingHTTPServer` on its own port using blocking
`subprocess.run`; here:

- every child process is spawned with `asyncio.create_subprocess_exec` and awaited
  under an `asyncio.wait_for` timeout (`COMPILE_TIMEOUT_SEC` 120s,
  `BIBTEX_TIMEOUT_SEC` 60s, clone 120s, network git 60s, local git 15s);
- a timeout kills the whole **process tree** via
  `platform_compat.kill_process_tree_async` and then reaps it, so no zombie holds
  its pipes;
- every synchronous filesystem call — the tree walk, file read/write, compiler
  discovery, the `.bst`/`.bib` search-path `rglob`, `installed.json` reads, and the
  managed-compiler probes behind `/health` and `/compiler/provision` — is offloaded
  with `asyncio.to_thread`;
- **the path-validation gate is offloaded too, because validation is itself
  blocking.** `_project` / `_project_for_create` / `_safe_relative` read like cheap
  string checks, but each calls `Path.resolve()` plus a `stat`-family probe
  (`is_dir`/`exists`). With `KIROCREW_HOME` on a stalled network mount those
  syscalls block for as long as the mount takes to answer, wedging every session,
  every cron job and the liveness heartbeat *inside the authorization check* —
  before the handler has done any work of its own. Twenty-two call sites had that
  shape;
- the compiler download itself runs on a daemon thread, not the loop and not a
  pooled executor (see Managed compiler). Pinned by
  `test_tectonic.py::TestEventLoopDiscipline` and
  `test_routes.py::TestProvisionCompiler::test_the_handler_never_blocks_the_event_loop`.

### Offloading the gate without weakening it

The gate is the app's authorization boundary (see Path containment), so moving it
off the loop must not change *when* it runs relative to the access it authorizes.
Two rules hold:

- **One hop per handler.** A handler groups its validation together with the
  filesystem work that validation permits into ONE sync closure behind a single
  `asyncio.to_thread`. Splitting them across two hops would put an `await` between
  the check and the use, letting another request interleave — so grouping makes the
  check/use window strictly *narrower* than it was when the gate ran inline. `GET
  /pdf` gains the most: the `is_file()` probe and the `read_bytes()` that follows it
  are now in one closure instead of two hops.
- **Validation-only hop where grouping is impossible.** `POST /projects/clone`,
  `POST /compile` and the four `git` routes cannot group, because the "use" is an
  `await` on a subprocess that needs the validated path. They take one hop for the
  gate alone; their check/use window is unchanged, since it was already an `await`
  wide.

`web.HTTPException` raised inside a worker thread propagates through the `await`
unchanged, so the gate keeps answering 400/404/409 from off the loop. It
subclasses none of `ValueError` / `OSError` / `FileExistsError` / `store.PathRejected`
— the exceptions each handler catches and converts — so an authorization refusal
cannot be swallowed into a 500. That is asserted, not assumed
(`TestHttpExceptionsAreNotSwallowed`).

Pinned by `test_papyrus_routes.py::TestNoBlockingCallsOnTheLoop`, an **AST** walk
over every `async def` in `routes.py` that fails if one *calls* a known-blocking
helper (naming it as an `asyncio.to_thread` argument is allowed, and a nested sync
closure is skipped because that is what the worker runs). A new handler that
validates inline therefore fails without anyone remembering to extend a list; the
guard's own detection is proved by running it over source shaped like the defect.
`TestValidationErrorsSurviveTheOffload` pins the status codes and the ordering —
a refused name never reaches `gitops` or the compiler.

## Compile pipeline

1. Resolve the main document (`store.resolve_main_file`).
2. Locate a compiler, widest-trust first: `pdflatex` on PATH, then `tectonic`, then
   a userspace TeX Live install under `~/texlive` (the usual no-sudo route, which is
   not on PATH), then **the app's own managed Tectonic install**. The managed copy
   is probed LAST on purpose — a user who installed a real TeX distribution must
   keep using it, so provisioning can never displace their `pdflatex`. Cached
   process-wide, including the negative result, so a successful provision MUST call
   `reset_compiler_cache()` or the stale "no compiler" answer sticks (it does, from
   the provisioning job's completion). Pinned by
   `test_tectonic.py::TestResolutionOrder`.
3. Extend `BSTINPUTS`/`BIBINPUTS` with **every** project subdirectory holding a
   `.bst`/`.bib`. Conference templates stash `acl_natbib.bst` under
   `templates/<conf>/`, and without this bibtex fails with "I couldn't open style
   file".
4. Run the bibliography cycle when the `.aux` shows the document cites anything:
   `pdflatex → bibtex → pdflatex → pdflatex`. Cutting it short leaves `[?]` in the
   PDF. Tectonic drives that cycle itself, so it is skipped there. The cheap
   question (read the `.aux`) is asked before the expensive one (locate `bibtex`),
   so a paper with no bibliography pays neither.
5. Without a bibliography, re-run once if the log says "Rerun to get…" — how a
   table of contents or a `\ref` settles. Not retried when the pass **failed**: a
   failing pass that also asks to rerun is broken, not merely unsettled.
6. `ok` requires exit 0 **and** a PDF on disk — `pdflatex` can exit 0 having
   produced nothing usable.

### Log parsing

`parse_log` extracts four shapes, capped at `MAX_DIAGNOSTICS`:

| Shape | Level |
|---|---|
| `file:line: message` (the most reliable form) | error |
| `! message` + a nearby `l.<n>` | error |
| `LaTeX Warning` / `Package <p> Warning`, line embedded in the text | warning |
| Over/underfull boxes | typesetting |

**Consecutive `!` errors must not borrow one another's `l.<n>`.** The line lookup
is bounded to the text before the next `^!`; without that bound the second error
inherits the first's line and the editor jumps somewhere wrong while looking
authoritative. Carried over from upstream and pinned by
`test_latex.py::test_two_bangs_do_not_share_a_line`.

## Managed compiler

A stock machine has no LaTeX compiler, so before this the app was broken by
default on a fresh `pip install kirocrew`: every compile answered *"No LaTeX
compiler found"*, and the only remedy on offer was a multi-gigabyte TeX Live
install. There is no LaTeX compiler on PyPI either — the `tectonic` name there is
an unrelated placeholder (version `0.0.0dev`, no files).

`tectonic.py` closes that gap. **Tectonic** is one self-contained static binary
(10-22MB) that downloads only the TeX support files a document actually needs and
drives its own bibtex/rerun cycle, which `latex.py` already knew how to handle.
The module follows `kiro_crew/embeddings.py`, the in-tree precedent for the same
problem (a large per-platform artifact, fetched over plain HTTPS, sha256-pinned,
installed persistently under the data home, downloaded off the loop with retries,
behind a status surface the UI polls).

### Pinned artifacts

Release `tectonic@0.17.0`. **The digest, not the tag, is the trust anchor** — a
re-tagged or rebuilt asset fails verification and is discarded, so bumping
Tectonic means recomputing EVERY digest in `_ASSETS`. Each digest was produced by
downloading the published asset and hashing it (`shasum -a 256`); sizes were
cross-checked against the GitHub release API and one asset was re-downloaded to
confirm the digest reproduces.

| Platform / arch | Asset | Why |
|---|---|---|
| macOS arm64 | `…-aarch64-apple-darwin.tar.gz` | |
| macOS x86_64 | `…-x86_64-apple-darwin.tar.gz` | |
| Linux x86_64 | `…-x86_64-unknown-linux-musl.tar.gz` | **musl**: statically linked, so one artifact works across distributions with no glibc-version floor |
| Linux aarch64 | `…-aarch64-unknown-linux-musl.tar.gz` | same (covers Graviton) |
| Windows x86_64 | `…-x86_64-pc-windows-msvc.zip` | |

Windows-on-ARM is deliberately absent: the release publishes no
`aarch64-pc-windows` asset, so that host reports `supported: false` and keeps the
manual install path. `platform.machine()` normalization handles the
`arm64`/`aarch64` and `AMD64`/`x86_64` naming splits.

`KIROCREW_PAPYRUS_TECTONIC_URL` overrides the download URL for a mirrored or
air-gapped deployment. It must be `https://` (a `file://` or `http://` value is
refused and logged), and the pin still has to match — an override changes **where**
bytes come from, never **which** bytes are accepted. Logged URLs are redacted to
scheme+host+path so a signed query string or userinfo never reaches a log.
`KIROCREW_PAPYRUS_SKIP_TECTONIC_DOWNLOAD=1` refuses provisioning outright, which
is how the test suite guarantees it never reaches the network.

### This is NOT the system-package install `pptx-maker` refuses

`docs/system-specs/modules/pptx-maker.md` records a deliberate product decision:
that app's upstream shelled out to `brew`/`apt-get` from a browser request, and
*installing a system package is a host-level action a web request must not take*;
a test there pins the absence of `POST /deps/install`. **That decision stands.**
`POST /compiler/provision` is a categorically different act, and the differences
are the reason it is allowed:

- **No package manager and no privilege.** Nothing is elevated, no `sudo`, no
  system installer runs. One archive is unpacked; nothing inside it is executed at
  install time.
- **Nothing is written outside this app's own data dir.** The binary lands in
  `…/apps/papyrus/data/vendor/tectonic/` — never a system prefix, never
  `/usr/local`, never a shell profile — and `PATH` is not mutated; the compiler is
  invoked by absolute path.
- **The bytes are pinned**, so the operator receives exactly the artifact this
  source names, or nothing.
- **It is reversible by deleting one directory** this app owns.

The distinction is also stated in `tectonic.py`'s module docstring and on the
handler, so a reader meeting either one first cannot mistake it for the same
mistake.

### Safe extraction (`tarfile`/`ZipFile` are traversal sinks)

`extractall` writes wherever a member name points, which makes any downloaded
archive a path-traversal sink. Nothing is written until every member passes
`_reject_member_name`, which refuses — mirroring `store.safe_child`'s rules —
absolute POSIX paths, Windows/UNC paths, **backslashes anywhere** (a separator on
Windows, so a `/`-only check would be a bypass), any `..` segment, NUL bytes and
empty names. Beyond names:

- **tar**: only regular files and directories are accepted. A symlink or hardlink
  member escapes the destination even when its own name looks innocent, and
  device/FIFO members have no business in a compiler tarball. Ownership is dropped
  and the mode normalized, so **setuid can never survive extraction**. The check
  runs as stdlib's own `filter=` callable so it happens INSIDE `extractall` with no
  TOCTOU gap; on **Python 3.10**, which this project still supports and where
  `filter="data"` does not exist, the same callable is applied to every member and
  the extraction restricted to the validated list (the `TypeError` fallback shape
  `snapshot.py` already uses). Both legs are tested.
- **zip**: `ZipFile` has no filter hook, so validation is explicit and runs over
  the whole `infolist` **before any member is written** — a hostile archive lands
  nothing at all. A Unix `S_IFLNK` mode smuggled in `external_attr` (zip's only way
  to carry a symlink) is refused too. Both `orig_filename` **and** `filename` are
  checked, and that is load-bearing on Windows: `ZipInfo.__init__` rewrites `\` to
  `/` whenever `os.sep` is `\`, so on Windows `filename` never carries a backslash
  and the backslash rule could only ever fire on POSIX — the one platform where a
  backslash is *not* a separator. Checking the name as the archive actually spells
  it keeps the guard self-sufficient instead of dependent on a stdlib
  normalization detail.
- Both cap a member at `_MAX_MEMBER_BYTES` (256MB), bounding a decompression bomb
  even though the digest pin already means the archive can only be the named one.

Pinned by `test_tectonic.py::TestSafeTarExtraction` / `TestSafeZipExtraction`,
including that a refused archive leaves the destination empty and writes nothing
outside it.

### Provisioning flow and failure modes

On-demand only — never at import and never at gateway start, so a 22MB fetch is a
decision rather than a startup cost. `POST /compiler/provision` answers **202
immediately** and hands the work to a **daemon** thread: a transfer must not pin
interpreter exit (a `ThreadPoolExecutor` or the loop's default executor joins its
non-daemon workers at exit, so a Ctrl-C would hang for up to the HTTP timeout),
and the default executor is also what the loop uses for DNS. One job at a time; a
second click while one runs starts nothing. The sha256 is computed over the
**stream**, so verification cannot be fooled by a file swapped between write and
check, and the binary is `chmod`-ed and then `os.replace`-d into place, so it is
never observable at its final path without its exec bit.

Every failure degrades cleanly and leaves the manual route working:

| Condition | Outcome |
|---|---|
| Unsupported platform/arch | 422 `compiler_unsupported_platform`; the message still names TeX Live/tectonic |
| Digest mismatch (corrupt, tampered, or a swapped mirror object) | Refused, nothing installed, the error names the mismatch |
| Unsafe archive member | Refused, nothing installed, nothing written outside the destination |
| Extracted binary absent or implausibly small | Refused — a stub named `tectonic` is not a compiler |
| Download failure | Retried with capped backoff; no staging or work directory survives |

`binary_installed()` is size-gated as well as existence-gated, so a truncated or
half-written file can never read as a usable compiler.

## Git

`gitops.py` mirrors the upstream flows with the same hard-won behaviour:

- **Clone** — the URL must match `GIT_URL_RE` (http/https/git/ssh/scp-like) and is
  passed **after `--`**, so a value like `--upload-pack=…` cannot be smuggled into
  argv. A failed clone removes the partial directory.
- **Pull** — rebase-pull with autostash, because a dirty tree (typically compiler
  artifacts not in `.gitignore`) would otherwise refuse the rebase outright. On a
  real conflict the rebase is aborted and the stash restored, so the tree returns
  exactly as it was. **If the stash pop itself conflicts the stash is deliberately
  KEPT** and reported (409) — silently discarding the user's edits to let the
  operation "succeed" is the worse outcome. Pinned by
  `test_gitops.py::test_a_failed_pop_keeps_the_stash`.

  **Every** post-stash failure path restores the stash, including the ones that
  raise from inside `_git` rather than returning a non-zero code — a pull that
  exceeds `NETWORK_TIMEOUT_SEC`, or `git` disappearing mid-operation. Those skip
  the `code != 0` branches entirely, so without an explicit `except GitError` the
  user would be handed an apparently-clean tree with their work parked in an
  unannounced `papyrus-pull-autostash` stash — indistinguishable from "my edits
  vanished". The recovery pop is best-effort and never masks the original error.
  Pinned by `test_papyrus_gitops.py::TestPull::test_a_raising_pull_still_restores_the_stash`
  and `::test_a_failed_recovery_pop_does_not_mask_the_pull_error`.
- **Push** — auth failures are classified across transports (`_AUTH_MARKERS`) and
  surface as **401**, so the UI can say "log in" rather than "something broke".

### Repo config cannot execute a command (and the assumption this rests on)

**The threat.** A cloned repository — or the co-author agent, which can write into
the project — controls `.git/config` and `.git/info/attributes`, and a dozen git
config keys name an *executable* git will run. Push deliberately runs in
`standard` sandbox mode (an SSH push needs the key), so on that path the OS
sandbox does **not** back this up: the denylist is the load-bearing control.

`_git` therefore neutralizes three classes, each shaped by what `-c` can and
cannot reach:

1. **~19 `-c` overrides** — `core.sshCommand`, `core.hooksPath=/dev/null`,
   `core.fsmonitor`, `core.alternateRefsCommand`, `gc.recentObjectsHook`,
   `credential.helper`, `core.askPass`, `gpg.program`, `core.pager`,
   `core.editor`, `sequence.editor`, `diff.external`, `interactive.diffFilter`,
   `protocol.ext.allow=never`, and the rest.
2. **An attributes pin** (`_ATTRIBUTES_PIN`, re-established on every call) and
   **pack-program flags** (`--upload-pack=` / `--receive-pack=`) — because
   `filter.<name>.clean` and `remote.<name>.*` have **attacker-chosen subsection
   names**, so no fixed `-c` key can cover them. Verified: `-c 'filter.*.clean='`
   did not stop the filter, and `remote.pushDefault` / `branch.<b>.remote` routed
   straight past the config pins.
3. **`GIT_PROXY_COMMAND=true` in the env** — because `core.gitProxy` is
   **multi-valued**, so `-c` APPENDS and git uses the repo's value first. `=none`,
   `=` and `=true` were all tested via `-c` and all still executed the script.

**Two ways the pin itself was defeatable**, both fixed and both pinned by
`TestTheAttributesPinCannotBeNeutralized`:

- **"Already present" is not "last".** The idempotence check returned early when the
  pin line was already in the file, so an attacker pre-seeded the pin and appended
  `* filter=x` after it. Git resolves attributes per name with the LAST match
  winning, so the attacker's rule won while the early return prevented the real pin
  from being re-appended (verified against real git: `check-attr` reported
  `filter: x`). The fix strips every existing copy and appends exactly one, so
  repeat calls still converge — but the pin's POSITION is what gets re-established.
- **A symlink at the name.** `read_text`/`write_text` both follow one, so
  `attributes -> /dev/null` made the pin unobservable AND unwritable (silently
  inert forever), and `attributes -> <any file>` turned a `GET /git` status poll
  into an arbitrary-file append that also read the victim's contents back. Both
  **All three** of `.git`, `.git/info` and `.git/info/attributes` are now refused
  if any is a link — symlink **or Windows junction**, via the shared
  `store.is_reparse_link`. Each segment needs its own check for a different reason:
  `mkdir(exist_ok=True)` is a no-op on a directory link, so `info` would silently
  relocate the write; and a linked **`.git`** roots the whole chain on an unverified
  indirection — point it at another repository and `info`/`attributes` are
  legitimate non-links *inside that repo*, so both inner checks pass while a status
  poll rewrites a different repository's attributes, outside the project entirely.
  The rule holds for every segment KiroCrew traverses by name, not just the leaf.
- **The rewrite preserves the existing mode.** `atomic_write` renames a fresh temp
  file into place, so without carrying the mode across, a user who had tightened
  `.git/info/attributes` to 0600 would find it 0644 after any status poll — a
  protective guard silently loosening permissions on the file it protects. `None`
  on a first write (nothing to preserve) and on Windows, where the POSIX bits are
  not the ACL that governs.

**The assumption, stated plainly:** the `-c` list is an *enumeration of git's
command-executing config keys as of the git versions tested*. A future git that
adds a new hook-or-program key is not covered, and **no drift guard against
upstream is possible** — there is no "ignore repo config" switch
(`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_NOSYSTEM` suppress the *other* two scopes; the
repo's own config is always read), and git publishes no machine-readable list of
executing keys. **Re-audit this list against `git config` documentation on a major
git upgrade.**

The tests are the real record, and several run against **real git** (skipped when
absent) — so they catch a regression in our own code, but not an upstream git that
grows a new key: `TestRepoConfigCannotExecuteCommands`,
`TestFsmonitorAndOtherHooksAreNeutralized`,
`TestPackProgramsArePinnedForEveryRemote`, `TestGitProxyCannotExecuteACommand`,
`TestGitattributesCannotNameAProgram` in `test/test_papyrus_gitops.py`.

## Frontend

`website/src/apps/papyrus/`, registered at `/papyrus` in `builtinRegistry.ts`
(nav icon `ScrollText` in `builtinIcons.tsx`).

Two views behind one route:

- **No paper open** → `ProjectList`, which carries the standard page layout
  (`PageHeader` + `px-6 pb-8 overflow-y-auto flex-1 min-h-0` + a `StatCard` row +
  `Card`/`CardTitle`/`InfoTip` sections + a `table-striped` table + `EmptyState`).
- **A paper open** → a full-bleed split workspace (file tree, Monaco, diagnostics
  | PDF | optional co-author panel) with its own toolbar. A paper and its PDF need
  the whole viewport, which is why the editor is not inside the page container.

Dependency decisions, both deliberate:

- **Monaco, not CodeMirror.** Monaco is already vendored here
  (`@monaco-editor/react` + `monaco-editor`, loaded from the local bundle by
  `utils/monacoLocal.ts`), so the upstream CodeMirror stack is not reintroduced.
  Monaco ships no TeX grammar, so `latexLanguage.ts` registers a small Monarch
  tokenizer; if that registration fails, Monaco falls back to plaintext, which is
  the correct degradation. Compiler diagnostics are pushed into Monaco's own
  **marker store**, so squiggles survive scrolling/folding/resize for free.
- **The browser's PDF viewer, not `pdfjs-dist`.** Upstream shipped ~1 MB of JS
  plus a worker chunk and a hand-written text layer to reproduce what Chrome,
  Firefox, Safari and Edge all do natively (selection, find-in-page, zoom,
  thumbnails, print). This repo has no PDF renderer today and adding one to
  reimplement a built-in viewer is not a trade worth making. `<object>` (not
  `<iframe>`) so `onError`/fallback content can offer a download when a browser
  genuinely cannot render inline. The URL carries a version counter, because the
  same-URL document would otherwise be served from the in-page cache and a
  recompile would appear to do nothing.

### Leaving a file always flushes it

The editor buffer is the one piece of genuinely local state, and it exists **only
in memory** until a save lands — so resetting it without writing does not "forget"
the work, it destroys it, with nothing on disk to recover from. Every way of
navigating away from a dirty buffer therefore flushes it first:

- `openFile` awaits the save before switching file.
- `saveAndCompile` (Cmd/Ctrl+S) awaits the save before compiling, because the
  compiler reads the file off disk and would otherwise typeset the previous
  revision.
- `closeProject` (the toolbar's **Papers** back button) awaits the same save
  before tearing the workspace down. This is the gesture most likely to surprise:
  the toolbar advertises "Editing {file} — unsaved" immediately beside the button.
  If the flush **fails**, the workspace deliberately stays mounted and surfaces the
  error rather than discarding the very edits the flush exists to protect.

Save-then-leave rather than a confirm dialog, so all three paths behave
identically and the user is never asked a question whose safe answer is always
"save". Every such path goes through ONE `flushBuffer()` helper rather than repeating the
save inline, because the list kept growing and each new caller was a chance to
forget: `openFile`, `closeProject`, `reloadOpenFile`, **creating a file** (its
success switches `currentFile` away, abandoning the outgoing buffer) and **pulling**
(a rebase rewrites the file on disk, so flushing *after* would push the pre-pull
buffer over upstream's version — flushing first turns the bad case into a visible
git conflict). The helper returns false on a failed write and every caller treats
that as "do not proceed". Pinned by `PapyrusCloseProject.test.tsx`.

Other conventions: React Query owns all server state; the only local state is the
editor buffer and which pane is open. Framer Motion animates the diagnostics
drawer and the chat panel. Lucide icons only, zero emoji. Rows are `<Clickable>`;
icon-only buttons carry `aria-label`. Every user-facing string is a
`apps.papyrus.*` catalog key in all 11 locales.

### Co-author session

`CoAuthorPanel` mounts the FULL native `ChatPage` (`switchSlot()` +
`<ChatPage embedded embedMode="chat" noUrlSync />`), the same approach
`ArtifactChatPanel` takes — so the co-author gets follow-up chips, question cards,
tool groups, regenerate, voice and approvals. Upstream hand-rolled a raw
WebSocket with backoff reconnect plus a regex that stripped tool-use markup from
the stream; all of it is gone.

The slot is created on first open, its key is remembered per paper in
`localStorage` (`kc:papyrus:slot:<project>`), and silent context naming the paper
and its main document is injected via `api.chatSlotContext` — the
`papyrus-writing` skill supplies the rest. Stale slot keys are pruned whenever the
project list lands, because a name reused after a delete would otherwise
resurrect the old paper's conversation.

When the session goes busy→idle the app re-reads the open file and recompiles: the
agent edits on disk, so the pane the user is watching is stale until then. That
transition is read from `selectComposerBusy`, the store's single answer to "is this
session working", rather than a private `chat_done` subscription.

## Skill

`src/kiro_crew/builtin_skills/papyrus-writing/SKILL.md` — bundled (NOT the
repo-only top-level `skills/`), so every `pip`/DMG install receives it, per the
skill-bundling rule in `AGENTS.md`. It carries the project path, the compile
workflow, an error→cause table, the venue/style rules, and the figure/table/
equation/citation patterns. It is trigger-loaded on LaTeX vocabulary rather than
`always: true`, so it costs nothing in unrelated sessions.

The manifest deliberately declares **no** `skills` entry: a builtin app's
directory receives only `app.json` at registration, so a manifest-declared path
would log a "skill directory not found" warning on every boot while the bundled
copy is what actually reaches users.

## Platform

`app.json` declares `platform.os: ["macos", "linux", "windows"]`. Windows was
added after the port; what it took, and what it did NOT, is worth recording
because the guess ("Windows means work in `tectonic.py`") was wrong in both
directions.

**Already correct at merge:** the managed compiler (a pinned
`x86_64-pc-windows-msvc` asset, the `.zip` extraction leg, `tectonic.exe`,
`chmod` via `platform_compat.chmod_safe`), and process handling throughout —
no bare `fcntl`/`os.killpg`/`signal.SIGKILL`, `start_new_session=IS_POSIX`
+ `creationflags=CREATE_NEW_PROCESS_GROUP`, and kills via
`platform_compat.kill_process_tree_async`.

**What actually had to change, none of it in `tectonic.py`:**

- **The sandbox chokepoint.** Windows has no sandbox backend (user namespaces are
  Linux, `sandbox-exec` is macOS), so `wrap_argv` fail-closes with
  `SandboxUnavailableError` — and both spawn sites called
  `sandboxed_spawn_argv` OUTSIDE their `try`, so it escaped as an unhandled 500
  on **every** compile, clone, commit, push and pull. The refusal is now
  translated, not bypassed: `latex` returns `CompileResult.sandbox_error` →
  422 `compiler_sandbox_unavailable`, and `gitops` raises
  `GitSandboxUnavailable` (a `GitError` subclass, so existing handlers keep
  working) → 422 `git_sandbox_unavailable`. Both carry the sandbox layer's own
  remedy text, which names the `agent.sandbox_allow_unsandboxed_exec` opt-in that
  `docs/guides/windows-install.md` documents for this host. Bypassing the wrap was
  rejected: `strict` mode is what stops `\input{../../.aws/credentials}` from
  typesetting the operator's keys into the PDF, and `gitops` runs `standard`
  precisely so an SSH push can see the key.
- **`minimal_env`'s allowlist** (`apps/registry.py`) needed two fixes, and this one
  fails early and opaquely rather than loudly: a Windows child without `SystemRoot`
  usually dies before `main()` (DLL/crypto init resolves through it), and one
  without `USERPROFILE` cannot resolve `TEXMFHOME`.
  1. The list was POSIX-only. The Windows location hints are now allowlisted
     alongside the POSIX ones (same set and reason as
     `kiro_prerequisite._SAFE_ENV_KEYS`).
  2. The match was case-SENSITIVE, which made (1) inert on the platform it was for:
     Windows env names are case-insensitive and `os.environ` upper-cases keys, so
     `items()` yields `SYSTEMROOT` while the list held the documented `SystemRoot`.
     The comparison now folds **on Windows only** — POSIX keeps `PATH` and `Path`
     distinct, where a fold would admit a lookalike.

  The fold widens case, never the key set: the credential-scrub property is
  unchanged, and `TestMinimalEnvHonorsWindowsCaseInsensitivity` pins all three
  properties.
- **`os.pathsep`** for `BSTINPUTS`/`BIBINPUTS`. A hardcoded `":"` was both the
  wrong delimiter on Windows and a splitter of `C:\proj\bib` into two useless
  fragments — reproducing the "I couldn't open style file" failure that env var
  exists to prevent.
- **`platform_compat.rmtree_force`** for every tree that may hold a git checkout.
  Git writes loose objects read-only, and Windows checks the read-only ATTRIBUTE
  on the file being deleted (POSIX consults the parent directory), so
  `rmtree(..., ignore_errors=True)` silently left `.git/objects` behind while the
  handler answered `ok: true` — the project name stayed taken and the next create
  answered 409. Delete now reports `project_delete_incomplete` if the tree
  survives.
- **`PlatformConfig`** (`apps/manifest.py`) had no `windows` row in
  `_OS_TO_PLATFORM`/`_PLATFORM_TO_OS`, so `"windows"` was not expressible in ANY
  manifest — a declaring app silently matched nothing — and `current_os()`
  returned the raw `"win32"`. The default stays `["macos", "linux"]`: an app opts
  in by naming `windows`.
- **21 `os.symlink` tests** across `test_papyrus_store.py` /
  `test_papyrus_latex.py` gained the file's existing
  `skipif(sys.platform == "win32")` guard — symlink creation needs privilege on
  Windows, and no papyrus entry exists in `conftest`'s `collect_ignore` or
  `windows-expected-failures.txt`, so the Windows shard ran them unguarded.

Windows-on-ARM remains unsupported by the managed compiler (no upstream asset);
that host reports `supported: false` and keeps the manual install path.

## Files

| Path | Purpose |
|------|---------|
| `src/kiro_crew/apps/builtins/papyrus/app.json` | Manifest (opt-in, author `tricatte`, Apache-2.0) |
| `.../papyrus/ATTRIBUTION.md` | Upstream credit + the port's diff |
| `.../papyrus/backend/store.py` | Path-containment gate, project/file layout |
| `.../papyrus/backend/latex.py` | Compiler discovery, the compile pipeline, log parsing |
| `.../papyrus/backend/tectonic.py` | The managed, digest-pinned Tectonic install (pins, safe extract, provisioning job) |
| `.../papyrus/backend/gitops.py` | Clone/status/commit/push/pull, **and** the repo-config RCE denylist (19 `-c` overrides + the attributes pin + pack-program flags + `GIT_PROXY_COMMAND`) |
| `.../papyrus/backend/routes.py` | aiohttp handlers + `register_routes` |
| `src/kiro_crew/builtin_skills/papyrus-writing/SKILL.md` | The co-author's LaTeX skill |
| `website/src/apps/papyrus/PapyrusPage.tsx` | Route entry; project list vs. workspace |
| `website/src/apps/papyrus/ProjectList.tsx` | Landing view (standard page layout) |
| `website/src/apps/papyrus/PapyrusEditor.tsx` | Monaco source pane + marker push |
| `website/src/apps/papyrus/PdfPreview.tsx` | Native PDF viewer pane |
| `website/src/apps/papyrus/FileTree.tsx` | Collapsible source tree |
| `website/src/apps/papyrus/DiagnosticsList.tsx` | Clickable compiler messages |
| `website/src/apps/papyrus/CoAuthorPanel.tsx` | Embedded native chat |
| `website/src/apps/papyrus/api.ts` | Typed fetch wrapper |
| `website/src/apps/papyrus/lib.ts` | Pure helpers (tree, word count, persistence) |
| `website/src/apps/papyrus/latexLanguage.ts` | Monaco LaTeX tokenizer |
| `website/public/app-assets/papyrus/` | Icon + light/dark hero art |

## Tests

| File | Covers |
|------|--------|
| `test/test_papyrus_store.py` | Traversal/symlink/backslash/NUL defenses, project-name slug rule, **the `.git` refusal (any depth, any case) and the symlinked-project-entry refusal incl. the self-referential `-> .` case**, untrusted `.papyrus.json`, main-document discovery, bounded walk, file I/O |
| `test/test_papyrus_latex.py` | Log parsing (incl. the two-bangs rule), the `-no-shell-escape` invariant, the pass sequence, timeout kill, compiler discovery + cache, env minimalism (incl. the Windows location hints, and that widening the allowlist admitted no secrets), the platform `os.pathsep` for `BSTINPUTS`, and that a sandbox refusal is reported rather than bypassed |
| `test/test_papyrus_gitops.py` | URL allowlist + `--` placement, autostash flows incl. the kept stash and the raising-pull restore, auth classification, sandbox/preexec routing, tree-kill, the repo-config denylist against real git, **that the attributes pin ends up LAST even when pre-seeded and is never written through a symlink**, and that a sandbox refusal becomes `GitSandboxUnavailable` |
| `test/test_papyrus_routes.py` | The `_require_enabled` gate on every registered route, name/path authorization, response contracts, PDF security headers, git status mapping, the provisioning endpoint (202/idempotent/unsupported/one-job) and its off-loop discipline |
| `test/test_papyrus_tectonic.py` | Platform→asset mapping incl. the arch-naming splits, digest-shape and mismatch/tamper refusal, tar **and** zip traversal + symlink + setuid + bomb refusal, the Python-3.10 no-`filter` leg, atomic install + exec bit, unsupported-platform degradation, no partial install after a failure, that a user's own `pdflatex` still wins, cache reset after install, and daemon-thread/off-loop discipline |
| `website/src/test/PapyrusLib.test.ts` | Tree building/flattening, artifact filter, word count, slot persistence + pruning |
| `website/src/test/PapyrusDiagnostics.test.tsx` | Click/Enter-to-jump, non-interactive line-less rows, collapsed hints, no emoji |
| `website/src/test/PapyrusCloseProject.test.tsx` | Leaving the workspace flushes a dirty buffer, stays put when the flush fails, and writes nothing when clean |
| `website/src/test/PapyrusPdfPreview.test.tsx` | The preview embeds an `<iframe>` and never `<object>`/`<embed>` (the `object-src 'none'` block), the `key={src}` remount that makes a recompile visible, the persistent download affordance, and the pre-compile empty state |

The backend tests live in the repo-level `test/` tree, not an in-package
`tests/`: `setup.cfg` sets `testpaths = test transfer`, so a test under
`src/kiro_crew/apps/builtins/...` is never collected by CI.

Every backend test mocks its subprocesses — no `pdflatex`, `bibtex` or `git` is
ever invoked, so the suite runs on a host with no TeX installation.

**No test reaches the network.** Every compiler download is mocked at the
`urllib.request` opener, and `test_papyrus_tectonic.py` sets
`KIROCREW_PAPYRUS_SKIP_TECTONIC_DOWNLOAD=1` for the whole module as a second belt,
so even a test that slipped past its mock is refused before a socket opens rather
than pulling 22MB in CI.
