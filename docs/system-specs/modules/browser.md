## Browser Module

Website browsing through `playwright-cli`, the Playwright agent CLI. An agent
drives a browser by running shell commands; Kiro Crew owns the install flow, the
snapshot directory, and the dashboard surface that displays and hands over a live
session.

### Architecture

The browser is a **shell capability, not a tool namespace.** Each browser action
is one `playwright-cli` invocation on the agent's ordinary command path, so there
is no MCP server to register, no tool schemas re-sent per request, and no
per-message browse marker. The agent decides per task whether a browser is
warranted or whether `web_fetch` answers the question.

```
agent turn ──shell──▶ playwright-cli <verb> …
                          │
                          ├─▶ stdout: page URL, page title, path to a snapshot YAML
                          └─▶ disk:   .../page-<timestamp>.yml   (the accessibility tree)

agent reads the YAML with its own file tools ONLY when it needs the tree
```

**The stdout line is the contract.** Every command prints the resulting page URL,
the page title, and a filesystem path to a snapshot YAML. Roughly 250 characters
of stdout carry a complete action result, and the accessibility tree stays on
disk until the agent decides it needs it. This is why no compression layer
exists: a wrapper that read the YAML and summarized it would put the tree back
into the model context, which is the cost the on-disk handoff removes.

