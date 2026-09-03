# CLI Module

## Overview

The CLI module (`kiro_crew/cli.py`) provides the `kirocrew` command using stdlib `argparse`.

## Import Weight Contract

`cli.py` is the shared dispatcher for every subcommand — including the
long-lived MCP stdio servers (`kirocrew mcp-core` / `mcp-cron` /
`mcp-computer`), which hold its module-scope imports resident for their whole
lifetime. Its module scope therefore stays light: `cli_commands`,
`cli_server` (which pulls `slack.gateway`) and `dashboard.state` (which pulls
`vector_memory` → `numpy`) are imported inside the one `main()` dispatch
branch that uses each name, never at module scope. Deferring them cuts a
fresh `import kiro_crew.cli` from ~1.3 s / ~112 MB to ~0.5 s / ~54 MB, paid
per CLI invocation and per MCP backend process.
`test/test_cli_lazy_imports.py` ratchets the contract: after
`import kiro_crew.cli` in a fresh interpreter, none of those modules may be
present in `sys.modules`, and every deferred dispatch import must resolve.

The entry point itself is not negotiable: all invocation forms
(`kirocrew <sub>`, `python -m kiro_crew <sub>`, and the frozen desktop
binary) land in `cli.main()`, whose prelude runs `boot_platform()`
(fail-closed for non-standalone profiles), sandbox env hygiene, and
`KIROCREW_PROJECT_DIR` resolution before any dispatch.

## Source Checkout Launcher

The POSIX wrapper at `bin/kirocrew` resolves symlinks to find the real checkout,
sets `KIROCREW_PROJECT_DIR` to that checkout unless the caller already supplied
one, and delegates every argument to `.venv/bin/kirocrew`. The virtualenv entry
point comes from the editable install created by the setup scripts, so it makes
`src/kiro_crew` importable without adding the source tree to `PYTHONPATH`. Any
caller-provided `PYTHONPATH` is inherited unchanged.

If `.venv/bin/kirocrew` is unavailable, the wrapper exits with source-install
guidance instead of falling through to a different Python environment.

## Standalone Wheel Installer Trust Contract

`cli.sh` installs channel or pinned-version wheels only from an authenticated
manifest. This distribution trust boundary is independent of the runtime CLI
and of macOS signing/notarization.

- Schema: `kirocrew-cli-artifact-manifest-v1`.
- Algorithm: `RSASSA_PKCS1_V1_5_SHA_256`.
- Key identity: `sha256:` plus the lowercase SHA-256 digest of the public
  SubjectPublicKeyInfo DER bytes.
- Signed fields: `algorithm`, `channel`, `key_id`, `pub_date`,
  `python_requires`, `schema`, `sha256`, `version`, and `wheel_url`.
- Optional signed field: `min_version` — the forced-update floor for a
  breaking release, declared in `packaging/MIN_VERSION` at publish time. Must
  be a bare release (`0.6.0`, no prerelease suffix) and must not exceed the
  manifest's own `version`; both are enforced by
  `packaging/signing/cli-manifest.py` before signing. The installer verifies
  its format but does not act on it (it always installs the signed version);
  running gateways read it from the channel feed and mark the update
  REQUIRED when their own version sits below the floor — but only after
  `platform/feed_trust.py` verifies the manifest signature against the same
  pinned key, because the floor coerces the dashboard UI and an unverified
  one must degrade to the ordinary dismissible prompt. Absent means no
  floor, and the canonical payload omits the key entirely so no-floor
  manifests stay byte-identical to the pre-floor format.
- Signature field: base64 RSA signature over sorted, compact UTF-8 JSON of all
  signed fields; `signature` itself is excluded.
- Channel source: `feed/<channel>/latest-cli.json`. Pinned-version source:
  `cli/<channel>/<version>/cli-manifest.json`; pinned installs do not resolve
  through the mutable channel feed.

The installer embeds the public key and expected key id. Before any network
request, it requires OpenSSL, rejects an unconfigured pin, materializes the key,
and verifies its DER fingerprint. It then applies bounded input/object sizes,
duplicate-key and exact-field-set rejection, printable-ASCII/string checks,
canonical URL and digest validation, requested channel/version matching, and
pinned-key signature verification. Artifact fields are not consumed and wheel
bytes are not fetched until the signature succeeds. The downloaded wheel must
then match the authenticated SHA-256 digest before `pipx install` runs.

Any unavailable trust root, malformed or unsigned legacy feed, unknown field,
wrong schema/algorithm/key id, signature failure, metadata mismatch, network
failure, or wheel digest mismatch terminates installation. There is no unsigned,
`SHA256SUMS`, or trust-on-first-use fallback. Until the operational public key is
pinned, the repository's explicit `UNCONFIGURED` state therefore makes stock
`cli.sh` non-installing by design. Provisioning and rollout are specified in
`packaging/signing/README.md`.

## Project Directory Detection

At startup, `main()` auto-detects the project root and sets `KIROCREW_PROJECT_DIR`:

1. If `KIROCREW_PROJECT_DIR` env var is already set, use it
2. Walk up from CWD looking for a directory with both `skills/` and `src/kiro_crew/` (`_PROJECT_MARKERS`). The project-level `agents/` dir was removed when agent config was consolidated into `src/kiro_crew/config/` (commit bbbc1f6e), so the marker no longer references it — a stale `agents/` requirement left detection (and the dashboard changelog) silently broken.
3. Read saved path from `~/.kiro/crew/project_dir` (written by `kirocrew setup`); the saved path is re-validated against the same markers

This allows `kirocrew` to find project-level agent config and skills from any directory.

## Commands

### Top-level help

`kirocrew --help` (and a bare `kirocrew`, which prints the banner first) does NOT
use argparse's own subcommand block. With ~40 commands that block is one flat
list in registration order, so the three commands a new install needs — `gateway`,
`service`, `doctor` — land in the middle of it, and the `{chat,doctor,gateway,…}`
choice blob makes the usage line unreadable.

`cli_help.py` owns the taxonomy instead:

- `COMMAND_GROUPS` is an ordered list of sections, each an ordered list of
  `(command, one-line summary)`. It is the single source of truth for what the
  top-level help lists and in what order; `Start here` is first and holds exactly
  `gateway`, `service`, `doctor`.
- Its notes answer the two questions the flat list never did: how `gateway`
  (foreground, dies with the terminal) differs from `service install` (systemd
  unit / launchd agent, detached, restarts on crash, starts at boot, only one at
  a time), and that the dashboard on loopback `5476` is the **only** port opened
  — messaging channels connect outbound.
- `cli.py` sets `help=argparse.SUPPRESS` on the subparsers action to hide
  argparse's listing, passes `cli_help.TOP_USAGE` as the top-level `usage=`
  (the suppressed action would otherwise drop the placeholder), and pins
  `prog="kirocrew"` on the action so each subcommand's own usage line is
  `usage: kirocrew <cmd> …` rather than the whole top-level usage string.
- Every user-facing command is registered with `cli_help.add_command(sub, name)`,
  which raises `KeyError` for a name that is in no section — a new command cannot
  be added without appearing in the help. The section summary becomes the
  subparser's `description`, which is what `kirocrew <cmd> --help` prints, so the
  sentence is not duplicated. A caller may pass its own longer `description`
  (`bench` does).
- Internal `mcp-*` servers call `sub.add_parser(name)` with no `help`, which keeps
  them out of both listings. They are also kept out of argparse's
  `invalid choice: 'x' (choose from …)` message: `cli_help.hide_internal_commands`
  swaps the subparsers action's `choices` for a live Mapping view over the same
  parser map that ITERATES only user-facing commands, in the help's section order.
  Membership is unfiltered and `_name_parser_map` is untouched, so `kirocrew
  mcp-core` still dispatches — the filter changes what argparse prints, never what
  it accepts. It must be installed after the last `add_parser` and before
  `parse_args`.
- `test/test_cli_help.py` pins offered-vs-listed parity by reading that same
  message, and pins that a hidden command still resolves.

