# Migration: Playwright MCP + Kiro Crew proxy to Playwright CLI

Status: proposed. Replaces the browser stack wholesale rather than adding a
second path, because two browser backends would double the surface that already
produces the defects this migration retires.

## Why

Today an agent drives a browser through `@playwright/mcp` behind a Kiro Crew
proxy that exists to keep accessibility trees out of the model context. The
proxy earns its keep, but the surrounding machinery does not:

- The launcher is resolved at runtime through PATH and an `npx` fallback, so a
  gateway start depends on npm registry auth. When a token expires every
  `browser_*` tool disappears.
- Twenty-two MCP tool schemas are re-sent every request. Measured against the
  server this host actually runs: 22 tools, 14,080 bytes of schema, roughly
  3.5K to 5K tokens per turn that no compression can remove.
- The compressed outline still lands in the context on every snapshot.
- Browser Mode is a persistent on/off flag, and every write path has to be
  taught not to destroy operator config as it flips.

`@playwright/cli` (verified: v0.1.18) answers all four, and does so with
capabilities we currently hand-roll.

## Verified facts this plan rests on

Established by running the CLI on a developer host, not from documentation:

| Fact | Evidence |
|---|---|
| Snapshot goes to disk, stdout gets a path | `### Snapshot` / `- [Snapshot](.playwright-cli/page-<ts>.yml)`; 315 bytes on disk for example.com, ~250 chars of stdout per command |
| Dashboard can be served over HTTP | `show --port <n>` documented in `--help` as "start as a blocking http server on this port"; logs `Listening on http://localhost:45613` |
| Dashboard is loopback-only by default | `--host` "defaults to localhost"; a request from the host's LAN address fails to connect |
| **Default bind is IPv6 only** | `lsof` reports `IPv6 ... TCP localhost:45613 (LISTEN)`; `http://127.0.0.1:<port>/` fails outright. `--host 127.0.0.1` produces an IPv4 listener |
| **Root path answers 302, not 200** | `curl -o /dev/null -w %{http_code}` returns `302` on `/` |
| Dashboard includes remote control | Docs: "Live viewport with tab bar, navigation controls, and full remote mouse/keyboard input. Press Escape to release." |
| No capability gating exists | Docs, verbatim: "In the CLI all capabilities are always available -- there's no gating." |
| Skills install is agent-neutral and targetable | `install --skills` accepts `claude` (default) or `agents`; `--global` installs into the home directory instead of the workspace |
| Operator launch flags already have a home | Config schema carries `browser.launchOptions.args`, `launchOptions.proxy`, `contextOptions.viewport/locale/userAgent/storageState/permissions` |
| Install does not require a global install | `npm install -g @playwright/cli@latest` or `npx playwright-cli`; Node.js 20 or newer |

## What is deleted

| Component | Lines | Replaced by |
|---|---:|---|
| `mcp_playwright_proxy.py` | 1,284 | snapshot-to-disk, `screenshot`, `pdf` |
| `browser/setup.py` | 2,077 | `install --skills`, the CLI's own config file |
| `browser/command_bus.py` | 397 | dashboard remote input |
| `browser/cli.py` | 257 | `state-save`/`state-load`, `cookie-*` |
| `browser/auth.py` | 212 | same |
| `browser/screencast.py` | 91 | `show --port` (see the frontend section) |
| `test_browser_setup.py` | 2,288 | far smaller suite; most cases test machinery that stops existing |
| `test_browser_screencast.py` | 661 | same |
| `test_browser_native_routing.py` | 448 | same |
| `test_mcp_playwright_proxy.py` | 432 | same |

Roughly 9,700 lines of code and tests. 115 files across `src/`, `website/src/`,
`docs/` and `test/` mention playwright and need a sweep.

Retired concepts, each of which currently has code and tests of its own:
`KIROCREW_PLAYWRIGHT_CMD`, the `npx` fallback, `playwright-config.json`
generation, `playwright-storage-state.json` assembly, the extension token file,
`playwright-extension-mode`, `browser-mode-enabled`, the four-value registration
status, the agent-shadow scan, and the entry-carryover sidecar.

