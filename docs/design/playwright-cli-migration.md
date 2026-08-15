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

## Consent model

The current capability gate is tool presence: with Browser Mode off the
`browser_*` tools are absent, so the capability does not exist for the model.
That mechanism does not survive the migration, and no CLI feature replaces it:
capabilities cannot be gated, and a binary on PATH is reachable from any shell
turn.

**Installation therefore becomes the gate.** Not present means the capability
does not exist; installing it is the operator's act of granting it. This is the
only coherent model available, not a preference.

**Decided:** an install Kiro Crew performs is consent by construction, the
operator's existing working setup is treated as consent so a migration never
silently disarms them, and **presence of `playwright-cli` on PATH is consent**
whether or not Kiro Crew installed it.

### Accepted risk

Presence-as-consent has one hole, accepted deliberately rather than overlooked.
An operator who installed `playwright-cli` for their own work has granted nothing,
yet the capability is armed. It cannot be narrowed after the fact:

- The CLI has no capability gating (verbatim: "In the CLI all capabilities are
  always available -- there's no gating"), so there is no subset to grant.
- `attach --extension` connects to the operator's own running Chrome, which
  carries their live logged-in sessions.
- A binary on PATH is reachable from any shell turn, so nothing in the tool
  surface can express the restriction.

The exposure is therefore: on such a host, an agent turn can drive a browser
holding the operator's authenticated sessions without the operator having said
yes to that. This is the price of removing the toggle, and the toggle is what
produced the defect class this migration retires.

Two mitigations are cheap and do NOT reintroduce a gate, and should ship with
Phase 2:

- State it in the docs and in the Settings surface, so presence-as-consent is
  discoverable rather than a surprise found by reading code.
- Make browser use visible after the fact. The dashboard panel showing a live
  session is itself the disclosure, and it is already in Phase 1.

## Approval: which commands run without a prompt

A browsing step is a shell command now, so without an allow path every `open`,
`click` and `snapshot` raises an approval prompt and browsing is unusable. The
allow path is deliberately NOT the bundled `auto_approve_tools` list, because
that list is matched against the tool *title*, which the model writes: an
injected agent could title `rm -rf /` as "playwright-cli snapshot" and be
auto-approved. Instead the check sits on the same command-keyed path the user's
own "always allow" grants use, and reads the real command out of `tool_input`.

The boundary is the page. A verb whose whole effect lands inside the browser
page or session is auto-approved; a verb that reaches the local machine is not:

| Kept interactive | Why |
|---|---|
| `eval`, `run-code` | run attacker-authored code in an authenticated page; with `fetch()` that is a complete exfiltration path |
| `upload` | sends an arbitrary LOCAL file to the current page |
| `state-load` | reads an arbitrary local path and injects its cookies into the live session |
| `state-save <path>`, `video-start <path>` | bare, these write inside the service's output dir; with an argument they are an arbitrary-path write |
| `install`, `install-browser` | mutate the machine; installing is the dashboard's job, not the agent's |
| `requests`, `network` | a URL can BE the credential (presigned S3 URL, magic-link); listing URLs prints a credential into context the same way `cookie-list` does |
| `delete-data` | with `attach`, destroys operator session state (cookies, storage, cache) nothing recovers |
| `cookie-set`/`-delete`/`-clear`, `localstorage-set`/`-delete`/`-clear`, `sessionstorage-set`/`-delete`/`-clear` | with `attach`, the -set verbs are session fixation (inject a controlled credential the attacker can reuse); the -delete/-clear verbs destroy operator login state nothing recovers |
| `route`, `unroute`, `network-state-set` | a route intercepts requests and returns forged responses — the agent reads the page via `snapshot`, so a route lets an injected agent control what the NEXT read returns. `unroute` removes a route the operator set intentionally. `network-state-set` toggles offline mode (denial-of-service on the operator's browsing) |
| `config-print` | prints the session's launch configuration, and the documented way to constrain this browser is a proxy set through `launchOptions.proxy.server` — whose value carries the proxy credential. The verb that reads as a harmless settings dump prints a secret on exactly the setup this design recommends |
| `close`, `tab-close`, `close-all`, `kill-all` | with `attach` these are the operator's OWN window, tabs and logins; closing them loses unsaved work and nothing recovers it, and `close-all`/`kill-all` do it to every session at once. The agent prompt already tells the model never to close an attached browser, but prose the model is asked to honor is advice rather than a control, and the gate cannot see whether a session is attached or CLI-owned, so it fails closed. `detach` stays approved and is what cleanup needs: it releases the session and leaves the window alone |

Three properties make this hold up rather than merely look careful:

Read the list below as a record of a gate that had to be narrowed five times,
not as a design that arrived correct. Each entry after the first two was found
by review, and each was a dimension the previous version had not considered:
shell redirection, then non-shell tools carrying a `command`, then verbs whose
return VALUE is the credential, then a positional argument that is a local file.
The pattern is what matters for anyone extending this: a new dimension is the
expected case, so the structure fails closed on anything unrecognized, and a new
CLI verb or flag stays denied until someone lists it deliberately.

- **Allowlists, not denylists**, for both verbs and flags. A verb added by a
  future CLI release is denied until someone reviews and lists it.
- **Flags are checked too.** The official docs use `screenshot --filename=<path>`,
  so a local path can arrive as a flag rather than a positional argument.
  Skipping unrecognized flags on the way to the verb would auto-approve an
  arbitrary local write; an unknown flag therefore denies the whole command.
- **Positionals are checked too, not just flags.** A page verb whose arguments go
  unread falls through to "approved" on the verb alone. That makes
  `goto file:///<path>` auto-approved, which is not a page action: the file lands
  in the page and the next `snapshot` prints it into the agent's context, i.e. an
  arbitrary local file read behind the one gate whose job is to refuse those. A
  URI-shaped argument passes only as plain http(s), and only to a host that is
  neither a local control plane nor link-local.
- **No local control plane is auto-navigable.** Kiro Crew's own dashboard is
  served over loopback, and the approval mode, trust settings and YOLO switch all
  live on it -- so auto-approved navigation plus auto-approved clicks is a path
  from "browsing is allowed" to "the agent widened its own ceiling", with no
  human in the loop. The repo already refuses computer-use on its own dashboard
  for exactly this reason. The rule is therefore the whole loopback range and the
  loopback names (`localhost`, the reserved `.localhost` suffix) plus the
  unspecified address, not one port number: a pod's dashboard port is only known
  at runtime, and one class rule also covers whatever else the operator runs
  locally -- another admin UI, a notebook server. The cost is one approval prompt
  when previewing a local dev server, in a scenario where the operator is already
  watching; public http(s) browsing is untouched.
- **No non-globally-routable address is auto-navigable.** Private (RFC 1918:
  10/8, 172.16/12, 192.168/16), CGNAT/shared (100.64/10), multicast, reserved,
  documentation, and benchmarking ranges are all refused. A `goto
  http://10.0.0.5/admin` followed by an auto-approved `snapshot` prints internal
  infrastructure responses into the agent's context -- the same SSRF vector as
  link-local, aimed at internal services rather than the metadata endpoint.
  Ranges are tested by `ipaddress`' own `is_global` property (True only for
  globally-routable addresses), applied to any embedded IPv4 as well as its
  wrapper. `is_global` subsumes loopback, link-local, unspecified, private,
  CGNAT/shared, multicast, reserved, documentation, and benchmarking ranges
  in one predicate without hand-rolled CIDRs.
- **DNS names are NOT resolved.** Resolving inside the approval predicate is a
  blocking network call on the hot path AND a DNS-rebinding TOCTOU: a name can
  answer a public address at approval time then resolve to a private one when the
  browser re-resolves milliseconds later. The residual risk -- a public name
  pointing at a private address -- is accepted; browser-side network policy is
  the correct mitigation layer for that class.
- **A host must be canonical to be classified.** Non-globally-routable
  addresses are refused through `ipaddress`' `is_global` property rather than
  against hard-coded addresses. That alone is not enough:
  `ipaddress.ip_address("2852039166")` *raises*, so treating an unparseable
  host as "a DNS name" hands back 169.254.169.254 through its decimal, hex,
  octal-dotted and short forms, and an IPv6 wrapper's `is_global` may not see
  through an IPv4 embedding on all versions. A host is accepted as a name only
  when its final label starts with a letter -- which refuses every numeric
  spelling without enumerating them -- and an address that embeds an IPv4 one
  is tested through the embedding as well as the wrapper. Anything
  unclassifiable costs one prompt.
- **The approval layer checks a URL; it cannot bind a DESTINATION.** This is a
  structural limit, not an omission, and it is accepted deliberately. The host
  rules above run on the URL handed to `goto`. The browser then resolves that
  name and follows redirects on its own, so a public URL answering
  `302 Location: http://127.0.0.1:5476/` reaches the dashboard anyway, and a name
  that resolves public at check time can resolve private at connect time. No
  amount of parsing closes that: the check and the connection are different
  events, and re-resolving inside the predicate would add a blocking network call
  to the approval path while still losing the race.

  What closes it is the layer that acts on the RESOLVED ADDRESS at connection
  time. Chrome's Local Network Access does exactly that: it classifies by
  destination address rather than by the initiating page's URL, covers redirect
  hops, and is enforced by default from Chrome 142. Because it gates on a
  permission and an automated browser has no user to grant one, the automated
  case fails closed — which is the behaviour we want. Treat this as the primary
  mitigation, with the caveat that it is read from Chrome's documentation and
  issue tracker rather than measured here, and that it depends on the Chrome
  version the CLI installs.

  An operator wanting belt-and-braces, or running an older Chrome, can pass a PAC
  file through the CLI's own `browser.launchOptions.args`
  (`--proxy-pac-url`): PAC is re-evaluated per redirect hop and `isInNet()`
  compares a literal IP directly, so it refuses a redirect to `127.0.0.1` without
  needing name resolution. It costs their own loopback dev-server previews unless
  they carve out a port, and it retains a DNS-rebinding window, because Chromium
  keeps the full resolved address list and may try an address the PAC decision did
  not see.

  Two controls are specifically NOT the answer here, both worth naming so nobody
  reaches for them later. `--host-resolver-rules` acts on name resolution, and a
  literal-IP redirect never resolves a name, so it cannot see this case at all.
  And the CLI's own `network.allowedOrigins` / `blockedOrigins` are documented
  upstream as not a security boundary and as not affecting redirects — using them
  here would look like a mitigation while changing nothing about the threat.
- **One splitter, and it must honor escapes.** The segment split (which rejects
  command substitution and requires every segment of a chained command to pass) is
  shared with the existing trusted-pattern path rather than written a second time,
  because a second shell splitter is how a bypass gets introduced. Sharing it also
  means its quote tracking is load-bearing for every approval path: a closing
  quote followed by `\'` leaves quoted context, so the separator after it is real,
  and masking it collapses a two-command line into one approved segment.
- **Redirections are refused.** A redirection is the SHELL's work, so
  `playwright-cli snapshot > file` creates or truncates that file before the
  approved command runs, and the verb allowlist cannot see it: `>` and the path
  arrive as ordinary tokens. The check is quote-aware, because `click "div >
  span"` is a legitimate selector. This was found by review, not by design --
  the first version approved it.
- **Only a shell tool reaches this path.** `_extract_bash_command` reads a
  `command` field out of ANY tool input, so without an `is_shell` gate a
  non-shell tool that carries one (`cron_add`, which can schedule a shell
  command) would be auto-approved -- turning "browsing is allowed" into
  "creating a durable scheduled job is allowed".

The install and token endpoints refuse **app tokens** (403 + SEL), because route
scoping is not capability scoping: an app listing `/api/browser` in its manifest
would otherwise be able to install the binary that arms auto-approval, or replace
the attach token that silences the browser's own per-attach prompt.

Every auto-approval is recorded in the security event log with
`reason: "browser_cli"`, so the decision is auditable after the fact and is
distinguishable from a grant the user made themselves.

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

None. All four are settled above: global install only, presence as consent (with
the accepted risk recorded), the service prunes snapshots, and the agent reads
snapshot YAML with its own file tools.