| Command | Description |
|---------|-------------|
| `kirocrew chat -m "msg"` | Send a single message, print streaming response |
| `kirocrew chat` | Interactive chat mode (readline, exit with Ctrl+D) |
| `kirocrew chat --model X` | Override model for this session |
| `kirocrew gateway` | Start the Kiro Crew server (dashboard + messaging channels) |
| `kirocrew gateway --slack-only` | Start without dashboard or SSH tunnel instructions |
| `kirocrew gateway --no-crons` | Start without cron scheduler (use when another instance handles crons) |
| `kirocrew gateway --no-tunnel` | Never publish a tunnel: refuses to start or provision one for the life of the process, whatever `tunnel.enabled` says. SCOPED TO TUNNELS — it does not change where the dashboard binds, so a config that widens `dashboard.url` off loopback still does, with token auth as the control there; do not read `publish_disabled()` as "no published surface of any kind". Reach the instance on the loopback port it binds (`ssh -L` from another host). A Dev Fleet pod boots with this whenever its own checkout declares the flag — the pod's argv is built by the control plane but executed by the target worktree's gateway, so `pod.runtime.target_supports_flag` probes that checkout first and DROPS the flag when it is absent (passing it would make argparse exit 2, which the unit's `Restart=on-failure`/`RestartSec=5` turns into a 5s restart loop). Such a checkout keeps the tunnel behaviour it had before this flag existed and is not given the guarantee — see `security.md` for why no config-side substitute is applied. |
| `kirocrew setup` | Install agent config, save project dir, configure credentials |
| `kirocrew setup --agent-only` | Only install agent config (skip credentials) |
| `kirocrew setup --slack` | Run the guided Slack credential + slash-command setup (opt-in) |
| `kirocrew setup --whatsapp` | Run the guided WhatsApp opt-in: report the optional `whatsapp` extra and the pairing state, then enable the channel (opt-in) |
| `kirocrew doctor` | Verify kiro-cli is installed and config is valid |
| `kirocrew cron add/list/remove` | Manage cron jobs |
| `kirocrew spawn run/list` | Manage background subagents |
| `kirocrew app install/list/enable/disable/uninstall` | Manage App Kit apps. Uninstall preserves `apps/<name>/data/` by default. |
| `kirocrew app uninstall NAME --purge-data` | Explicitly uninstall an app and permanently delete its app data. |
| `kirocrew app dev <name> [--off] [--confirm-out-of-install-root]` | Toggle an installed app into/out of dev mode (no-store UI serving + live reload on file change). See [App Dev Mode](#app-dev-mode). |
| `kirocrew learn add/list/remove` | Manage learned corrections |
| `kirocrew run TASK.md` | Run an autonomous task from a spec file |
| `kirocrew token` | Print a dashboard access URL with auth token |
| `kirocrew logout` | Revoke all active dashboard sessions, refresh chains included |
| `kirocrew manifest` | Generate Slack manifest with user alias auto-populated |
| `kirocrew update` | Update to latest version (git fetch + hard reset to upstream + rebuild; a diverged checkout is refused — `--force` discards its local commits) |
| `kirocrew status` | Show runtime stats from running gateway |
| `kirocrew stop` | Stop a running gateway (service-aware: stops the systemd/launchd service if active, otherwise terminates the gateway found by a cross-platform port lookup — lsof on POSIX, netstat on Windows). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew restart` | Restart a running gateway (service-aware: restarts the systemd/launchd service if active, otherwise terminates the foreground gateway and respawns it detached). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew service install` | Install gateway as a system-level systemd service (Linux, requires sudo for `tee` + `systemctl` only) or launchd LaunchAgent (macOS, no sudo). Auto-restarts on crash, auto-starts on boot. |
| `kirocrew service uninstall` | Stop and remove the systemd unit / launchd plist. |
| `kirocrew service status` | Show service status (`systemctl status` or `launchctl list`). No sudo required. |
| `kirocrew logs` | Tail gateway logs from the systemd journal, launchd stdout file, or `~/.kiro/crew/gateway.log`. |
| `kirocrew logs -f` | Follow logs live (long-running tail). |
| `kirocrew cloud launch/list/status/connect/stop/start/destroy/iam-policy/doctor` | Provision, connect to, and manage a KiroCrew EC2 instance in the user's AWS account. |
| `kirocrew security events` | Show recent SEL audit events (`-n N` for count) |
| `kirocrew security verify` | Verify SEL HMAC chain integrity |
| `kirocrew snapshot` | Create a .tar.gz snapshot of all KiroCrew state |
| `kirocrew snapshot --keep N` | Auto-prune to N most recent snapshots (default 7) |
| `kirocrew snapshot --list` | List existing snapshots |
| `kirocrew restore <file>` | Restore from a snapshot (auto-detects replace vs merge) |
| `kirocrew restore <file> --mode replace\|merge` | Force restore mode; merge skips malformed incoming or local cron JSON with a file-specific warning |
| `kirocrew restore <file> --components X,Y` | Selective component restore |
| `kirocrew restore <file> --dry-run` | Preview restore without writing |
| `kirocrew restore --list-components` | Show available component names |
| `kirocrew snapshot --allow-unpinned-staging` | Stage by path name where a directory cannot be pinned by descriptor |
| `kirocrew restore <file> --allow-unpinned-staging` | Same, for the restore side |

### Staging is descriptor-pinned, and refuses rather than degrading silently

Snapshot and restore stage through `kiro_crew.pinned_fs`: the parent chain is resolved
once, pinned component by component with `openat` + `O_NOFOLLOW`, and everything
downstream is addressed through the descriptor already held. A validated path and the
inode later opened are otherwise not the same thing, and anything running as the user
— which in this product includes an agent — can plant the swap between the two.

`os.supports_dir_fd` is empty and `O_NOFOLLOW` does not exist on Windows, so pinning
is unavailable there. The decision, recorded here rather than only in the pull request
that made it: staging is **refused** on such a platform unless
`--allow-unpinned-staging` is passed, and when it is, the archive's `MANIFEST.json`
carries `"staging": "unpinned"` and `kirocrew restore --dry-run` prints that the
archive was staged by name. The refusal is the default because a by-name walk is not a
slightly weaker version of a pinned one; it is the mechanism whose failure closed two
earlier attempts at this change. The flag is a permission for a platform that cannot
pin, **not** a switch that turns pinning off where it works.

`MANIFEST.json` also carries `"skipped"`: any file omitted during staging (a hardlink
alias, a symlink, an entry that vanished mid-walk) with its reason, so an incomplete
archive says so in its own record instead of only in the console output of whoever ran
the command.

SQLite databases are **out of scope** for the pinned staging described here: they keep the
`sqlite3.backup()` path they already had, which reopens the live name. Capturing a live
database without reopening its name is a genuine conflict of requirements — SQLite accepts
only a path and cannot be pointed at a held descriptor — so it is tracked separately rather
than solved alongside the tree walk. The exposure is unchanged from before this staging
work, not introduced by it.

A refusal to stage is a permission decision and is written to the SEL audit log —
`snapshot_rejected` or `state_restore_rejected`, both with `reason=unpinnable_staging`.

The dashboard's import path (`portability.apply_import_zip`) is the **exception**, and
deliberately: it has no flag and no consent surface, so refusing there would not mean
"ask the user", it would mean deleting import on that platform. It therefore proceeds with
a by-name traversal where pinning is unavailable and records `"staging": "unpinned"` in its
returned summary, with a logged warning. Snapshot and restore keep refusing, because
`--allow-unpinned-staging` lets them ask. The per-entry screens apply on both paths — the
copy opens `O_NOFOLLOW` and the walk rejects links and reparse points — so what the import
path gives up is ancestor-swap resistance, not link resistance.

| `kirocrew config get [key]` | Print full config or a dot-path value |
| `kirocrew config set <key> <val>` | Set a config value (auto type detection) |
| `kirocrew config set --file <path>` | Replace config from a JSON file |
| `kirocrew config edit` | Open config in `$EDITOR` |
| `kirocrew memory list/search/stats/audit` | Inspect vector memory (entries, semantic search, counts, suspicious-content scan) |
| `kirocrew memory show [preferences\|projects\|history]` | Read the markdown memory layer (all three when no target given); `--format md\|json`, `--since YYYY-MM-DD` for history |
| `kirocrew memory export/import/migrate` | Export memory to JSON (`--include-markdown` adds the markdown layer), import it back, or migrate legacy markdown memory into the vector store |
| `kirocrew policy show/validate/explain/profile` | Inspect the effective enterprise security policy, load-check it and all profiles, explain one tool/scope decision for a surface, or print a profile. `show` also summarizes the built-in denied-command catalog as grouped counts (`--ids` lists each category's rule ids), on every install regardless of whether an enterprise policy is active — the one place an agent can learn a class of work is hard-denied before planning around it. |
| `kirocrew pod up/down/ls/status/token/url/scenarios/api/logs/exec/install/provision` | Isolated worktree test gateways (**Linux `systemd --user` only** — every systemd-touching verb refuses with a one-line message on macOS/Windows). See `src/kiro_crew/pod/README.md`. |
| `kirocrew pod scenarios [--json]` | List packaged seed scenarios in deterministic name order. Human output shortens each description to the last complete sentence that fits, cutting between words with an ellipsis when none does; `--json` emits an array of `{name, description}` rows with each fixture manifest's complete `description:` scalar; literal (`|`) blocks preserve newlines, while folded (`>`) blocks normalize to one paragraph. Extraction stays dependency-free without requiring PyYAML at runtime. An empty registry returns success with `[]` in JSON mode or an explicit human diagnostic. |
| `kirocrew pod api` | `api <wt> <METHOD> <path> [--data JSON] [--allow-write]` makes one authenticated request and prints `{name, method, path, status, ok, body}`; GET/HEAD are the default surface and other methods require `--allow-write`. It refuses caller-supplied `token` query parameters without echoing them, authenticates with the dashboard's query-token contract, caps response reads, and mints only after the pod PID record agrees with systemd MainPID (listener tools are optional corroboration). The authenticated request goes over the pod's private `dashboard-<port>.sock` in the pod's own home with no TCP fallback, so the minted token cannot reach a process that took the pod's port; an absent socket refuses through the envelope before minting. See `src/kiro_crew/pod/README.md`. |
| `kirocrew pod up --seed <scenario\|dir>` | Pre-populate the isolated home. A bare name is a packaged fixture and populates the whole home; a path contributes only its sanitized `config.json`. Unknown names are refused with the available list. Named fixtures copy directly into the final home through pinned source/destination directory descriptors; config/workspace setup uses the same held home, and the manifest lands last as the completion marker. Populated homes are never overwritten: a named-seed request against one refuses before start even when its marker matches; use plain `pod up` to restart it unchanged. Seeded config disables channel enablement and restores the sandbox floor. A per-instance systemd drop-in runs the checkout's own venv binary, post-health marker readback detects a seed that did not land, and `pod down` removes the drop-in with the home. Pod homes provide operational/state isolation, not protection from arbitrary same-UID host processes; Controller v1 invokes seeding from the host control plane and does not support nested pod control. |
| `kirocrew knowledge dedup [--apply]` | Collapse cross-source duplicate knowledge documents (dry-run unless `--apply`) |
| `kirocrew cron preview <script>` | Run a script cron locally with real MCP tools; notifications are captured and printed instead of delivered |
| `kirocrew workspace create/update --dir <name>` | `--dir` is a directory NAME that must resolve to a **strict descendant of the data home** (`~` is expanded first); anything landing outside — and the home **root itself**, in any spelling — is refused with a SEL `denied` audit event. Containment, not an absolute-path ban: an absolute path *under* the home resolves where the relative form would and is accepted. The strict-descendant test is what closes the root case for tilde paths, since the per-call-site root-equality checks compare un-expanded `config_dir() / ws_dir`. Deliberately stricter than the dashboard's `POST /api/workspaces`, which accepts an absolute `dir` anywhere, screened by `is_sensitive_path`. |
| `kirocrew computer doctor [--json]` | Report computer-use availability: platform support, the keystone primary-enable state, and the **advisory** macOS Accessibility / Screen Recording probe with a `responsible_hint`. See [Computer Use Commands](#computer-use-commands). |
| `kirocrew computer apps` | List on-screen applications the accessibility layer can address (human-facing twin of the `computer_list_apps` MCP tool). Gated by the same chokepoint as `call` — refused while the feature is off or the session is unattended. |
| `kirocrew computer call <tool> [k=v ...]` | Run ONE computer-use tool through the same gated chokepoint the agent uses, and print its reply (debug / reproduction) |
| `kirocrew computer call --calls '[…]'` | Run a JSON array of tool calls in a SINGLE process, so `element_index` values from an earlier `computer_get_state` are still resolvable |
| `kirocrew mcp-cron` | MCP server for cron tools (spawned by kiro-cli) |
| `kirocrew mcp-core` | MCP server for spawn, learn, task tools (spawned by kiro-cli) |
| `kirocrew mcp-computer` | MCP server for computer-use tools (spawned by kiro-cli; hidden — registered with no `help`, so it is in neither listing). A **thin shim** — it forwards to the gateway over loopback and does no accessibility work itself. |
| `kirocrew --version` | Print version |

## Token Command Output Streams

`kirocrew token` has a **machine-readable stdout contract**: stdout carries only
the dashboard URL(s), and every failure reason (invalid TTL, gateway not running,
gateway unreachable, empty token) goes to **stderr**.

The contract exists because stdout is parsed, not just read by a human. The
remote-mint path (`kiro_crew.instances.token_mint.mint_remote_token`) runs
`kirocrew token` on a remote host over SSH and regex-extracts the JWT from its
stdout. Error prose on stdout would both break the Unix convention and hide the
reason from a caller that captures stderr.

**Legacy remote handling.** Older remotes predate this split and still print
their failure reasons to stdout, which made a stderr-only error message degrade
to a bare `<no stderr>`. `mint_remote_token` therefore also carries a bounded,
redacted **stdout tail** in `TokenMintError` — appended only when stdout was
non-empty, so a current remote keeps the single-stream message shape. Because
stdout is the one stream that legitimately carries a token, the tail is
token-scrubbed (URL-borne and bare forms) before the generic credential and
exfiltration redactors run.

## Setup Command

`kirocrew setup` performs:

1. Saves `KIROCREW_PROJECT_DIR` to `~/.kiro/crew/project_dir`
2. Installs agent config to `~/.kiro/agents/kirocrew.json`
3. Prompts for Slack credentials and the slash-command name only when `--slack`
   is passed; the default wizard configures no messaging channels and prints a
   pointer to connect them later
4. Offers to set up custom domain `kirocrew.localhost` (macOS/Linux)

The saved project dir enables running `kirocrew` from any directory.

### First-run Kiro CLI prerequisite onboarding

KiroCrew exposes the same two-step readiness contract on every supported
platform: an executable candidate must answer `kiro-cli --version`, then
`kiro-cli whoami` must confirm authentication. Candidate discovery includes
supported fixed locations in addition to inherited `PATH`; unusable candidates
are reported for repair. Setup probes the same first executable candidate ACP
will launch, so a stale earlier candidate cannot produce a false-ready result
from a different later installation.

- Missing CLI: the setup page offers an explicit install action on macOS,
  Linux, and Windows. macOS/Linux download the fixed
  `https://cli.kiro.dev/install`; Windows downloads the fixed
  `https://cli.kiro.dev/install.ps1`. Every redirect and the final response
  must remain on the exact `cli.kiro.dev:443` endpoint and expected path, with
  no userinfo, query, or fragment. Redirect destinations are resolved and
  validated before any request is sent, and the chain is limited to three
  redirects. Responses are size-bounded and must match a release-pinned
  SHA-256 digest plus the platform-specific official installer marker. A
  changed upstream script therefore fails closed until a KiroCrew release
  updates the pin; the manual official guide remains available. The exact
  validated bytes stay in memory and run through the fixed system interpreter's
  standard input. The installer receives a system-only `PATH` plus explicit
  HTTP(S) proxy variables, never user-writable executable directories or
  ambient application credentials. The official installer additionally
  verifies its downloaded package manifest and artifact checksum.
- Unusable CLI candidates: the same page identifies that Kiro CLI needs repair
  instead of treating a spawn failure as a signed-out session. If the upstream
  POSIX installer would require an interactive `/dev/tty` replacement prompt
  (an existing macOS app bundle or Linux `~/.local/bin/kiro-cli`), automatic
  repair is disabled and the user is directed to the official guide.
  A candidate that already runs is directly usable for sign-in regardless of
  install source; the post-installer attestation file is now write-only
  bookkeeping and does not gate credential access.
- Installed but signed out: the setup page names the commands the USER runs and
  runs nothing itself — `kiro-cli login` for a personal account (Builder ID,
  Google, or GitHub), or `kiro-cli login --use-device-flow --license pro` for
  organization SSO, which prompts for the organization's start URL and region.
  Both are backend code constants rendered verbatim in a `<code>`, never catalog
  values, because a translated command cannot be typed. Both tiers are named
  because the browser portal the bare command opens presents a free Builder ID
  as a peer of organization SSO; Kiro Crew does not detect which tier applies,
  so the gate describes the choice and the user makes it. Sign-in completion is
  observed only through the read-only `kiro-cli whoami` probe.
- Browser dashboard: the authenticated SPA gate operates on the **gateway
  host**, not the browser host. This covers native Windows source installs,
  Linux gateways, and browsers connected to another machine.
- Desktop shell: the shell starts or reuses the gateway first, then displays the
  same gateway-served setup gate as a browser. Remote gateways are therefore
  checked on the remote host rather than the desktop host.
- Offline test harness: the explicit `gateway --test-mode` bundle injects a
  ready prerequisite state so deterministic fake-ACP smoke and Playwright
  suites do not depend on a developer machine's Kiro installation, identity, or
  Linux sandbox capabilities. Ordinary gateway invocations always use the real
  probe/install/login service.

The setup client cannot supply a command, URL, argument, or output path. The web
API exposes only fixed install/login mutations to the configured owner (or the
signed `local-app` / `local-startup` identities before an owner exists).
Authenticated non-owner dashboard users receive only a redacted readiness bit:
they enter the dashboard once ready but cannot see host state/output or operate
setup. App tokens remain denied. Electron has no separate installer/login IPC
or subprocess implementation. Filesystem discovery runs off the event loop.
Version probes use a minimal noninteractive environment with no proxy
credentials or desktop-session IPC. They use the strict OS sandbox and
additionally hide the configured data home, `~/.kiro/crew`, `~/.kirocrew`, and
every known Kiro identity store. Any candidate that runs `--version` is eligible
for `whoami` and device login — trust is "it runs, and it has a valid login",
not install source, owner, or fixed path (KiroCrew is not the authority on where
Kiro CLI is installed, and its self-updater rewrites its own bytes as the user).
Auth calls execute the user's installed binary IN PLACE, never a private copy of
its bytes — a multi-call Kiro CLI resolves its sibling subcommand executable
relative to its own path, so a copy strands it (see security.md).
Sign-in itself is delegated to Kiro CLI: `login --use-device-flow` runs in the
standard sandbox against the user's real home, with only the Kiro Crew data
homes hidden, and the CLI writes its own credential store exactly as it does
from a terminal. KiroCrew stages nothing and publishes nothing, so no staged
state has to be reconciled after a failure, timeout, or cancellation. The
credential-minimal temporary home populated only with Kiro identity JSON and
SQLite files survives as an opt-in read-only mode — one that also hides
unrelated AWS, SSH, GitHub, and Kubernetes state — and its temporary directory
is removed on every exit path. Any allowlisted live identity artifact that is a
symlink, non-regular, oversized, unreadable, or disappears while being captured
aborts that mode before the command runs. Every
probe emits a critical `invoked` SEL event before spawn
and a best-effort terminal event without argv, candidate paths, output, or
environment values. Installer and login timeouts cover process exit and
output-pipe draining. On POSIX, a private supervisor remains the process-group
leader after the real command exits and keeps the group safely addressable until
all descendants close or are terminated. Windows cleanup opens an exact
primary-process handle before yielding after spawn and completes an initial
descendant snapshot even when the launcher exits immediately. It then retains
exact child handles and continues discovery from every live child, so late
helpers remain supervised and identifier reuse cannot target an unrelated
process. Numeric parent edges are accepted only when exact-handle creation and
exit times prove that the child was created during the parent's lifetime, and
the check is repeated across both tree snapshots. The primary root and each
retained child root receive one final snapshot after becoming inactive, so a
child spawned immediately before its parent exits is not lost between polls.
Failure to anchor or validate the primary process, create a Toolhelp snapshot,
or complete any process enumeration fails the operation closed. Ordinary
pipe/task errors follow the same terminate, reap, cancel, and cleanup path
before another action can start.

An auto-created `config.json` alone does not mark first-run setup complete; a
successful authenticated probe writes the setup marker, while existing
session/history state preserves established-install migration behavior. Fresh
installs receive the full-screen flow. Established dashboards remain navigable
and fully usable when signed out — no controls are paused. Readiness is probed
once at gateway boot and thereafter only on explicit user action, so no path runs
a subprocess probe on the message hot path; the authoritative logout signal is
the ACP attempt's `AcpAuthRequired`. The SPA refreshes ready status every 30 seconds,
retains cached readiness across transient refetch errors, and invalidates
prerequisite state after access-cookie refresh.
POSIX group membership ignores zombie records, which cannot retain pipes or
perform work, so an unreaping PID 1 cannot hold the supervisor forever.
The supervisor source is captured eagerly at import for replacement resistance;
if it is missing or unreadable, gateway import still succeeds and each affected
POSIX setup operation fails cleanly before spawning a command.
Sandbox launcher/profile preparation and cleanup are worker-thread operations
and do not stall the asyncio gateway loop.

Setup and ACP launch share the side-effect-free `kiro_cli` resolver on every OS.
Status requests never publish a discovered path by mutating `KIROCREW_KIRO_BIN`.
Both setup discovery and ACP launch enumerate the same candidates — inherited
`PATH`, the interpreter Scripts directory, package-manager dirs (incl. the
Windows Program Files `Kiro-Cli` tree and winget/scoop/user installs on `PATH`),
and an operator override — and accept a runnable candidate wherever it lives,
since trust is "the CLI runs". ACP launch runs the resolved candidate in place on
every platform — never a copy of its bytes. Setup discovery and ACP resolution therefore agree on
Windows, so a winget/scoop install is never sent to a redundant reinstall.

When a previously completed setup is no longer ready, the dashboard remains
fully navigable and fully usable — nothing is paused and no sign-in chrome is
shown. A signed-out CLI is reported by the turn itself as an actionable
`kiro-cli login` error card (see `modules/learn-cron-dashboard.md` § "The
dashboard does not guide the user to sign in"). Only the endpoints that act
BEFORE a turn still return 503: the poll-driven `kiro-cli` spawn sites
(`/api/models`, `/api/sessions/usage`) and the destructive reruns (regenerate,
edit-resend, rewind), which rewrite persisted history up front.

### Custom Domain

After credentials, `kirocrew setup` offers to add `127.0.0.1 kirocrew.localhost` to the system hosts file so the dashboard is accessible at `http://kirocrew.localhost:5476`:

- **macOS/Linux**: Uses `sudo tee -a /etc/hosts` for safe append

Skipped if `kirocrew.localhost` is already present or user declines.

## Cloud Command

`kirocrew cloud` is a human installer/control-plane surface for running
KiroCrew on the user's own AWS EC2 instance. Provisioning and teardown are not
LLM-facing tools. AWS credentials are resolved by the AWS CLI; KiroCrew stores
only profile, region, and the most recent instance tag in `cloud.json`.

`kirocrew cloud launch` runs a six-step wizard: check AWS reachability, explain
permissions, choose whether to keep an existing deployment or create a new one,
choose an instance size when creating a new stack, deploy or resume the
CloudFormation stack, sign in the remote `kiro-cli`, and open the dashboard
through SSM port forwarding. Launch is resume-safe by default: if `cloud.json`
contains a `last_tag` whose stack still exists in the same saved profile/region,
rerunning interactive `launch` offers to keep/resume that stack or create a new
installation. If `cloud.json` is missing or stale, launch discovers existing
`kirocrew-*` CloudFormation stacks with `cloudformation:ListStacks` and offers a
choice to resume one or create a new installation. `kirocrew cloud launch --new`
is the explicit escape hatch for creating a separate new stack. `--yes` keeps a
single or saved existing stack; if multiple unsaved stacks exist it fails closed
instead of choosing one arbitrarily. For a new launch, the generated tag is
written to `cloud.json` before the long CloudFormation deploy starts, so an
interrupted provisioning run can be found on the next launch attempt.

Launch and connect require the local AWS Session Manager plugin for
`AWS-StartPortForwardingSession`. If `session-manager-plugin` is missing,
`cloud launch` prompts to install AWS's official package for the current local
platform (macOS `.pkg`, Debian/Ubuntu `.deb`, or RPM Linux `.rpm`) before the
wizard reaches sign-in/dashboard tunneling. `--yes` accepts this installer
prompt. `cloud connect` performs the same check and installer prompt before
opening the dashboard tunnel. If installation is declined or fails, the command
exits non-zero and tells the user to retry after fixing the local prerequisite.

The instance-size picker supports arrow keys in an interactive terminal
(`↑`/`↓`, `j`/`k`, digit shortcuts, Enter to select) and falls back to the
numbered prompt for non-TTY input. Ctrl-C must interrupt prompts and long AWS
subprocesses; unhandled cloud-command interrupts return exit code 130.

Remote Kiro sign-in prefers the device-code flow over SSM. The launcher starts
`kiro-cli login --use-device-flow` as a background process on the instance,
captures the URL/code from its log, and leaves that same process alive while the
wizard polls for completion. It must not kill that process after scraping the
prompt or start a second hidden device-code flow. If device-code startup does
not produce an actionable URL, launch falls back to the Google/GitHub callback
flow automatically: it starts `kiro-cli login` on the instance with FIFO-backed
stdin, captures the printed loopback callback port, opens an
`AWS-StartPortForwardingSession` from the same local port to the remote port,
sends the Enter continuation back to the remote CLI, then opens or prints the
local browser URL. The temporary callback tunnel is closed after the sign-in
poll completes. In headless local terminals, browser auto-open is skipped and
the URL is printed for manual opening.

`kirocrew cloud connect` mints a dashboard token over SSM, opens an
`AWS-StartPortForwardingSession`, waits for the local tunnel port to accept TCP
connections, and opens or prints the local dashboard URL. If the tunnel port
does not become reachable, the command reports failure, does not present the
dashboard URL as usable, and does not keep a dead tunnel process open. If final
dashboard opening fails during `cloud launch`, the instance remains running but
launch returns non-zero and tells the user to rerun `kirocrew cloud connect`
after fixing the local SSM tunnel issue.

## Config Command

`kirocrew config` manages `~/.kiro/crew/config.json`:

- **get** — prints full effective config (with defaults resolved) or a single dot-path value
- **set key value** — sets a value with auto type detection (bool/int/float/JSON/string). Rejects unknown leaf keys.
- **set --file path** — replaces entire config from a JSON file. File read routed through `hooks.safe_read_file()` (blocks sensitive paths).
- **edit** — opens config in `$EDITOR` (supports args like `code --wait` via `shlex.split`). Creates default config if missing.

All write paths emit SEL audit events (`config_get`, `config_set`, `config_set_file`, `config_edit`).

### Gateway Auto-Create

`kirocrew gateway` creates `~/.kiro/crew/config.json` with defaults if the file doesn't exist. Does nothing if it already exists.

## Verbosity

| Flag | Level | What you see |
|------|-------|-------------|
| (none) | WARNING | Errors only |
| `-v` | INFO | Session lifecycle, context %, compaction |
| `-vv` | DEBUG | ACP events, message updates, full traces |

## Interactive Mode

- Prompt: `you> `
- Exit: `exit`, `quit`, `/exit`, `/quit`, `:q`, Ctrl+D
- Streaming output printed as chunks arrive

### Tool permission requests

A backend that routes tool decisions over ACP holds the turn open until it gets
an answer, so the stream consumer must respond — an ignored request is not a
missed prompt, it is a turn that never ends.

Answering one is an **authorization decision**, so the CLI is a security
surface, not just a prompt. Every request runs the same ladder, in this order:

```
permission_request
  → HookManager.on_tool_call        (sensitive paths, denied commands, ceiling ∩ profile)
      deny → SEL "denied" → reject_tool → stderr notice          [not overridable]
  → may this invocation ask?        (command mode AND both streams a TTY)
      no   → SEL "denied" → reject_tool → stderr notice
  → prompt the human
      exactly "a" → SEL "allowed" → approve_tool(always=False)
      anything else → SEL "denied" → reject_tool
```

The gate is fed the event's non-model-authored fields (`tool_kind`,
`raw_tool_params`, `shell_command`, `is_shell`, `mcp_server_name`, `tool_name`),
not just `title`: for a shell tool the title may be an LLM-authored
description, so a dangerous command behind a benign label is exactly what
keying on the title alone lets through. The CLI identifies itself as
`session_key="cli_chat"` (SEL source `cli`) with the resolved agent, which is
what lets the gate resolve `ceiling ∩ profile` rather than the ceiling alone.
No second copy of the sensitive-path or denied-command rules lives in the CLI.

The hook result is used as a **deny ceiling only**: `TOOL_DENY` rejects, and
both `TOOL_ALLOW` and `TOOL_AUTO_APPROVE` still ask the human. This consumer
answers permission requests; it does not carry the dashboard's trust and
auto-approval semantics, and honouring `TOOL_AUTO_APPROVE` here would add a
second execution path with no human confirmation. Asking more often than the
dashboard is the safe direction.

| Mode | stdin+stdout TTY | Behaviour |
|---|---|---|
| interactive REPL | yes | Prompt. Exactly `a` allows once; anything else denies. |
| interactive REPL | no | Deny automatically, notice on stderr, stdin untouched. |
| `-m` single message | either | Deny automatically, notice on stderr, stdin untouched. |

Both conditions are required, and neither implies the other. `-m` is documented
as `Single message (non-interactive)`, so a terminal does not license a prompt
there — a script wrapped in a pty would otherwise block on a question nobody is
watching for. And a prompt nobody can see is a hang, not consent, so the REPL
still needs a real terminal on both ends. The mode is passed explicitly rather
than inferred from a TTY check, because only the caller knows which mode runs.

A shell call shows the command it is asking about:

```
Permission required: Run a helpful script
Command: git status --short
   [a] allow once  [d] deny (default):
```

`title` is LLM-authored prose, so approving on it alone is consent to a
description rather than to what runs — the same reason the gate keys on
`shell_command`. The command is redacted with the two `security` helpers,
collapsed to one line, and capped with an explicit `... [truncated]`. Local
paths are deliberately **not** redacted here: this is the operator's own
terminal, and seeing the real path is part of the consent.

**A call that names a file discloses that file.** A trusted tool identity says
WHICH tool runs, not what it runs against, so `fs_write` under a benign title is
consent to a verb:

```
Permission required: Tidy up the notes
Tool: fs_write
Path: /home/tester/thesis.md
   [a] allow once  [d] deny (default):
```

The path is read from the request's own `raw_tool_params`, under the same
`path` / `file_path` / `filePath` spellings `hooks._SEARCH_DENY_ARG_KEYS`
accepts. Sharing the spellings is the point: the gate already denies a
*sensitive* path or a write-protected config path read from those keys, so what
is left for a human to judge is exactly the ordinary valuable file no rule
speaks for — and a prompt reading a different field than the gate inspects would
let the two disagree about what the target is. Rendered like the command
(sanitised, one line, capped), and shown for any call that carries a path rather
than only an `edit`-kind one, because the kind is agent-influenced and
disclosure can only inform the decision.

Absence of a path is **not** a refusal. Most builtin calls legitimately act on no
file — a memory write, a tag creation — so denying whenever a path cannot be
found would refuse them all to close a gap that exists only for tools which name
one. A non-string value is treated as absent rather than raised on: raising would
leave the request unanswered, which is the hang this path exists to end.

Beyond the command and the path, the whole tool input is still **not** shown —
this is the question, not a detail panel.

**Terminal controls are neutralised on this surface.** Every untrusted string
the permission UI prints — the title, the command, and a gate reason — goes
through `_for_consent`, which redacts as above and then replaces ESC, the C0 set
(`U+0000`–`U+001F`), DEL (`U+007F`), and the C1 set (`U+0080`–`U+009F`) with
spaces before collapsing whitespace, so the result is always a single line. Lone
UTF-16 surrogate code points (`U+D800`–`U+DFFF`) are removed by the same boundary:
they are not Unicode scalar values, so even a strict UTF-8 stream cannot encode
them. The result is then round-tripped through the destination stream's codec
with `backslashreplace`. UTF-8 terminals retain ordinary Unicode; a strict cp1252
or other legacy Windows stream sees inert ASCII escapes for characters it cannot
represent. This is an authorization surface: OSC 52 writes the clipboard, and
CSI can move the cursor and erase what is drawn, so a model-authored title could
otherwise repaint the question a human is answering and hide what is being approved.
Scope is the permission prompt only — ordinary streamed model output is printed
raw by `_send_and_print` and is unchanged, which is a surface-wide rendering
question rather than part of answering a permission request.

**An authorization-boundary failure is itself a refusal.** If the shared gate
raises, the CLI records `gate_failed` best-effort and rejects the request. If TTY
detection, prompt rendering, or prompt reading raises, it records `prompt_failed`
best-effort and rejects. The transport response is sent before any explanatory
notice, so a closed or unencodable output stream cannot leave the backend waiting.
This does not change cancellation: Ctrl-C still aborts the session and provider
teardown owns the pending request, because waiting on a possibly wedged rejection
transport would swallow the cancellation.

**The audit write never runs on the event loop.** `sel()` opens the audit log
and replays it to recover the running HMAC chain, so the first permission of a
fresh chat pays a filesystem cost inside the call — an unbounded one on slow or
corrupt storage. The decision coroutine shares its loop with the ACP reader and
stderr-drain tasks, exactly as the gate call does, so the audit is awaited
through `asyncio.to_thread` for the same reason: a blocking write here would
stop draining the backend and freeze the turn the audit is about. The one
exception is the cancellation teardown, which keeps the synchronous call because
awaiting anything there would swallow the `CancelledError` being delivered.

**There is no "always allow".** A persistent approval asks the backend to stop
sending permission requests for matching calls, and a request that is never sent
is a call this ladder never runs and never audits. The dashboard offers it
because its tool pipeline re-gates every call; this consumer does not.

The answer is matched **exactly**, not by first letter: a prefix match reads
`abort` as an allow. Blank line, EOF, and any unrecognised word all deny.

Denial is **not** an error: the request is answered, the turn runs to completion,
and the exit code is unchanged. Only the existing transport failures
(`AcpTimeoutError`, `AcpError`) exit non-zero.

Every decision is written to the SEL log **before** the matching
`approve_tool`/`reject_tool`, so a transport failure cannot erase a decision
already made:

| decision | `outcome` | `error` |
|---|---|---|
| gate denied | `denied` | `hook_deny` |
| gate raised before a verdict | `denied` | `gate_failed` |
| execute-kind request has no verified command | `denied` | `unverified_shell` |
| nobody to ask | `denied` | `noninteractive` |
| prompt availability/render/read failed | `denied` | `prompt_failed` |
| user allowed | `allowed` | — |
| user allowed but the critical audit failed | `denied` | `audit_unwritable` |
| user denied / blank / EOF / unrecognised | `denied` | `user_denied` |
| session cancelled with the question open | `denied` | `session_aborted` |

`error` is a stable machine code, never the gate's reason: a reason names the
path or command that triggered it, and an audit record must not restate the
thing it protects (`log_tool_invocation` does not redact for its callers). The
audited `tool_name` is the canonical `_meta.kiro` identity when the backend
supplies one, falling back to a redacted `title` — an audit trail keyed on prose
the model wrote can be steered by the model being audited.

Responses go through `provider.approve_tool()` / `provider.reject_tool()`. The
CLI never reads the advertised `options`: those ids are backend-specific and the
ACP layer owns the mapping.

#### stdin ownership

The prompt is read on an owned daemon thread through a private duplicate of the
stdin descriptor, not on the event loop and not through `sys.stdin`. The turn is
parked inside an active stream — the ACP runtime holds a reader task on the
backend's stdout and a drain task on its stderr — so a read on the loop thread
would stop draining those pipes until the human answers.

**A cancelled prompt ends the session.** A blocking terminal read cannot be
retracted: cancelling the await frees the coroutine, not the thread, and that
reader stays parked and takes the next line the user types. So cancellation
marks stdin poisoned and propagates; `_require_usable_stdin` then refuses at
every later entry point — a second permission prompt and the REPL's own `you>`
alike — rather than racing the abandoned reader for keystrokes. There is no
input broker and no recovery path: the abandoned reader can only outlive the
session, never compete with a live prompt.

Teardown **must not await the backend.** The request in flight is left
unanswered and audited as `session_aborted`, because answering it means awaiting
a transport: `CancelledError` has already been raised once and nothing
re-delivers it, so a wedged `reject_tool` would leave the Ctrl-C that asked for
the teardown unable to land. The provider is shut down with the session, so the
unanswered request dies with it. `StdinPoisonedError` carries the same rule.

That last sentence is a guarantee, not an expectation: `provider.start()` hands
back a live backend process, so `_chat` runs the whole message/REPL lifecycle
under `try` and calls `provider.shutdown()` from `finally`. A normal return, an
exception, and a cancellation raised through a permission prompt all tear the
backend down; without it a Ctrl-C at the prompt would exit leaving the backend
running with nothing owning it. Cleanup never swallows the cancellation — a
failing `shutdown()` is logged rather than raised, because replacing the
exception already propagating would discard the very `CancelledError` the
teardown exists to clean up after — and `gc.collect()` is nested inside its own
`finally` so a raising shutdown cannot skip it.

### Context Tracking

After each message, checks `provider.context_usage_pct()`:
- `>= autocompact_pct` (default 70%): compact → shutdown → restart provider, reset counter
- `>= autocompact_pct - CONTEXT_WARN_MARGIN_PCT`: warning printed to stderr. Relative, not absolute: the compact arm is tested first, so an absolute warn level at or above the threshold would be unreachable

CLI compaction is blocking (single-user, acceptable).

## Entry Point

`console_scripts` in `setup.cfg` maps `kirocrew` → `kiro_crew._bootstrap:main`.

### Gateway asyncio child watcher

`_install_child_watcher()` runs once on the **`gateway` command path only** (not
`chat`, `doctor`, or any other subcommand) and must be called before
`asyncio.run`, on the main thread. It replaces CPython's default
thread-per-child `ThreadedChildWatcher` — whose `os.waitpid` reaper threads can
starve the event loop when many `kiro-cli`/MCP children die at once — with a
single-descriptor alternative:

| Runtime | Installed watcher |
|---------|-------------------|
| Linux, `os.pidfd_open` probe succeeds (kernel ≥ 5.3) | `PidfdChildWatcher` |
| Linux, probe raises `OSError`/`AttributeError` | `SafeChildWatcher` (SIGCHLD) |
| macOS / other non-Linux Unix | `SafeChildWatcher` (SIGCHLD) |
| **Python ≥ 3.14** (child-watcher API removed) | **none — no-op** |
| `SafeChildWatcher` unavailable (e.g. Windows) | none — default retained |

**Python 3.14+ is a deliberate no-op.** CPython 3.14 removed
`set_child_watcher`, `PidfdChildWatcher`, `SafeChildWatcher`, and
`ThreadedChildWatcher`; the Unix event loop reaps children itself with a single
non-thread reaper, so the loop-starvation wedge this installer exists to prevent
cannot occur. The function short-circuits on `hasattr(asyncio,
"set_child_watcher")` — probed by capability, not `sys.version_info`, so a
runtime that still ships the API keeps the mitigation. Without that guard the
Linux pidfd branch raised `AttributeError` and `kirocrew gateway` died before
binding its port, while every other subcommand kept working.

### Linux gateway heap reclamation

The event-loop heartbeat offers a self-gating maintenance object a tick every
five seconds. At most once every ten minutes on Linux, it reads current RSS
directly from procfs. When RSS is at least 1.5 GiB, it asks glibc
`malloc_trim(0)` to return wholly-free heap pages to the OS and logs reductions
of at least 16 MiB. The probe and allocator call run in a worker thread, after
the dashboard socket has bound, so maintenance cannot delay readiness or block
the event loop. Healthy gateways remain below the threshold and do no
allocator-wide work.

The heartbeat waits at most two seconds for a pass. A timed-out worker keeps the
single in-flight slot until it exits, preventing repeated submissions or a
watchdog-triggering wait when the executor is saturated. Missing `ctypes`,
non-glibc libc, failed current-RSS probes, and rejected trim calls are
best-effort no-ops: reclamation must never stop the liveness heartbeat or make
the gateway unavailable. macOS and Windows are unchanged.

### Live-target bootstrap

On the `gateway` command path only, immediately after `_JAILED_COMMANDS`
attestation and before the `--seed` handler:

```python
if args.command == "gateway":
    from kiro_crew.service.live_target import maybe_reexec
    maybe_reexec(sys.argv[1:])
```

`maybe_reexec` reads the live-target pointer (`config_dir() / "live_target.json"`)
and, when it names a different checkout, `os.execve`s into that checkout's own
`kirocrew` binary. This runs before anything is written to `$KIROCREW_HOME`,
before the gateway lock is acquired, and before any socket is bound — so exec'ing
away leaves nothing half-done. It is **fail-safe**: an absent, unreadable,
malformed, or stale pointer (missing binary, same image already running, or
`KIROCREW_LIVE_EXECED` marker already in env) causes the function to return, and
the currently-installed build boots normally. A bad pointer can never leave the
host with no gateway.

Gateway only — a plain CLI invocation (`kirocrew doctor`, `kirocrew chat`, etc.)
keeps running the install the user typed, not a worktree someone made live.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `KIROCREW_HOME` | Override config/data directory (default `~/.kiro/crew`) |
| `KIROCREW_PORT` | Override dashboard port (default `5476`, validated as int at CLI startup) |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory |
| `KIROCREW_WORKSPACE` | Override workspace root directory |

For local dev:
- **macOS/Linux**: `bin/kirocrew` (POSIX shell wrapper); `source setup.sh` adds `bin/` to PATH

The wrapper sets `KIROCREW_PROJECT_DIR` and routes to the right runtime based on install type:

- **One-liner install** (`install.sh` clones the repo into `~/.kirocrew-app/`): if a sibling `.venv/bin/kirocrew` exists, the wrapper execs it directly.
- **pip editable install** (`pip install -e .`): the console_scripts entry point resolves directly.

## Setup Scripts (First-Time Bootstrap)

`setup.sh` (macOS/Linux) auto-installs all dependencies from scratch using public tooling only.

> **Note:** Windows is not supported.

**Install order:**
1. Node.js (via `ensure-node.sh`)
2. Optional tools (git-lfs, ffmpeg for voice)
3. kiro-cli (`npm i -g`)
4. kiro-cli login (guided authentication)
5. Frontend build (`npm install && npm run build`)
6. Backend build (`pip install -e .`)
7. PATH setup + shell profile persistence
8. `kirocrew setup --agent-only` (install kiro-cli agent config)
9. Optional Slack credential configuration (`kirocrew setup --slack`)

Each step checks if the tool is already installed and skips if present.

## Doctor Checks

1. `kiro-cli` binary in PATH
2. Project directory and git repo
3. Agent config installed
4. Config values (provider, model, approval mode, dashboard port)
5. **MCP tools**: `@kirocrew-cron` and `@kirocrew-core` in `tools`, `allowedTools`, and `mcpServers` — auto-fixes missing entries
6. **Global mcp.json**: kirocrew MCP servers present with valid binary paths — auto-fixes stale paths
7. **Python environment**: checks Python 3.9+ availability and dependency installation
8. **Vector memory (in-process embeddings)**: vendored llama-cpp-python runtime importable, embedding model file present (downloads in background on gateway start; when absent, a light HTTPS-reachability probe of the resolved model URL runs); embeddings are always-on (`embeddings:  ✅ always-on`). On platforms with no vendored native libs (`_platform_libs_dirname()` returns None, e.g. darwin/x86_64 — Intel Macs or a Rosetta interpreter), the runtime line reports `⏹ unsupported platform … — memory uses keyword search` and is NOT counted as an issue (designed degradation per `embeddings.py`); only a load failure on a supported platform flags `embedding runtime`. When that failure is an INCOMPLETE shipped payload, doctor additionally names the absent files (`Missing native libs for <platform>: …`, from `embeddings.verify_vendored_libs()`) and says it is a packaging defect rather than an unsupported platform — the two are indistinguishable in ctypes' own `Shared library with base name 'llama' not found`, which reads as an architecture problem and misdirects diagnosis. When `LLAMA_CPP_LIB_PATH` is set, doctor reports THAT directory as the thing to check instead (mirroring the loader's exemption): the libs load from there, so blaming the bundled tree would send the operator to reinstall a package they are deliberately not loading from. A `faiss:` line reports whether the optional FAISS accelerator is importable — never an issue on any platform (episodic recall falls back to the stdlib cosine scan); when absent it suggests `pip install faiss-cpu`
9. **Speech-to-Text (optional)**: recognizer, selected model and audio-decoder presence when STT is enabled. Supported desktop releases bundle and build-gate the recognizer plus the pinned `imageio-ffmpeg` executable; a missing decoder there is a corrupt payload whose remedy is reinstalling Kiro Crew, never Homebrew/Winget/Apt. Source installs use `kirocrew[voice]` for the recognizer and a fixed system FFmpeg path for compressed audio. Windows preserves its non-fatal `⚠️` marker for an optional-extra gap so enabled-by-default STT cannot block gateway startup; on macOS/Linux a missing active runtime still flags an issue.
10. Slack credentials (optional)
11. **Discord (optional)**: the channel's enabled flag, whether a bot token is present (never any part of its value), the three allow-lists, the privileged Message Content intent, the live connection, and the install URL. Blocking issues are enabled-without-a-token, an empty `discord.allowed_user_ids` (the transport fails closed, so every message is denied while it is empty), a thread or channel allow-list with Message Content OFF, and a reachable gateway whose Discord connection recorded a `connect_error`. The intent state comes from `discord/intent_probe.py`: one read-only `GET /oauth2/applications/@me` that decodes Discord's application-flags bitfield as a tri-state per intent PAIR (`enabled` / `limited` / `disabled`, since a limited grant still delivers the data) and degrades to `unknown` on any failure rather than aborting the report. Granted-but-unused Server Members / Presence intents are hardening notes, never issues. The install URL comes from `discord/install_url.py`, the OAuth-authorize analogue of Slack's app manifest: named permission bits OR'd to `309237711936` for a thread-capable install (the number [`discord-integration.md`](../../../src/kiro_crew/docs/discord-integration.md) publishes), and none at all for the recommended DM-only install
12. **WhatsApp (optional)**: printed whether or not the channel is enabled, because a channel that is invisible in the preflight is the failure this section exists to catch. When enabled it reports the optional `neonize` extra, checked with `find_spec` and never imported (importing it loads a ~19 MB ctypes CDLL plus protobuf descriptors, and a health check must not initialize the subsystem it inspects, nor construct a client), and whether the linked-device session store exists at `<data home>/whatsapp/session.db`, resolved from the same expression the channel opens it with so the two can never describe different files. A missing extra IS an issue: the channel is enabled, cannot start, and the fix is one offline `pip install`. An absent store is a `⚠️` note and never an issue, because pairing is a QR scan served BY the running gateway, so failing here would break the documented `kirocrew doctor && kirocrew gateway` chain at the one moment the operator has to start the gateway to make progress. Group membership is not knowable offline, so the section reports the configured count and the gateway logs the unmatched JIDs on connect
13. kiro-cli connectivity
14. Gateway running status
15. **Cron job health**: names cron jobs that auto-paused after repeated failures (`Fix: kirocrew cron resume <id>`) and jobs whose last run errored while still scheduled (`Fix: kirocrew cron trigger <id>`), with an aggregate count each and the named list capped at 5 plus a `+N more` tail. Read-only — doctor never resumes or triggers a job, because an auto-pause after `_AUTO_PAUSE_THRESHOLD` consecutive failures is usually load-bearing and lifting it silently would hide the problem the run is meant to find. The scan is `cron.unhealthy_jobs_from_disk()`, which reads `crons.json` directly rather than via the gateway API: the dashboard's per-job `err` badge and the gateway's hourly failure re-alert both run inside the gateway, so neither can report a wedged one, and that out-of-process property is the point of this check. A job the user paused explicitly is never reported (only `auto_paused` is a health signal, and both flags can be set at once since pausing an auto-paused job preserves `auto_paused`). Silent on a healthy store and on a fresh install with no `crons.json`. A store that EXISTS but yields no readable job list — unparseable, non-UTF-8, wrong shape, or holding no usable record — is reported instead, because the scheduler can load nothing from it and every job has stopped; reporting that as a clean bill of health would reproduce the silence this check exists to break. No corruption aborts the run

## Update Command

`kirocrew update` pulls the latest source and rebuilds:

1. `git fetch` + `git reset --hard origin/<branch>` from `KIROCREW_PROJECT_DIR`.
   The reset only runs for a FAST-FORWARDABLE checkout — behind its upstream
   and not ahead of it (`git rev-list --count --left-right
   HEAD...origin/<branch>` shows behind > 0, ahead = 0) — mirroring the
   dashboard check's verdict, because the hard reset discards committed local
   work and the uncommitted-changes prompt does not cover it. A DIVERGED
   checkout (both sides non-zero) is refused with a non-zero exit and a
   rebase-or-merge instruction; `--force` is the explicit opt-in that lets the
   reset discard the local commits. An ahead-only checkout has nothing to pull
   and is reported as up to date without resetting (even under `--force` — the
   flag lets a real update discard diverged work, it does not delete commits
   when there is nothing to update to). An unreadable comparison refuses
   (fail closed). Uncommitted tracked changes still prompt before being
   discarded — and because that prompt makes the gap to the reset unbounded,
   the divergence count is re-taken immediately before the reset and refuses
   commits that appeared while the update was waiting (committing the listed
   edits in another terminal to rescue them is the natural response to the
   prompt, and is exactly what would otherwise be reset away). Only `HEAD` can
   move in that window, so the re-check needs no second fetch.
2. Rebuilds the dashboard via `build_frontend_sync()` (npm; non-fatal on failure).
   Non-fatal also means non-destructive: `npm ci` deletes `node_modules` before it
   installs, so the tree is moved aside and restored unless the install succeeds
   (`node_modules_txn.NodeModulesBackup`). A failed install therefore costs the
   rebuild, not the dependency tree -- which matters because the registry needed
   to rebuild one is usually what was unavailable. The gateway's unattended
   auto-apply takes the async sibling and is protected the same way.
3. Reinstalls backend via `pip install -e .`

## Client Port Resolution

`kirocrew token` / `status` / `logout` / `stop` / `restart` must find the port
the gateway is actually bound to. `port_resolution.resolve_client_port()`
(re-exported by `cli_server`) resolves it
in this order, first hit wins. The MCP stdio servers (`mcp_core` /
`mcp_computer`) resolve their gateway API base through the same helper —
lazily, on the first gateway call, and cached for the process lifetime — so a
loopback callback and a client CLI command always agree on which gateway they
are talking to:

1. An explicit `--port N` flag (`0` counts — the check is `is not None`).
2. `KIROCREW_PORT`, when it parses as an int. Deliberately above the bound
   export: an explicitly-set `KIROCREW_PORT` is how a caller retargets a
   child at a DIFFERENT gateway — `pod exec` builds a client env with
   `KIROCREW_PORT=<pod-port>` while the inherited `KIROCREW_BOUND_PORT`
   still names the spawning live gateway, and the bound value outranking it
   would walk pod `token`/`status`/`logout` into the live plane.
   (`build_pod_env` additionally scrubs `KIROCREW_BOUND_PORT` outright.)
3. `KIROCREW_BOUND_PORT`, when it parses as an int — the port the parent
   gateway actually bound, exported once its TCP site is listening
   (`dashboard.server._export_bound_port`). Never persisted —
   `service_environment()` deliberately does not capture it.
4. A port **explicitly written** in `dashboard.url`. A portless URL
   (`http://my.host`) is *not* a port choice: `parse_dashboard_url()`
   substitutes `5476` for the server's benefit, so the client re-splits the URL
   and only accepts the port when it was actually named.
5. The sole **gateway-owned run-marker**. A running gateway records
   `<data-home>/run/gateway-<port>.bin` (see
   `kiro_crew.instances.run_marker`, written for the SSH token-mint), so its
   filename already advertises the port. A client with nothing configured reads
   the marker names — never the file contents — and uses that port. Two guards
   keep it from being a guess:
   - **Ownership, not reachability** — `clear_marker()` only runs on graceful
     shutdown, so a crash leaves a stale marker behind and an unrelated process
     may since have bound that port. Because `_token` / `_logout` send
     `X-Local-Secret` to whatever answers, a bare "is something listening" probe
     would walk the local secret into that process. A command-line check is not
     enough either — argv is attacker-chosen, so a listener started as
     `/tmp/kirocrew gateway` would pass it. `_gateway_owns_port()` therefore
     requires three things, none sufficient alone:
     1. the pid recorded in `run/gateway-<port>.pid` (written `0600` inside the
        `0700` `run/` dir, which is on the `is_sensitive_path` floor, so neither
        another local user nor an agent file tool can write it);
     2. that pid must be among the pids listening on the port
        (`platform_compat.find_listening_pids`), which is what makes a stale
        recorded pid harmless;
     3. that pid must be owned by the calling uid
        (`platform_compat.process_owner_uid`) and look like a KiroCrew process.
        The uid check is what closes pid *recycling* into a foreign user's
        process; argv is retained only as defense in depth.

     It **fails closed** at every step: no sidecar, an unparseable pid, a pid
     that does not hold the port, an unresolvable uid, a missing `lsof` /
     `netstat`, or a throwing lookup all deny, and discovery is skipped. A
     same-user attacker is out of scope by construction — they can already read
     `.local_secret` under their own uid.

     **On non-POSIX platforms the step denies outright.** `process_owner_uid`
     cannot report an owner on Windows, and a `KIROCREW_HOME` writable by another
     user would let them replace both the marker and the sidecar with a forged
     listener — the file-permission argument that carries requirement 1 stops
     holding there. So discovery is skipped rather than approximated: Windows
     users keep `--port` / `KIROCREW_PORT`, exactly where they were before this
     fallback existed, so nothing regresses.
   - **Ambiguity** — with several gateways up there is no basis to pick one, so
     the step refuses, prints the candidate ports and the `--port` /
     `KIROCREW_PORT` hint to stderr, and falls through.
6. `_DEFAULT_PORT` (`5476`).

Steps 3 and 5 are what make a single gateway started on a non-default port
(`kirocrew gateway --port 6776`) reachable from a bare `kirocrew token` with
zero configuration; before it existed, the client hit a dead 5476 while the
marker naming the live gateway sat unread. Config-load, URL-parse (including a
non-string `dashboard.url`, which raises `TypeError` rather than `ValueError`),
and discovery failures all degrade to the next step — a client command never
dies on a bad config or an unreadable data home.

Because `restart` resolves a port and then polls it for readiness, it passes the
resolved port to the detached replacement (`_spawn_detached_gateway(port)`). The
child re-resolves independently, so without that the replacement could bind 5476
while the parent waited on the discovered port.

The marker is written for **every** dashboard-serving gateway, including a
source-tree `python -m kiro_crew` launch with no console script beside
`sys.executable`: in that case the `.bin` file is written empty, which is inert
for the token mint (its shell clause requires a non-empty executable path) but
still advertises the port for discovery. The pid always goes to the separate
`.pid` sidecar — never into the marker, whose contents mint `cat`s and execs. A
`--slack-only` gateway serves no dashboard, so it writes no marker — there is no
client port to discover.

Writing a marker also **prunes** markers naming other ports. A gateway is a
singleton per data home (`gateway.lock`), so any other port's marker is residue
from a run that crashed before `clear_marker()` could fire. Unpruned, they
accumulate one per port ever used and each costs every client command an extra
listener lookup, making discovery slower the longer a dev box churns ports. The
live gateway is the only writer and knows which port is current, so it is the
right place to reap them; pruning is best-effort, and the ownership check still
rejects anything it misses.

CLI→gateway requests are built against the literal `127.0.0.1`, never the name
`localhost`. On a dual-stack host `localhost` can resolve to `::1` first, and the
listener verification is address-agnostic (`lsof -ti TCP:<port>` cannot tell an
IPv6 squatter from the real IPv4 gateway), so a name-based URL could deliver
`X-Local-Secret` to a socket other than the one that was verified. The URL
*printed* for the browser still uses `resolve_dashboard_host()` (`localhost`) —
that must not change, because the SPA's per-origin `localStorage` is keyed on it.

## Stop Command

`kirocrew stop [--port PORT]` stops a running gateway:

1. If a systemd/launchd service is active **and** the caller did not pass
   `--port` explicitly (see Service Management), stop it via the service
   manager and return — without this branch, SIGTERM-by-port would be
   racing the manager's auto-restart.
2. Otherwise (no service active, or `--port` was passed explicitly to
   target a non-default dev gateway): `platform_compat.find_listening_pids(port)`
   to find PIDs — `lsof -ti TCP:{port} -sTCP:LISTEN` on POSIX, `netstat -ano`
   parsing on Windows (there is no `lsof` there; this previously made
   `kirocrew stop` a no-op on Windows). Both binaries are resolved through
   `platform_compat.trusted_system_bin()` — the fixed system directories, never
   `PATH`, which on a gateway can lead with same-uid-writable dirs — and a name
   that does not resolve there counts as absent rather than falling back.
   `listening_pid_tool_available()` performs the same pinned resolution, so it
   distinguishes "no listener" from "lookup tool missing" without disagreeing
   with the lookup it describes. The pinned set is the FHS directories plus
   `/run/current-system/sw/bin`, which is root-owned and rewritten only by a
   system rebuild. A tool installed anywhere else — a Homebrew or conda prefix —
   still reads as absent, and deliberately so: those prefixes are writable by the
   invoking user, which is the exposure the pin exists to close.
   `trusted_system_bin()` logs a warning once per name when the tool is on
   `PATH` but not resolvable under the pin, and `tool_outside_trusted_dirs()`
   lets `stop` name where the tool actually is rather than tell an operator who
   already has it to install it. That case carries SEL
   `reason=<tool>_outside_trusted_dirs`, distinct from `<tool>_not_found`, so
   the two are separable in the audit log.
   When the trusted lookup tool exists but returns no pid, stop makes one
   fixed-loopback `POST /api/shutdown` request carrying the gateway generation's
   local secret. That handler independently requires loopback origin and a
   constant-time secret match, then triggers the same graceful shutdown event as
   SIGTERM. The acknowledgement body is capped at 4 KiB before JSON parsing;
   excessive nesting is treated as malformed input. A successful acknowledgement
   ends the stop command; a missing secret, refusal, malformed response, or
   transport failure retains the ordinary no-gateway diagnostic. This fallback never converts the pid sidecar into
   authority to signal a process.
3. `platform_compat.process_command_line(pid)` to verify it's a KiroCrew process —
   `/proc/<pid>/cmdline` (Linux), `ps -o command=` (macOS), `Win32_Process.CommandLine`
   via WMI (Windows). The Windows venv `kirocrew.exe` re-execs `python.exe`, so the
   match is on the command line (`-m kiro_crew gateway` / `\Scripts\kirocrew.exe gateway`),
   not the image name.
4. Terminate each verified PID: `os.kill(SIGTERM)` on POSIX; `taskkill /T /F`
   (via `platform_compat.kill_process_tree`) on Windows so the gateway's detached
   children are reaped too. Liveness is probed with `platform_compat.pid_exists`
   (a raw `os.kill(pid, 0)` would *terminate* the process on Windows).
5. Waits up to 1s for exit.
6. SEL audit event logged.

## Restart Command

`kirocrew restart [--port PORT]` restarts a running gateway. Mirrors
`stop`'s service-aware structure:

1. If a systemd/launchd service is active **and** the caller did not
   pass `--port` explicitly, ask the platform to restart it. On Linux:
   `sudo systemctl restart kirocrew.service` (single
   atomic operation, smaller down-window than stop+start, and the
   supervisor stays in charge of the lifecycle the whole time). On
   macOS: `launchctl unload <plist>` + `launchctl load <plist>` (no
   `-w`, so persistent enable state is unchanged). The deprecated
   `launchctl restart` is avoided because under `KeepAlive` it behaves
   like `stop` (SIGTERM + immediate respawn) and never re-reads the plist.
2. Otherwise (foreground gateway, no service, or `--port` passed
   explicitly to target a non-default dev gateway):
   - `platform_compat.find_listening_pids(port)` (lsof on POSIX, netstat
     on Windows) to detect a running gateway. If found — OR if the lookup
     tool is absent (`not listening_pid_tool_available()`, so a missing
     tool is not mistaken for a dead gateway) — run the existing `_stop`
     path. When the trusted lookup tool exists but returns no pid, restart
     independently attempts the authenticated shutdown request. An acknowledged
     request always refuses the immediate replacement and tells the operator to
     retry after shutdown completes: neither an absent pid sidecar nor a live
     same-user pid from that sidecar can prove singleton-lock release, because a
     stale pid may be recycled and exit before the incumbent. The second restart
     sees no incumbent and safely starts the replacement.
   - If no incumbent evidence exists (e.g. the user runs `restart` after a
     crash), skip the stop step rather than erroring — the user expects to end
     up with a running gateway either way. The `_stop` call is wrapped in a
     `try / except SystemExit` so a TOCTOU race (gateway exits between the
     listener check and `_stop`'s own lookup → `_stop` calls `sys.exit(1)`)
     does not abort the restart before the spawn.
   - Spawn a detached `kirocrew gateway` via `subprocess.Popen`, stdin set
     to `subprocess.DEVNULL`, and stdout + stderr redirected to
     `~/.kiro/crew/gateway.log` (the same file the `kirocrew logs` command
     tails for foreground gateways). Detach is per-platform: POSIX uses
     `start_new_session=True`; Windows uses `creationflags=DETACHED_PROCESS
     | CREATE_NEW_PROCESS_GROUP` (there is no setsid) — both via
     `platform_compat`. The shell returns immediately and the user can
     follow logs via `kirocrew logs -f`.
3. SEL audit event logged with `via=service` or `via=fork pid=<n>` so
   the audit trail distinguishes the two paths.

## Service Management

`kirocrew service {install,uninstall,status}` registers the gateway
with the OS service manager so it survives SSH disconnects, restarts
on crash, and starts on boot. Implemented in `src/kiro_crew/service/`.

- **Linux** (`current_platform() == SYSTEMD`):
  - Unit file: `/etc/systemd/system/kirocrew.service` (root-owned).
  - Install: `sudo install` writes the unit, then `sudo systemctl
    daemon-reload && sudo systemctl enable --now kirocrew.service`.
    Privilege is resolved per call: already-root (euid 0) skips `sudo`
    entirely — required on minimal container / `root`-login images that
    ship no `sudo` binary — and a non-root caller with no `sudo` fails
    with a clear `ServiceInstallError` rather than an uncaught
    `FileNotFoundError`.
  - The gateway runs as `User=$USER Group=$(id -gn)`. Every elevated
    executable is a stock system program, not a kirocrew one; the module
    docstring in `service/linux.py` sits next to the call sites, names the
    current set, and records what escalating the AppArmor step's
    interpreter does and does not guarantee. [security](security.md)
    carries the reasoning behind that step's four tools.
  - **Environment**: values are captured from the installer's environment
    into the unit's `Environment=` lines at install time
    (`service_environment()` in `service/common.py`) — this is how
    `KIROCREW_PORT=5477 kirocrew service install` binds a non-default port.
    The unit also reads `EnvironmentFile=-/etc/kirocrew/kirocrew.env`, an
    operator-editable file the installer seeds create-if-absent (a reinstall
    never clobbers edits). systemd applies the file AFTER — and overriding —
    the baked `Environment=` lines, so editing it and running `sudo systemctl
    restart kirocrew` changes a value (e.g. the port) without reinstalling.
    Uninstall removes the file and its `/etc/kirocrew` directory.
  - **Credentials are deliberately NOT captured.** Both baked locations are
    world-readable — the unit lives in root-owned `/etc/systemd/system` and the
    override file is installed `0644` — so a model credential placed there
    would be readable by every local user on the host. `service_environment()`
    therefore carries no credential — its only installer-derived values are
    `PATH`, `KIROCREW_KIRO_BIN` and `KIROCREW_PORT` (it also returns `HOME`,
    `LANG` and `LC_ALL`) — and a test pins the absence so a future "just
    propagate it" change fails.
    Consequence: a `KIRO_API_KEY` exported in the installing shell does not
    reach the service, the readiness probe (which forwards that variable from
    the *gateway's own* environment) sees no credential, and unless a
    `kiro-cli login` credential store under the baked `HOME` supplies one
    instead, the dashboard reports a signed-out state on a host where `kiro-cli`
    itself is authenticated. `install_service()` prints a warning naming the
    variable and the remedy when it detects that case, and `~/.kiro/crew/.env`
    is the supported home — `load_credentials()` reads every key from that file
    into the gateway environment at boot and forces `0600` on it first. The
    warning is diagnostic only: it is non-fatal by construction, since the unit
    is already written and started by the time it runs. `kirocrew doctor` reports
    the same condition next to its `kiro login` line — the one output where the
    contradiction is visible, since that line runs `whoami` with the inherited
    environment and reports signed in. Doctor's report is gated on a service
    definition existing (`installed_unit_path()`): without one the gateway runs
    in the foreground and inherits the invoking shell, so the credential does
    reach it and a warning would be a false positive. It is **advisory only** —
    never appended to doctor's `issues`, which is the exit-code channel — since
    that gate establishes a definition on disk, not that the serving gateway
    lacks a credential; a fall-back login store, or a stopped unit beside a
    foreground `kirocrew gateway`, both leave the host healthy while the check
    fires.
  - Boot survival via `WantedBy=multi-user.target` (no linger needed —
    that's a user-service concept; this is system-level).
  - Crash-loop safety: `StartLimitBurst=3 StartLimitIntervalSec=300`.
  - Logs are read from the journal: `sudo journalctl -u kirocrew -f`,
    or unprivileged if the user is in `systemd-journal` / `adm`.
- **macOS** (`current_platform() == LAUNCHD`):
  - Plist: `~/Library/LaunchAgents/dev.kirocrew.gateway.plist`
  - Install: `launchctl load -w <plist>`. `RunAtLoad=true` and
    `KeepAlive` ensure auto-start and crash recovery.
  - Stdout and stderr are written to
    `~/Library/Logs/KiroCrew/gateway.{log,err}`.
- **Other platforms**: install/uninstall return exit code 2 with a
  message pointing to manual setup.

`kirocrew stop` is service-aware: if the service is active it calls
the platform's stop instead of SIGTERM, so the manager does not
immediately restart the gateway under us.

## Logs Command

`kirocrew logs [-n LINES] [-f]` tails the gateway log from whichever
source is most appropriate:

1. systemd journal if the system service is installed on Linux. Tries
   unprivileged `journalctl` first; falls back to `sudo journalctl`
   only if the unprivileged probe returns no rows.
2. launchd stdout file if a plist exists on macOS and that file is
   non-empty. Both conditions matter: the platform probe reports launchd
   on any macOS host, and an install that never started the agent leaves
   a 0-byte log behind, so either check alone would capture the command
   and tail nothing.
3. `~/.kiro/crew/gateway.log` for foreground gateways

Uses `os.execvp` so signals (Ctrl+C) propagate naturally to the
underlying `journalctl`/`tail` process.

## Dashboard Self-Update

On gateway startup and every 12 hours, a background task runs `git fetch` and
reports an update when EITHER the checkout can be FAST-FORWARDED
(`git rev-list --count --left-right HEAD...@{u}` shows commits behind and none
ahead) OR the pull already landed and only a restart is missing (`HEAD` equals
its upstream while the on-disk `__version__` outranks the one this process
imported).

Commit distance is the primary signal because `__version__` is bumped only at a
release: comparing version strings alone reported "you're on the latest version"
to a checkout hundreds of commits behind `main`, for as long as the next bump
took.

Both conditions are narrower than "is it behind", because `available` is also
read by an unattended apply. `GatewayOrchestrator._auto_apply_update` applies
`git fetch` + `git reset --hard origin/<branch>` with no prompt, so:

- "Behind" alone is true both for a checkout that is purely behind and for a
  DIVERGED one carrying its own commits, and the second would have those commits
  reset away. Only a fast-forwardable checkout is offered an update.
- The version signal is required to come with `HEAD == @{u}`. A checkout that
  pulled a version bump and then committed on top is ahead, so its upstream
  still reads newer than the imported version; without that requirement the
  same reset would drop those commits.

**The unattended apply is triggered by the version, not by commit distance.**
The check reports `version_newer` beside `available`, and the git branch of the
`auto_update` path requires both. `available` is what the dashboard shows, and
on commit distance alone that would mean any upstream commit — resetting a
source checkout within 12 hours of one, where the version-only verdict only did
so at a release. Requiring both keeps that path firing no more often than
before. Commit distance without a version bump lights the dashboard badge, and
`POST /api/update` (`git pull`, dirty tree refused with 409) is the
non-destructive way to apply it.

- Topbar shows `📦 v0.1.3` badge — click to check and view changelog
- If newer version found: badge turns into "📦 Update Available"
- Clicking opens a dismissible changelog modal with rendered markdown
- "Update Now" button: `git pull` → rebuild → `os.execv()` restart
- Health indicator shows "Updating…" during the process
- SSE auto-reconnects when the new process starts

## Status Command

`kirocrew status` queries the running gateway's `/api/status` endpoint
and prints uptime, sessions, messages, tool calls, subagents, crons, lessons.

## App Dev Mode

`kirocrew app dev <name> [--off] [--confirm-out-of-install-root]` toggles an
installed App Kit app into (or, with `--off`, out of) **dev mode**, which speeds
the app-UI edit loop by serving UI files uncached and live-reloading the
dashboard on file change. The command writes the flag out-of-process; the
running gateway's watcher picks it up within one poll interval, so no gateway
restart is needed. Full App Kit developer docs live in
`docs/app-kit/api-reference.md`; the durable contract surfaces this feature
introduces are:

- **Persisted schema — `installed.json` `dev: bool`** (default `false`): a
  per-app flag in each app's `~/.kiro/crew/apps/<name>/installed.json`. Tolerant
  on read (absent ⇒ `false`), reversible, no migration. This field is the
  authoritative source of truth for **watching and no-store serving**; enabling
  also records a gateway-owned **operator grant** binding the ui root's resolved
  path, which is what authorizes serving a ui root outside the install directory
  (see api-reference.md). Builtin apps cannot enter dev mode.
- **Endpoint — `POST /api/apps/{name}/dev`**, body `{"enabled": <bool>}`,
  returns `{"name": <name>, "dev": <bool>}`. Behind standard gateway auth;
  emits an `app_dev_mode` SEL audit event. `400` for a non-boolean body, a
  builtin app, an unsafe app name, or a refused grant (a sensitive ui root is
  never grantable; an out-of-install root always answers `400` over HTTP —
  only the CLI flag, run on the gateway host, supplies the confirmation,
  because a request-body flag from the dashboard origin is app-controllable);
  `404` when the app is not installed. The flag is additionally operator-only
  against agent shells through three tiers: the builtin rule
  `self-protection-dev-mode-out-of-root-confirm` (literal text plus its argv
  floor) refuses any agent shell command carrying it; the flag's consumption
  point performs a runtime human-vs-agent check that refuses a process
  showing evidence of agent-shell confinement
  (`dev_mode_operator_attestation_required`) — closing runtime flag
  synthesis, which no command-text scan can see; and the grant record
  (`~/.kiro/crew/apps/.dev-grants.json`) is sealed read-only inside the agent
  OS sandbox
  (materialized at gateway startup so the seal always has a target), so a
  sandboxed process cannot write a grant at all — any grant-touching toggle
  from such a process is refused up front
  (`dev_mode_grant_record_readonly`; use the dashboard toggle instead). Only
  the operator's own terminal can supply the attestation — and both the
  confirmed grant and each refusal emit a SEL event
  (`dev_mode_out_of_install_grant` for the permission decision,
  `dev_mode_grant_write` for the sealed-record refusal).
- **WebSocket event — `app_reload`**, payload `{"app": <name>, "ts": <float>}`,
  broadcast when a dev-mode app's `ui/` tree changes; the dashboard reloads that
  app so edits appear immediately.
- **Serving behavior:** while an app is in dev mode the gateway serves its UI
  with `Cache-Control: no-store`; otherwise the standard revalidation header
  applies.

An internal, unstable sentinel cache under `~/.kiro/crew/apps/` mirrors the set of
dev-mode apps so the zero-dev-apps steady state costs one `stat()` per second.
It is a derived cache reconciled from `installed.json` at watcher init (under a
cross-process lock, atomic with concurrent toggles), **not** part of the App Kit
contract — its path and format are internal and may change without notice.

## Computer Use Commands

`kirocrew computer {doctor [--json] | apps | call}` — hand-rolled dispatch
rather than argparse subparsers, because the parent CLI forwards `REMAINDER`
(see [computer-use.md](computer-use.md)).

**`doctor`** reports, in order: whether the platform is supported (macOS today;
Windows and Linux report a typed refusal), whether the keystone primary enable at
`~/.kiro/crew/computer_use.json` is on, and the macOS TCC probe
(`AXIsProcessTrusted()` + `CGPreflightScreenCaptureAccess()`). The probe is
**advisory and never a gate**: macOS attributes a grant to the *responsible
parent* of the process tree, so both rows can read `missing` while a
full-fidelity capture succeeds — observed live. `doctor` therefore prints a
`responsible_hint` naming the process a user should actually grant (the packaged
app, or the terminal that launched a dev gateway) and says outright that "not
detected" does not always mean unavailable. It never calls
`CGRequestScreenCaptureAccess`, which would pop a system dialog from a background
process.

`--json` is the machine form the **gateway shells out to** for the Settings
permission rows. That indirection is deliberate: a short-lived subprocess keeps
native ctypes out of the gateway, so a native fault cannot take down the gateway
and with it cron, Slack and the dashboard WebSocket.

**`apps`** lists on-screen applications resolved from
`CGWindowListCopyWindowInfo` (layer-0 windows only, never `pgrep` — a `pgrep -n`
lookup returns short-lived helper pids whose accessibility tree is empty). It runs
`computer_list_apps` through the SAME gated dispatcher as `call`, so it is refused
while the feature is disabled, in an unattended session, or under a policy that bans
computer use — the agent can run this command with bash, so an ungated version was
an unauthorized read of every window title.

**`call`** runs one tool — `call computer_get_state app=Finder` — or a whole
sequence in ONE process: `call --calls '[{"tool":"computer_get_state","args":
{"app":"Finder"}},{"tool":"computer_click","args":{"app":"Finder",
"element_index":12}}]'`. The batch form exists because `element_index` values only
resolve against the per-process snapshot cache that produced them, so two separate
invocations cannot share them. `key=value` arguments are JSON-decoded when they can
be (`element_index=3` → int, `screenshot=false` → bool) and kept as text otherwise
(`app=Finder`). `--json` emits `[{tool, text}, …]`; the exit code is non-zero if any
reply carries the `Error: ` prefix, and a batch runs to completion rather than
aborting at the first refusal.

`call` goes through `computer_use.tools.dispatch_tool`, the **same** chokepoint an
agent call traverses, so the primary enable, the target policy and the secure-field
floors all apply — it is a reproduction tool, not a bypass. Its session key is the
attended `cli_chat` surface, which is what the SEL audit records. There is no
separate diagnostics opt-in and no identity proof: the unattended-surface refusal
that made one necessary was removed along with the rest of the computer-use
governance model.

All three are **human-facing**. `apps` has an MCP twin (`computer_list_apps`) per
the MCP-first rule; `doctor` is a permission diagnostic rather than a capability,
so the rule does not bind it; and `call` adds no capability at all — it is a
harness over the eleven existing MCP tools, and deliberately has **no** MCP twin,
because a tool that runs other tools would let a model launder one per-call gate
decision into many. There is deliberately **no** `kirocrew computer state <app>` —
that would be a second, CLI-shaped spelling of an LLM-facing capability and would
have to be an MCP tool instead (it is: `computer_get_state`).

## Gateway Test Harness

Four composable flags let an integration test or eval harness boot a gateway
deterministically, with no model and no developer-machine state:

```bash
kirocrew gateway --test-mode          # bundle: ephemeral port + json-ready + reads approval
kirocrew gateway --port auto          # OS-assigned port, avoiding a collision with a real gateway
kirocrew gateway --json-ready         # print KIROCREW_READY:{port,token,pid,home} once listening
kirocrew gateway --approval reads     # auto-approve read-only tools
kirocrew gateway --approval yolo      # auto-approve ALL tools
```

`--json-ready` is what makes the harness race-free: the caller waits for the
`KIROCREW_READY` line instead of polling a port, and reads the token from it rather
than minting one.

**`--approval yolo` refuses to start unless `KIROCREW_HOME` is explicitly set to a
non-default path.** The flag disables every per-call approval, so pointing it at the
real data home would let a test drive an operator's live sessions and credentials.
The guard is a startup refusal rather than a warning because a warning in CI output
is not read.