## Consent and approval model

Tool presence means **availability**, not consent to skip approval. The absent
binary means browsing cannot run; an installed binary means an approved shell
turn can invoke it. This distinction matters because an existing user install or
a launcher planted in an agent-writable PATH directory is not a user decision.

There is no separate capability toggle: the CLI exposes no internal capability
gating once a shell command runs. The dashboard therefore enforces consent at the
ordinary shell approval boundary. Under normal mode each untrusted
`playwright-cli` command prompts; a trusted-command pattern, session trust, or
auto-approve mode is the explicit decision that can skip that prompt. The deny
and governance gates still run first.

This keeps migration non-destructive: an operator's existing install is
discovered and used as-is, while no unrelated install silently arms browser
auto-approval.

## Approval: ordinary shell commands

Browser commands no longer have a presence-based auto-approve tier. They follow
the same approval ladder as every other shell command and derive trusted patterns
from the real command in `tool_input`, never the model-authored display title.
The first approval can remain one-shot or establish a session-scoped command
pattern; broader trust modes remain deliberate, audited user actions.

The install, browser-download, token, and live-view mutations remain
dashboard-owner-only. Installation mutates the host, the attach token suppresses
the browser extension's own connection prompt, and the view URL controls a live
browser session, so an app token or non-owner dashboard user may not perform
them.

## Accepted limitation: npm is the only distribution channel

The capability rests on a package whose only official distribution is the npm
registry, and that is a real cost this migration accepts rather than solves:

- `@playwright/cli` is a Node program (`#!/usr/bin/env node`, Node 18+), so there
  is no way to drop a self-contained binary on a host. Bundling it would not
  remove that requirement, only the 19 MB download.
- The upstream GitHub release carries **no build assets**, so "download the
  standalone binary" is not an option that exists today.
- `pip install playwright` and the .NET tool install a DIFFERENT product (the
  `playwright` browser-installer / codegen CLI). They are not substitutes.
- yarn / pnpm / bun avoid the npm *client*, not the npm *registry*.

Who this hurts: an operator whose `.npmrc` points at a corporate registry that
does not mirror the package, and anyone with no Node toolchain at all. For the
first, the workaround is a user-prefix install against the public registry with
the binary symlinked onto `PATH` (documented in the `kirocrew-commands` skill,
including the two caveats: the bin dir must be on `PATH`, and overriding the
employer's registry config is the operator's decision). For the second, the panel
now names the remedy and links `nodejs.org` instead of only stating a version
requirement.

What would actually fix it is upstream: portable release archives with a bundled
Node runtime, checksums or Sigstore signatures so enterprises can mirror and
audit them, and OS package-manager entries. That is a reasonable feature request
to file against `microsoft/playwright-cli`, and it is deliberately out of scope
here.

## Snapshot files

The CLI writes a timestamped YAML per command into `.playwright-cli/` and
documents no pruning, so the directory grows without bound.

**Decided:** the gateway service prunes them on a schedule. Pruning belongs to a
long-lived component rather than to the agent, because the agent has no reason
to know the retention policy and a per-command prune would race the daemon.
The directory must therefore live at a path the service knows, not wherever an
agent's cwd happened to be. Snapshots are throwaway state, so retention is by
age and count, and the service must never delete a file the current session
still refers to.

**Decided:** the agent reads snapshot YAML directly with its own file tools. No
read-and-summarize layer. This is the whole point of the migration: the tree is
on disk, the stdout line carries the path, and the agent decides whether it
needs the file at all. A wrapper that read and summarized it would put the tree
back in the context and rebuild the proxy we are deleting.

## Install flow

**Decided: global install only.** `npx` re-resolves through the registry on
every invocation, which is precisely the fragility this migration removes: an
expired registry token would take browsing down again, exactly as it does today.
A global binary is resolved once at install time.