**The path printed on stdout is authoritative.** The agent uses the exact path it
was given rather than deriving one, because the snapshot directory is owned by the
gateway (see [Snapshot retention](#snapshot-retention)) and is not the agent's
working directory.

**Element refs are per-snapshot.** A ref such as `[ref=e5]` identifies an element
within the snapshot that produced it, and any page change invalidates it. The
invariant an agent must hold is therefore: after a navigation, a click that
changes the page, or a reload, take a fresh `snapshot` and address elements from
that one. A ref reused across a page change either misses or hits the wrong
element, and the failure is silent in the second case, which is why the rule is
stated as re-snapshot rather than as retry-on-error.

**Sessions are named.** `-s=<name>` selects a session, so several independent
browsing contexts coexist under one CLI install and one agent can keep a
logged-in context separate from a throwaway one.

### Capability model

**Presence of `playwright-cli` on PATH is availability, not approval.** The binary
absent means the capability does not exist; installing it makes the command
available but does not let a shell turn skip the ordinary approval ladder. A
dashboard session must receive an interactive command grant, a trusted-command
pattern, or an explicit trust/auto-approve mode before the command runs without a
prompt.

There is no separate capability toggle or flag file because the CLI exposes no
capability gating of its own: once an approved shell turn runs the binary, all of
its verbs are reachable. That limitation does not turn binary presence into
consent for automatic execution.

#### Approval boundary

An operator's existing working install is discovered and used as-is, but it grants
no silent execution authority. This keeps unrelated user installs from becoming an
agent-controlled browser merely because their launcher is on PATH. It also prevents
an agent from planting a `playwright-cli` shim in a writable PATH directory and
using the presence probe as its own approval.

The first command prompts under normal mode. The operator can approve once, trust
the command pattern for the session, or deliberately enable wider auto-approval.
The last two choices are ordinary audited trust decisions and remain subject to
the deny and governance gates.

### Install flow

Global install only. `npx` re-resolves the package through the npm registry on
every invocation, which makes browsing depend on registry auth at run time: an
expired token takes the capability down mid-session. A global binary resolves
once, at install, so registry auth applies at install time only.

1. Detect `playwright-cli` on PATH and Node.js 20 or newer.
2. Install when absent: `npm install -g @playwright/cli@latest`.
3. `playwright-cli install-browser chromium` for the baseline browser binary.
   The engine argument is required: omitting it installs every engine and lets an
   optional Firefox or WebKit dependency failure veto a working Chromium setup.
   The CLI downloads Chromium on first use regardless, so the explicit step exists
   to give the operator a progress surface and a visible failure rather than a
   stall inside the first browse. `--with-deps` is appended only on an apt host,
   and a refusal there is retried without it — see
   [OS dependencies](#os-dependencies).
4. `playwright-cli install --skills agents --global` so the command reference is
   discoverable from the skill file rather than occupying the system prompt.
   `--skills` accepts `claude` (default) or `agents`; `--global` targets the home
   directory instead of the workspace.
5. Record that the install happened.

### Readiness

`browser_ok` answers "is a build at the revision the installed CLI requires on
disk". A cache directory carries its revision (`chromium-1232`), and
playwright-core launches only the exact revision bound to its own version, so the
match is **exact** on the revision the CLI requires. A prefix match ignores the
revision, and a stale
`chromium-1208` left from before a CLI upgrade then reads as present while the
launch fails `Browser "chromium" is not installed` — and because the gate reads
ready, the panel never offers the download that would fix it. The prefix form also
let `chromium_headless_shell-<rev>` satisfy `chromium`, which is a different
artifact.

The required revision comes from `playwright-core/browsers.json`, the file
`install-browser` itself consults. Reading it is a plain file read, so readiness
stays subprocess-free.

**The manifest is attributed to a `@playwright/cli` package, never searched for.**
Resolution anchors on that package — the hoisted sibling in the same
`node_modules`, a copy nested under the package, or the standalone installer's
known prefix (`KIROCREW_PLAYWRIGHT_CLI_HOME`, else `<data home>/playwright-cli`).
Anchoring is a correctness property, not an optimization: walking ancestors
instead passes through `$HOME` on the standalone layout, where one unrelated
`~/node_modules/playwright-core` supplies a revision from a **different** install.
That reports a working browser broken and keeps doing so after the offered
download, because the gate goes on reading the foreign file. The standalone prefix
is probed by path because that installer generates a **wrapper script** rather
than a symlink, so its package tree is not an ancestor of the launcher at all.

When no manifest can be attributed, the revision is unknown and readiness falls
back to the older presence-only answer. Absent metadata is an unknown, not
evidence of a stale cache, so it must not turn a working browser into a reported
broken one.

### Command surface

The full reference lives in the skill the CLI installs in step 4, which is why the
system prompt states only the loop and the ref rule. The verbs:

| Group | Commands |
|---|---|
| Lifecycle | `open [url]`, `goto`, `close`, `attach --extension` |
| Pointer and form | `click <ref>`, `dblclick`, `fill <ref> <text>`, `type <text>`, `select`, `check`, `uncheck`, `hover`, `drag`, `upload` |
| Read | `snapshot`, `screenshot [ref]`, `pdf`, `eval`, `console`, `network` |
| Navigation | `go-back`, `go-forward`, `reload`, `press <key>`, `resize` |
| Dialogs | `dialog-accept`, `dialog-dismiss` |
| Tabs | `tab-list`, `tab-new`, `tab-select`, `tab-close` |
| State | `state-save [file]`, `state-load <file>`, `cookie-list`/`get`/`set`/`delete`/`clear`, `localstorage-*`, `sessionstorage-*` |
| Capture and scripting | `route`, `run-code`, `tracing-start`/`stop`, `video-start`/`stop` |
| Host | `show`, `install --skills`, `install-browser`, `config-print` |

Sessions are selected with `-s=<name>` on any command.

### Generated session reachability

Each agent process receives a generated `PLAYWRIGHT_CLI_SESSION`
(`kc-<random>`). For generated sessions only, `PWTEST_SOCKETS_DIR` and
`PWTEST_DAEMON_SESSION_DIR` point at separate short namespaces under
`<data-home>/pw/<8hex>/s` and `/d`. A `playwright-cli list` in one agent
therefore cannot enumerate peer chat families. Operator-configured PWTEST roots
are treated as base directories and receive the same generated-session
namespace. Relative configured roots are rejected because the gateway and agent
working directories can differ. Operator-provided non-`kc-` session names
preserve their complete existing Playwright environment and are not redirected.

Keeping both locations outside scratch means a daemon remains reachable after
its agent process or scratch directory is gone. Operator cleanup supplies the
generated session's `/s` and `/d` paths with the corresponding PWTEST variables
and then uses the ordinary `playwright-cli -s=<name> close` protocol. Kiro Crew
never executes the CLI or connects to the socket on its own: a stray has no
registry entry to resolve and its socket is unlinked by the first refused
connect, so a registry-driven `close` reclaims nothing.

### Stranded daemon reclamation

Reachability alone does not reclaim a daemon whose agent died, so the orphan
sweep (`session_pid`) carries a browser-daemon class alongside its MCP,
gatewayd and work classes. playwright-core spawns the daemon as `node
<...>/entry/cliDaemon.js <session-name>` with `detached: true` and no `env`
override, which decides both halves of the identity. Detachment makes it its
own session and process-group leader, so it is invisible to the teardown child
snapshot, to `kill_process_tree`, and to the SID ownership test the work class
uses. Inheriting the environment verbatim puts the generated
`PLAYWRIGHT_CLI_SESSION` and `KIROCREW_SPAWNED` in its exec-time environ, which
the kernel holds immutable after exec.

A daemon is reclaimed only when all of these hold: a structural cliDaemon argv
whose following element is a generated `kc-<8hex>` name; that same name in its
exec-time environ; the `KIROCREW_SPAWNED` marker; no live process outside the
daemon's own SID still holding that session; and an age past the work-class
floor. Ownership is therefore proven from kernel facts alone — argv, exec-time
environ, session id, process liveness — never from filesystem state a same-UID
agent could write, which is what made earlier reaper attempts unsafe. The probe
scans the whole process table rather than a manager-local set, so a peer gateway
sharing this data home sees and protects its own live sessions. An
operator-named session is structurally excluded and never signalled; the `kc-`
prefix is reserved so the two populations cannot be confused. Every stage fails
closed: non-Linux, an unreadable `/proc`, and an inconclusive per-process read
all read as "owner alive". The kill signals the process GROUP so the Chromium
tree goes with the supervisor, TERM first for a clean profile flush, only for a
genuine isolated group leader, with identity re-verified before escalating to
SIGKILL and the result SEL-audited.

Deliberately not keyed on the socket path the way the gatewayd class is:
`Session._connect` unlinks the socket whenever a connect fails, so an absent
socket records a refused connect rather than an unreachable daemon, and the
daemon holds its listening descriptor either way.

The generated socket root is rejected when its worst-case AF_UNIX path exceeds
the upstream 103-byte budget. Before either variable is injected, the installed
`@playwright/cli` package resolved from the active launcher is checked through
the same package anchor as browser revision detection (a stale standalone
fallback is never accepted), and its serving `playwright-core` sources must
contain both hooks on their execution paths (`process.env.PWTEST_SOCKETS_DIR ||`
and `process.env.PWTEST_DAEMON_SESSION_DIR`). A future upstream
rename/removal therefore logs a warning and fails back instead of silently
returning sockets to scratch. The source verdict is cached by path, mtime, and
size so an upgrade invalidates it.

Kiro-Crew-owned lifecycle directories are created
owner-only and then restricted with the fail-loud platform helper before their
environment variables are exported. A crash can leave a small per-session
registry/socket namespace behind. Reclaiming the daemon does not delete that
namespace: it is two empty directories, and pruning them would need its own
liveness argument for no memory benefit.

### Auth

Two paths, chosen by whose browser holds the session.

**Saved state.** `state-save [file]` writes the current context's cookies and
storage to a file, and `state-load <file>` restores it into a session. A logged-in
context is therefore reusable across sessions and across gateway restarts without
re-authenticating. `cookie-list`/`get`/`set`/`delete`/`clear` and the
`localstorage-*` / `sessionstorage-*` families operate on individual entries when
a whole-state round trip is heavier than the task needs.

**Attach.** `attach --extension` connects to the operator's own running Chrome,
which already holds their logins, so no state file is involved. This is the
stronger capability of the two: the sessions are the operator's real ones, which
is why the [approval boundary](#approval-boundary) above is mandatory.

State files hold live session credentials and are written with owner-only
permissions.

**The attach token.** `attach --extension` works without one: the extension
answers a tokenless handshake by asking the human to approve the connection in the
browser. Setting `PLAYWRIGHT_MCP_EXTENSION_TOKEN` removes that one click and
nothing else, so it is opt-in and absent by default. `browser_cli/token.py` stores
it owner-only behind `security._CREW_SECRET_LEAVES` — the agent inherits it through
the environment and can never open the file — and no status surface returns the
value, only whether one exists.

The extension presents the token as a shell assignment, so the settings field
accepts either form and stores the same token:

```
PLAYWRIGHT_MCP_EXTENSION_TOKEN=<value>
<value>
```

`normalize_paste` strips the prefix only when the text left of the **first** `=`
is exactly the variable name. That condition is a safety property rather than a
nicety: these tokens are base64url and can legitimately contain `=`, so a looser
rule would corrupt a bare token. `export`/`set` keywords and a matched pair of
surrounding quotes are removed for the same reason. Normalization also runs on
read, so a stored value holding the whole assignment repairs itself instead of
reporting "stored" while the extension keeps prompting. Clearing ignores an
already-absent file, but any other unlink failure propagates through the API so
the settings panel cannot report success while the credential remains active.

### Launch config

Kiro Crew installs, gates on, and offers downloads for **Chromium**:
`install-browser` fetches the Chromium build, `browser_ok` is
`browsers_present()["chromium"]`, and `attach --extension` supports that family
alone. The CLI's own default is a different browser — the branded Chrome
*channel*, an OS-level install at a path like `/opt/google/chrome/chrome` that
Kiro Crew never provisions and cannot install without root. So on a host that did
everything the product asked, the first browse fails with

```
Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome
```

while every readiness signal is honestly green, because the Chromium build really
is downloaded. `browser_cli/launch.py` closes that gap by naming the engine.

**Why a config file rather than a flag or a browser env var.** All three exist and
only the file works for a whole session:

| Mechanism | Why it cannot carry this |
|---|---|
| `--browser` | takes `chrome, firefox, webkit, msedge` — `chromium` is not accepted |
| `PLAYWRIGHT_MCP_BROWSER` | the same four values, so it cannot name the installed engine either |
| `--config` | accepted only on the session-establishing commands (`open`, `attach`) and rejected by the follow-up commands that make up most of a session |
| `PLAYWRIGHT_MCP_CONFIG` | names a config **file** and applies to every invocation uniformly |

The last one is the mechanism used, and for the same reason as
[snapshot retention](#snapshot-retention): the agent runs the CLI as a shell
command, so an inherited environment variable is the only channel that reaches an
invocation Kiro Crew never constructs. The config is written under the data home
at a fixed absolute path, independent of whichever working directory a turn ran in.

The schema is **nested** under a `browser` key — `{"browser": {"browserName":
"chromium"}}`. A flat top-level `browserName` parses without error and selects
nothing, which presents as the branded-Chrome failure above rather than as a
config error.

**The generated config names the engine and nothing else.** Every added key
becomes a default an operator must discover in order to override, and the engine
is the only one the install flow already decided.

**The browser sandbox is deliberately untouched.** Chromium's sandbox is a
security boundary, so no generated default removes it. A host that cannot run it —
a container lacking the kernel permissions, where the failure is
`No usable sandbox!` — needs an operator decision rather than a default that
quietly drops the boundary for every host. That is what the escape hatch is for:
when `PLAYWRIGHT_MCP_CONFIG` is **already set** in the environment, Kiro Crew adds
nothing and the operator's file wins entirely. Naming a config is how an operator
selects a different engine, pins an `executablePath`, or accepts the sandbox
trade-off on a host that requires it.

### Snapshot retention

The CLI writes one timestamped YAML per command and documents no pruning, so the
directory grows without bound.

**The gateway service prunes it on a schedule.** Retention belongs to a
long-lived component rather than to the agent for two reasons: the agent has no
reason to know the policy, and a per-command prune would race the daemon.
Snapshots are throwaway state, so retention is by age and count. The service
never deletes a file the current session still refers to, because the path on
stdout is the agent's only handle to the tree.

This is also why the snapshot directory is at a fixed path the service owns
rather than relative to whatever working directory an agent happened to have.

### Dashboard integration

`playwright-cli show --port <n> --host 127.0.0.1` serves the CLI's own dashboard
over loopback HTTP, and the panel embeds that in an iframe. The port is
OS-assigned by default; `dashboard.browser_view_port` pins the public port, for
remote-gateway deployments where the viewer reaches loopback through an SSH
tunnel that forwards a fixed set of ports. The pin is never handed to the
child: the supervisor claims the pinned port itself with a bound listener it
keeps holding, an atomic ownership proof that makes the deterministic,
operator-named port race-free, and relays byte-for-byte to the child's own
ephemeral port. The child's OS-assigned port keeps the unpinned path's
advisory bind window (unpredictable, loopback-local); both bind loopback only. The served dashboard
provides the session grid with live screencast, a session detail view with tab bar
and navigation controls, and full remote mouse and keyboard input, so a human can
take over a session directly: this is the path for a CAPTCHA or a 2FA prompt that
an agent cannot and should not complete. Escape releases input capture.

Three properties of the server must be honoured, because each failure mode
presents as a broken panel rather than as a misconfiguration:

1. **Bind `--host 127.0.0.1` explicitly.** The default listener is IPv6-only, and
   an iframe pointed at `127.0.0.1` gets a connection failure against it.
2. **Health-check for any response, not for 200.** The root path answers 302.
3. **Treat `show` as a supervised child process.** It blocks, so it needs an
   owned lifecycle rather than a fire-and-forget call. `show --kill` stops the
   daemon.

**Never pass `--host 0.0.0.0`.** The served dashboard carries full remote input on
a browser that may hold the operator's sessions, so binding it off loopback
exposes an interactive takeover surface to the network.

### Security

| Control | Implementation |
|---------|----------------|
| Capability availability | Presence of `playwright-cli` on PATH; see [Capability model](#capability-model) for why this is not approval |
| Dashboard exposure | `show` is bound to `127.0.0.1`; `0.0.0.0` is never passed, because the served view carries remote input |
| Saved state files | Owner-only permissions; they hold live session credentials |
| Launch config | Write-protected from the agent on both the file-edit and shell gates, and readable. Deliberately anchored rather than bare-token: the filename is not itself the grant, since the agent can name its own `PLAYWRIGHT_MCP_CONFIG` — so what the entry removes is the durable form (rewriting the config the product installed), and a `cd`-relative write is the accepted residual, exactly as for `.data-home-ready` |
| Page content | Treated as untrusted input. A URL, instruction, or form target read off a page never decides the next navigation |
| Attach mode | Operates the operator's real logged-in browser, so it is the strongest form of the capability and remains behind shell approval |
| Approval | Every `playwright-cli` shell command follows the ordinary approval ladder. Presence alone never auto-approves it; only an explicit trusted pattern, session trust, or auto-approve grant can skip the prompt |

### Platform notes

| Requirement | Detail |
|---|---|
| Node.js | 20 or newer |
| Install | `npm install -g @playwright/cli@latest` |
| Browser binary | `install-browser`; `--with-deps` on an apt host only |
| Attach | Chromium-family only, since Playwright ships an attach extension for that family alone |

### OS dependencies

Playwright's `--with-deps` implementation is **apt-only**. On a distribution it
does not recognize it does not decline — it selects its nearest Ubuntu package
set and runs `apt-get` as root anyway. On an rpm host that is wrong twice: the
package names do not exist, and the command needs a privilege a managed
workstation withholds. Because the flag and the browser download are one CLI
invocation, that refusal also took the download down, which is what made a
missing OS library present as a sudo policy error quoting a 60-package `apt-get`
line the user never typed.

`browser_cli/os_deps.py` resolves the host family from `/etc/os-release`
(`ID` plus `ID_LIKE`, so derivatives resolve through their base) and the browser
step adapts:

| Family | `--with-deps` | On failure |
|---|---|---|
| debian / ubuntu | passed | retried without the flag, so the download still lands |
| rpm (rhel, fedora, centos, amzn, rocky, alma, suse) | never passed | failure detail carries a `sudo dnf install` line naming the rpm packages |
| unrecognized Linux | never passed | no remedy offered — a guessed package manager fails on its own first argument and reads as the product being broken |
| macOS / Windows | not applicable | the browser download alone is sufficient |

The remedy is a command for a human to run, appended to the failing step's
`stderr` (which the settings panel already renders verbatim) rather than a new UI
state. Nothing in this path elevates or runs a package manager. The rpm list
covers Chromium alone: it is the engine `attach` supports and the one `browser_ok`
gates on, so it is what "browsing works" means.

**A zero exit is not a verdict.** MEASURED on Amazon Linux 2023: with libraries
missing, `install-browser` prints

```
Playwright Host validation warning:
║ Host system is missing dependencies to run browsers. ║
```

and **exits 0**, leaving the browser directories in the cache. Playwright
classifies it as a warning. Reading the exit code alone therefore reports a
browser that cannot launch as installed — the panel goes green, `browser_ok`
turns true because the build is genuinely on disk, and the real error arrives at
the user's first browse as an opaque stack trace. Every browser step is judged on
its output as well as its exit code (`os_deps.host_deps_unsatisfied`, matched
against the header and the message body so a reworded box still trips one), and a
match fails the step and carries the remedy.

`browser_ok` keeps meaning "a build is downloaded", which stays literally true on
such a host; the install error is what carries the truth that it cannot run.
Making `browser_ok` mean "and it can launch" would need a validation probe on
every settings poll.

### Standalone enterprise installer

`playwright-cli.sh` (macOS/Linux) and `playwright-cli.ps1` (Windows) install the
same `@playwright/cli` package as the install flow above, for the case that flow
cannot handle: a machine where `npm install -g` does not work. They are run by a
human at a shell, not by the gateway, and nothing in the product invokes them.

They exist because step 2 of the install flow assumes two things an enterprise
laptop often lacks — a Node toolchain of a recent enough major, and a default
registry that answers without a login. When either is missing, a bare
`npm install -g` fails with npm's own output, which does not distinguish "your
token expired" from "the registry is firewalled" from "this mirror does not carry
the package", and those three have mutually exclusive remedies. The scripts remove
both assumptions without introducing a private artifact channel: there is no Kiro
Crew-hosted Playwright build to keep in sync or to trust.

**Node is bootstrapped, not required.** A Node already on PATH is reused when its
major is at least the floor the install flow above requires, as is one recorded by
`ensure-node.sh` in `<data home>/node-bin-dir` — these installers *read* that
marker but never write it, so the sharing is one-directional: a Node they
bootstrap stays private to them, and `ensure-node.sh` still downloads its own.
That is deliberate, because `env.py` hands the marked interpreter to the gateway,
whose floor is higher again.

A reused Node is only reused if `npm` is actually beside it. On Debian and Ubuntu
`nodejs` and `npm` are separate packages, so `apt install nodejs` alone leaves a
perfectly good Node with no npm — and telling that user to install npm would hand
back the one prerequisite these installers exist to remove. Such a Node is
abandoned and a private one bootstrapped instead, because the release tarball
bundles npm. Missing npm in a tree the installer itself unpacked is a different
thing entirely — a truncated archive — and aborts rather than retrying.

Otherwise the release build for the detected platform is downloaded and its
SHA-256 checked against that release's `SHASUMS256.txt` **before it is
executed**; a mismatch, or an artifact the manifest does not list at all, aborts
the install. Selection is libc-aware because an official tarball is not portable:
musl hosts (Alpine) get the unofficial-builds variant, and so do pre-2.28-glibc
hosts (RHEL 7-era) **on x64 only**, which is the only architecture that variant
is published for. The manifest is fetched over the same channel as the artifact
and is not itself signed — identical to `ensure-node.sh`, so this is corruption
detection plus transport trust, not an independent trust root like the signed
manifest `cli.sh` verifies.

**The install is unprivileged and self-contained**, which is where it diverges
from the install flow above: `npm install --global` is run with
`npm_config_prefix` pointed at `<data home>/playwright-cli`, so nothing is written
outside the user's home and sudo is never involved. The generated entry point is a
**wrapper script, not a symlink**: npm's own shim starts `#!/usr/bin/env node`,
which resolves against the *caller's* PATH, so a user whose Node the installer had
to bootstrap would get `node: not found` from a tool that installed perfectly. The
wrapper pins the exact interpreter that was verified, and every path interpolated
into it is escaped, because a generated script treats its inputs as code.

**The public registry is pinned.** An ambient `.npmrc` that redirects the default
registry at a private mirror makes a *public* package 401 the moment that mirror's
token expires. `--registry` re-points it for the opposite case (public registry
firewalled, mirror reachable), and `--isolated-npmrc` ignores the ambient config
entirely. A registry URL carrying a credential — in userinfo or in a query
parameter — is redacted everywhere the scripts print it, and the log is created
owner-only, because npm writes that URL into its own output.

**A credential may not be passed as a flag.** `/proc/<pid>/cmdline` is
world-readable, so `--registry https://user:token@host/` publishes the token to
every account on the machine for as long as the install runs, and leaves it in shell
history besides — neither of which redaction can reach, since redaction covers only
what the scripts print. The credential travels in the environment instead
(`KIROCREW_NPM_REGISTRY`, `PLAYWRIGHT_DOWNLOAD_HOST`), where `/proc/<pid>/environ` is
readable only by its owner, or through `npm login`. The refusal keys on PROVENANCE
rather than content: the resolved registry value also holds an env-supplied
credential, and refusing that would break the escape the error message recommends.

**Enterprise failures are classified, not passed through.** npm's output is kept
at `<prefix>/playwright-cli-install.log` — namespaced because a caller-supplied
prefix could otherwise make that a generic name the installer truncates — and matched against the failures a corporate network
actually produces. The browser binary is fetched during the install rather than
left to first use, for the same reason: it comes from the Playwright CDN and not
the npm registry, so a network that permits one may block the other, and doing it
here turns that into exit 16 with a mirror remedy instead of a stall inside the
user's first browse. `--with-deps` is deliberately not passed — it installs OS
packages through the system package manager, and this installer never elevates.
The full exit-code table is in `--help`; the codes that carry a diagnosis are 13
(registry rejected auth), 14 (registry unreachable), 15 (package or version
absent) and 16 (browser download blocked).

### Related

- [web-browse](../../../src/kiro_crew/builtin_skills/web-browse/SKILL.md) for
  opening a page so the user can see it.
- [web-verify](../../../src/kiro_crew/builtin_skills/web-verify/SKILL.md) for
  screenshotting a front-end change as evidence.
- [mcp](../../architecture/mcp.md) for why browsing is deliberately not an MCP
  server.