1. Detect: is `playwright-cli` on PATH, and is Node 20 or newer present?
2. If absent, offer the install: `npm install -g @playwright/cli@latest`.
3. `install-browser` for the browser binary (`--with-deps` on Linux). The CLI
   downloads one on first use, but an explicit step gives a progress surface and
   a failure the operator can see.
4. `install --skills agents --global` so the command reference is discoverable
   without occupying the system prompt.
5. Record that the install happened.

Registry auth still applies at install time, which is unavoidable for an npm
package. The improvement is that it applies once at install rather than on every
gateway start.

## Frontend: display and control

`show --port <n> --host 127.0.0.1` serves the dashboard over loopback HTTP, and
that dashboard already provides the session grid with live screencast, a session
detail view with tab bar and navigation, and full remote mouse and keyboard
input. It replaces both halves of what we maintain today: `useBrowserFrame` plus
`screencast.py` for display, and `command_bus.py` for control.

The panel embeds it in an iframe. Three findings must be honoured or this fails
in ways that look like a broken feature:

1. Bind with `--host 127.0.0.1` explicitly. The default listener is IPv6-only
   and an iframe pointed at `127.0.0.1` gets a connection failure.
2. Health-check for any response, not for 200. `/` answers 302.
3. `show --port` blocks, so it is a supervised child process with its own
   lifecycle, not a fire-and-forget call. `show --kill` stops the daemon.

Never pass `--host 0.0.0.0`: it would expose a fully interactive remote-input
browser view to the network.

## Existing installs

An operator on the current design has a `playwright-mcp` entry in
`~/.kiro/settings/mcp.json`, possibly a `KIROCREW_PLAYWRIGHT_CMD` pin, a
`playwright-config.json`, a storage-state file, and an extension token.

What the migration does:

- Removes the canonical `playwright-mcp` entry it owns, matched **by argv** so a
  user's own entry of that name is left alone. Without this an operator who had
  Browser Mode on hits `ModuleNotFoundError` on every kiro-cli session, because
  the entry points at a module this change deletes.

What it deliberately does NOT do, and the consequence to state plainly:

- **No config or storage-state carryover.** `playwright-config.json`'s
  `contextOptions`/`launchOptions` and the storage-state file are orphaned rather
  than translated into the CLI's own config, and the `browser.enabled` flag is not
  read as a consent signal. So an upgrading operator who was browsing before is
  **disarmed until they install the CLI**, and saved logins do not come across:
  they re-authenticate once in the CLI's own profile.
- Why: consent in the new model IS the install (`playwright-cli` on `PATH`), and
  there is no toggle to carry a prior grant into. Translating a config whose
  schema happens to match is cheap, but pointing the CLI at a storage state
  minted by a different browser build is a silent-corruption risk that a
  re-login avoids outright. Carryover is worth revisiting once the CLI's own
  profile format has settled; it is not worth guessing at at cutover.
- The upgrade is therefore **not seamless by design**, and the Settings > Browser
  panel's guided empty state is what an upgrading operator lands on.

## Phases

Each phase is its own PR and leaves the tree working.

1. **Adapter behind the existing surface.** Add the CLI driver and the
   supervised `show` process. No deletions. The dashboard panel switches to the
   iframe. Proves display and control before anything is removed.
2. **Install and consent.** Detection, the install action, the consent record,
   and the migration of an existing install.
3. **Cut over and delete.** Remove the proxy, `browser/`, the MCP registration,
   and their tests. Rewrite `docs/system-specs/modules/browser.md`, the browser
   sections of the agent system prompt, and the `web-browse` / `web-verify` /
   `browser-auth` skills.
4. **Sweep.** The remaining files among the 115 that mention playwright:
   install guides, mcp architecture doc, e2e gate.

## Open decisions

None. All four are settled above: global install only, presence as availability
with approval enforced at the shell boundary, the service prunes snapshots, and
the agent reads snapshot YAML with its own file tools.
