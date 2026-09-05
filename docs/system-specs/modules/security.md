# Security Module

## Overview

KiroCrew implements defense-in-depth security across multiple layers: OS-level process isolation, credential path protection, input/output validation, authentication, authorization, and audit logging. This document consolidates all security controls and the vulnerabilities they address.

## Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| XPIA credential theft | LLM reads `~/.aws`, `~/.ssh` via `fs_read` or `cat` | Hook-layer path blocking + OS sandbox |
| XPIA data exfiltration | LLM embeds secrets in URLs posted to a chat channel or the dashboard | Output scanning + URL redaction |
| Cross-origin WebSocket hijack | Malicious page connects to `ws://127.0.0.1:5476/api/ws` | Origin header validation |
| Cross-origin mutation (CSRF) | Malicious page POSTs to dashboard API | Origin/Referer validation on non-safe methods |
| DNS rebinding | Attacker domain resolves to `127.0.0.1`; browser sends forged `Host` to the loopback-bound dashboard (incl. GET exfil) | `Host`-header allowlist validation on every method (`check_host` / `host_validation_middleware`), deny-by-default, 403 + SEL audit. Sole exemption: the three `PROBE_PATHS` liveness probes (orchestrators address containers by IP); their handlers strip identity fields via a second `check_host` gate, leaking nothing beyond TCP reachability |
| Unauthenticated remote access | Dashboard bound to `0.0.0.0` | Loopback-only by default (`127.0.0.1`); when user opts in via `dashboard.url`, token auth middleware requires HMAC-SHA256 signed, IP-pinned, single-use tokens on every request |
| Unauthenticated remote access (AEA tunnel) | `tunnel.enabled` exposes dashboard via public HTTPS URL | Double auth: Tunnels validates Midway OIDC at edge + KiroCrew token auth middleware. Security gate refuses tunnel start without token auth active. Owner-only access (Tunnels restricts by username). SEL audit on connect/disconnect/denial |
| Published surface on a throwaway instance | A scratch instance (Dev Fleet pod) boots a gateway that can publish a tunnel, becoming reachable off the host and contending with a real gateway's registration. A seeded `tunnel.enabled=False` does not hold: it is written once at HOME creation and anything composing config later can flip it back | `--no-tunnel` boot flag pins "never publish" for the life of the process (`tunnel.set_publish_disabled`, read via `publish_disabled()`), consulted by BOTH doors out — `setup_tunnel` at boot, ahead of the token-auth gate and before any `TunnelManager` is constructed, and the on-demand provisioning in `slack.allowlist`, which bypasses `setup_tunnel` entirely. Config is deliberately not consulted: the flag is process state, so no file — including a `config.local.json` overlay — can turn it back on. A pod passes the flag on every exec whose target checkout declares it; the control plane builds the argv but the target worktree's gateway executes it, so `target_supports_flag` probes that checkout first and DROPS the flag when absent, because handing it to a gateway that predates it exits argparse 2 and `Restart=on-failure`/`RestartSec=5` (no `RestartPreventExitStatus`) makes that a 5s restart loop. SCOPE: such a checkout does NOT receive this guarantee — it keeps its pre-flag tunnel behaviour, which is a non-regression rather than a fix, and no config-side substitute is attempted. Re-pinning `tunnel.enabled=False` in the pod config was tried and withdrawn: `KiroCrewConfig.load()` deep-merges `config.local.json` OVER `config.json` with the overlay winning and `config set` writes the overlay by default, so pinning one file does not pin the setting; and the gateway's enable test is an OR (`cfg.tunnel.enabled or current_context().tunnel.enabled()`) whose provider half no config file reaches. Pinning `KIROCREW_PROFILE=standalone` to reach that half is also refused — the profile selects the whole `PlatformContext` including the Level-1 governance ceiling, so it would skip an administrator's policy. Every refusal is SEL-audited — `tunnel.start_denied` at boot and `tunnel.provision_denied` on the on-demand path, both with `resources=no_tunnel_boot_flag` — and `/api/tunnel/status` reports `reason: "boot_flag"` |
| Unauthorized dashboard access | No auth on localhost | Token auth middleware on all requests (loopback bypass removed); file-based IPC secret for internal paths |
| Non-owner channel interaction | Any workspace/server member clicks YOLO/approve buttons | 5-layer owner verification |
| Fail-open owner lock | `KIROCREW_OWNER_ID` unset → no check | Deny-by-default: refuse connect + reject messages |
| MCP input injection | Malformed/oversized tool inputs from LLM | Centralized schema validation (`validation.py`) |
| MCP response DoS | Unbounded tool output fills memory | Response truncation at 100K |
| Destructive CLI commands | LLM runs `rm -rf /`, `git push --force` | Built-in denied-command rules (`BUILTIN_DENIED_RULES`, default-on / user-disableable) enforced at the hooks PreToolUse gate + governance `commands` force-deny (enterprise, un-opt-out-able) + 55 suspicious bash patterns with per-segment matching (`security.py`) |
| Frontend XSS | `dangerouslySetInnerHTML` with unsanitized content | DOMPurify + safe DOM APIs + Mermaid `securityLevel: 'strict'` (iframe sandbox) |
| Widget postMessage forged turn | LLM-emitted `<script>` in a sandboxed `<mcwidget>` iframe calls `parent.postMessage({type:'mc-widget-action'})`, bypassing the in-iframe `isTrusted` click guard | Frontend requires a human gesture: a widget action only PRE-FILLS the composer (never auto-submits) and tags the resulting user-initiated send `meta.origin='widget'`. Backend deny-by-default: `api_chat` refuses the sole chat-text-reachable privilege escalation — orchestrator `go`/`go all` auto-run — for `origin='widget'` turns (SEL `auto_run_denied`), letting the text fall through to a normal fully-gated turn. Mode changes and tool approvals are on separate endpoints the iframe cannot reach |
| YOLO mode abuse | Unbounded auto-approve window | Time-limited safety override: one ad-hoc duration for every surface (`agent.yolo_duration`, default 6h, hard ceiling 24h); the declared config grant is governed separately. Re-auth required after expiry. SEL audit on every lifecycle event |
| Trust reads bypass | Read-only command classification tricked into approving writes | Deny-by-default: rejects redirections, command substitutions, newline separator bypasses. Prefix matching only |
| Port-forward auth bypass | socat/ssh -R makes remote traffic appear as 127.0.0.1 | Loopback bypass removed; all requests require token auth. File-based IPC secret for internal paths |
| Observe-mode context poisoning | Non-owner messages in shared channels influence LLM context | `channel_history.push` gated on `_user_authorized` |
| Outbound data exfiltration | LLM exfils data via `curl -d @file`, `nc < file` | Data-egress/reverse-shell command shapes (`_BASH_EXFIL_PATTERNS` / `audit_bash_exfiltration()`) are **denied at the tool-invocation gate** (`hooks.on_tool_call` + `mcp_cron`), not only advisory-audited (commit 5682f92b); + `redact_exfiltration_urls()` on output. #7365 closed the long-form file-body gaps: curl multipart `=`-joined spellings (`--form="k=@f"`, `-Fk=@f`), the same spellings with a quoted or array/nested field name (`-F'files[x][content]=@f'` — the name is any non-space run, so `[`/`]` cannot slip the gate), and gh's `--input <path>` / `--field k=@path` (scoped to a `gh api`/`repo`/`release` invocation, so curl/wget and bare-`gh` commands do not false-positive; `--input -` and `--input=-` stdin and the never-file-reading `--data-raw` / `--form-string` stay allowed, while a longer dash-leading value is a path and stays denied). Each flag's separator owns exactly one whitespace run — no two `\s*` straddle an optional `=`, so a flag followed by a long whitespace run is rejected in linear time instead of backtracking quadratically on the event loop. The review round closed four gaps that first cut left: both rules also match the quote-normalized per-segment argv view, so a re-spelling the shell resolves (`--in''put`) denies like the plain spelling; the `=`-joined multipart rule anchors on a `curl`/`gh` invocation so another program's `-Fk=@f` (`echo -Fk=@f`) is no longer a false positive; `--input`'s stdin exclusion covers the quoted lone-dash spellings (`"-"`, `'-'`, `--input="-"`); and the gh anchor's span to the flag is tempered at the next `gh api`/`repo`/`release` (the multipart anchor's span at the next `curl`/`gh`) so repeated anchors reject linearly — a plain `.*` retrying from every anchor measured ~19 s of event-loop CPU on 144 KB, past the gateway watchdog |
| Credential file permissions | `.env` readable by group/other | `chmod 600` enforced at credential load time + setup wizard |
| SEL event forwarding leaks | Forwarded audit events contain raw credentials | `redact()` applied to all string fields before callback |
| Foreign-agent import widens trust | A local Codex/Claude Code/OpenClaw/Hermes (or edition-registered) config contains credentials, hooks, personas, instructions, unsafe paths, or permissive runtime/security settings | Authenticated scan/apply/state APIs + registry-validated source ids and a fixed category catalog + secret-free projections + native destination validation + merge-only writes; unsupported/secret items are reported, source trees remain untouched, and governance cannot be imported or widened |
| Unsigned/unadmitted app install | Malicious app installs/registers via CLI, registry, or `POST /api/apps/register` with no admission control (`register_external_app` writes `enabled=True`) | Contained App Kit admission gate (`apps/admission.py`) on install/update/enable/register/registry — kill-switch `banned` (always wins) + `approved` allowlist + optional HMAC `require_signature`, fail-closed on an unreadable `app_admission.json`; absent policy admits (interim default) |
| Implicit third-party app execution | An installed app reaches Python hooks, backend spawn, lifecycle/install shell scripts, or `openCommand` without explicit operator consent; a disabled app invokes `openCommand` | Central `apps/execution.py` decision defaults deny, accepts only JSON boolean `agent.apps_allow_third_party=true`, exempts positively identified builtins, fails closed on config errors, gates every execution chokepoint before side effects, requires enabled state for open, and SEL-audits denials |
| App manifest path traversal | `backend.entryPoint`/`agents`/`skills`/`sops`/`ui.entry` uses `..` or an absolute path to escape the app root | `AppManifest.validate(app_root=...)` canonical containment (resolve + `is_relative_to`) + absolute-path rejection at install/discovery; runtime backstop in `apps/backend.py` rejects an `entryPoint` that resolves outside the app root at boot |
| App over-privilege (advisory-only manifest model) | Malicious/buggy app exceeds its declared manifest `permissions` (extra `mcpTools`, `network`, `shared` memory) | **Advisory today** — `apps/permissions.py:validate_permissions`/`format_permissions_summary` are unwired (only exercised by tests), `check_tool_permission` fails open on empty allowlist; real confinement is the HTTP app-token scope (`token_auth.py`, CWE-269) + OS sandbox, plus the `agent.apps_allow_third_party` off-switch; in-process capability gating tracked in `app-sandbox-roadmap.md` (TRACKING) |
| App workflow-library escalation | An app allowlisted for `/api/workflows` plants protected executable definitions, revises them, or supplies another session's key for saved-run result injection | Definition create/update require a positive dashboard-user claim; saved-definition run rejects non-empty app claims before reading `X-Session-Key`; every denial is SEL-audited |
| Plaintext-transport registry MITM (CWE-319) | A federated registry added over `http://` lets a network attacker swap the fetched index + app manifests, whose setup code later runs with gateway privileges (signatures optional by default) | `_SAFE_HTTPS_URL_RE` in `apps/routes.py` accepts **`https://` only** (plaintext `http://` rejected); private remotes use an explicit `ssh://`/scp form. `POST /api/apps/registries` validates every `repo` through `_is_safe_repo_identifier` (bare name **or** vetted git URL — shell metacharacters / traversal / owner/repo shorthand rejected) |
| Registry-index SSRF via injected clone host (CWE-918) | An untrusted external registry index lists an app whose `repo` points at a loopback/internal address (e.g. `https://127.0.0.1:8443/x`); the App Store browse/refresh/install path clones automatically, driving `git clone` against the internal network (authenticated backend SSRF; DNS-rebinding-capable) | `is_clone_host_trusted` (`apps/registry.py`) fails **closed**, constraining every URL clone to a **host** in the public-forge ∪ configured-registry trust set, enforced at all three clone chokepoints (`_fetch_git_blob`, `_fetch_app_manifest`, `_git_clone_or_pull`). Gates on hostname not IP → rebinding-proof. Host-level SSRF defense, **not** a supply-chain control (admission/signature gate is the orthogonal second layer) |
| Registry-index path traversal via entry name (CWE-22) | A hostile/typo index entry `name` (`/tmp/victim`, `../../victim`) flows to `app_source_dir(name)` and, on a failed clone, `shutil.rmtree(dest)` on the attacker-selected path | Every index entry name is validated against `KEBAB_RE` during normalization and **dropped before it is cached or listed** (warning-logged only); non-string / non-kebab names never reach a filesystem operation |

## Modules

### OS-Level Sandbox (`sandbox.py`)

Hides credential paths from kiro-cli subprocess tree using platform-native isolation:

- **Linux**: user + mount namespace — `unshare(CLONE_NEWUSER)` → identity UID/GID map → `unshare(CLONE_NEWNS)` → bind-mount empty dirs. Availability is decided **empirically** by `_probe_unshare_once()`, which performs this **exact split sequence** rather than a combined `unshare(NEWUSER|NEWNS)` — see "Linux capability probe mirrors the split sequence" below for why the combined form gives a false positive.
- **macOS**: `sandbox-exec` with Seatbelt profile denying file reads. Backend availability is decided **empirically** by `_probe_sandbox_exec()` (write an `(allow default)` profile, run `sandbox-exec -f <profile>` against a trusted fixed system binary — `/usr/bin/true`, never the user-writable kiro-cli — and require exit 0) — there is **no hard-coded OS-version cutoff**. macOS 26 (Tahoe) is fully supported: Seatbelt is the same kernel subsystem backing App Sandbox/iOS/Chromium and was not removed; an earlier `major >= 26 → return False` gate wrongly disabled a working sandbox and was removed (verified the real profile compiles, runs a sandboxed process, and enforces credential-path denies on macOS 26.5). The `(allow default)` + targeted-deny profile also sidesteps the `(deny default)` sysctl-allowlist pitfall that caused the false "sandbox broken on macOS 26" reports.

#### Sandbox Modes

| Mode | Config value | Hides | Accessible | Env scrub |
|------|-------------|-------|------------|-----------|
| **Standard** | `"auto"` (default) | `.gnupg`, `.gpg`, `.config/gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` | `AWS_SECRET*`, `AWS_SESSION*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GIT_ASKPASS` |
| **Strict** | `"strict"` | All of the above + `.aws`, `.ssh`, `.kube` | Only `~/.ssh/known_hosts` | Same as standard |
| **Off** | `"off"` | Nothing | Everything | Nothing |

**Standard mode** (new default) enables git-over-SSH, AWS CLI via `credential_process`, and kubectl while maintaining OS-level isolation on non-workflow credential stores. Env vars are scrubbed in ALL modes — `credential_process` reads from `~/.aws/config`, not env vars.

**Pooled-backend declared-env forwarding (`mcp_gateway.forward_declared_env`, default ON)** — an agent spec may declare `mcpServers.<name>.env`. Under pooling one backend serves many sessions, so the rewriter expands any `${VAR}`/`${env:VAR}` placeholder the block declares — kiro-cli cannot, because the broker spawns the stub rather than the server — writes the resolved block to a `0600` sidecar, and the stub folds it into the `effective_env_hash` PoolKey dimension. Resolving once at write time keeps that sidecar the single source both the stub's hash and `gatewayd`'s coherence re-check read; an unresolved reference is left as a literal `${VAR}`, matching kiro-cli's expander. Placeholders dereference a **filtered view** of the gateway environment, not the raw one: names matching `is_secret_env_key`, `is_credential_env_key`, or the channel-credential scrub (`scrub_agent_denied_env`) are misses. Agent specs are agent-writable, so without that filter `{"TOKEN": "${env:AWS_SECRET_ACCESS_KEY}"}` would smuggle a credential *value* past the key-name filters below — the dereference view mirrors them, so a value the forwarder would refuse under its own name cannot ride in under another (and channel tokens, which the ACP spawn scrub hides from kiro-cli's own expander, are equally invisible here). With the flag ON, `gatewayd` reads the sidecar at **cold spawn only** and applies the surviving keys, filtered twice:

1. `hashing.non_secret_env` drops `ENV_SCRUB_PREFIXES` (`AWS_SECRET*`, `AWS_SESSION*`, `OAUTH*`). These are excluded from `effective_env_hash` **by design** so a credential rotation does not split the pool — which makes the hash non-injective over them, so two sessions with *different* secret values share one backend and no single value is correct to apply.
2. `manager.is_credential_env_key` drops every `_SENSITIVE_ENV_PREFIXES` match (the broader list above, adding `AWS_ACCESS*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GIT_ASKPASS`), so forwarding can never re-introduce a credential key that `_scrub_sensitive_env` deliberately removed from the daemon environment.

**Per-variable opt-in (`mcp_gateway.pool_identity_env`, default empty)** — an operator may name variables whose value IS part of a shared backend's identity. A named variable survives filter (1) *because* it is folded into `effective_env_hash`: the hash becomes injective over it, two sessions declaring different values get different backends, and "no single value is correct" stops being true for that key — so forwarding it is safe by the same argument that already licenses every other hashed key. It does not lift filter (2); `rewriter.pool_identity_env_keys` drops credential-scrub names at the source so a name can never sit in the identity while the forwarder still refuses it. The cost is the one the exclusion was avoiding: rotating a named value re-partitions that server's pool, so the next session cold-starts a backend. Naming nothing computes byte-identical hashes to before the setting existed, so no existing `PoolKey` is invalidated on upgrade. Because it selects the rewrite's output it is part of the rewrite fingerprint. **The stub's copy carries no authority**: the resolved names are passed on stub argv (names only — values stay in the `0600` sidecar) purely so the stub can compute the same hash, and `gatewayd` recomputes under the operator's own configured set, so a stub claiming a wider set produces a hash the daemon does not reproduce and the coherence gate below forwards nothing. Widening what may reach a shared backend therefore stays an operator decision, enforced by the gate that already existed rather than by a new check.

The forwarded set is therefore a **strict subset** of the hashed set — including under the opt-in, which widens both together and so can never widen one alone. Forwarding additionally **verifies coherence at spawn time**: `gatewayd` recomputes `hashing.hash_effective_env` over the sidecar it just read and forwards only when it equals the backend's `PoolKey.effective_env_hash`, skipping forwarding entirely on mismatch. That check is what makes "every forwarded key is part of the PoolKey, so all co-tenants of that backend declared the same value" true rather than merely intended — the stub hashes the sidecar when its session starts, but `gatewayd` re-reads it at cold spawn, so an operator editing `mcpServers.<name>.env` mid-session (with a running stub still holding the old PoolKey, e.g. across an adopted-daemon restart) would otherwise let a crash/idle-reap respawn apply the NEW values under the OLD key. Secret-bearing servers are unaffected: they read credentials from disk (the platform credential helper / the provider's default credential chain, protected per-session by the sandbox bind-mounts), name the variable via `mcp_gateway.pool_identity_env`, or stay unstubbed. The flag fails **closed** — an unreadable config, missing sidecar, malformed sidecar, or hash mismatch all forward nothing.

The flag also gates pooling eligibility at **rewrite time**: with forwarding OFF, an opted-in server that declares a non-empty `env` is left **unwrapped** (no stub) rather than pooled — a shared backend spawned without the env it declares dies at prime on every session, trips the circuit breaker, and degrades through a per-session fallback exec anyway (issue #3495 measured this as a permanent crash-loop for every env-declaring opted-in server). That coupling is why the default is ON: with it OFF, declaring a single ordinary key such as `LOG_LEVEL` forfeits pooling for the whole server, on the strength of a co-tenant disagreement the spawn-time hash check has already ruled out. Turning it off remains the escape hatch for a server that genuinely must not share a backend. The rewriter warns naming the knob; the session launches the server directly with its declared env applied, exactly as if it were not opted in. Because the flag selects the rewrite's OUTPUT, it is part of the rewrite fingerprint — flipping it regenerates the overlays. A declared value carrying a `${VAR}` reference selects the output too, but through the environment rather than a config file, and the environment is not a fingerprinted input: a pass that resolved one is therefore not cached at all and re-resolves on every boot, so a rotated credential cannot keep flowing its old value while no file changes. A placeholder-free spec is unaffected and still served from cache. Similarly, an opted-in server whose bare `command` cannot be resolved on the gateway's search path (the spec `env.PATH`, then the host `PATH` augmented by `env.augmented_path` — the same resolution the MCP probe uses) is left unwrapped instead of being stubbed into a guaranteed-ENOENT pooled spawn: gatewayd runs under the systemd `--user` environment, whose `PATH` lacks the toolbox/user-local bin dirs the session's own exec sees.

**Conditional Python-interpreter env strip** — `PYTHONPATH`, `PYTHONHOME`, and `PYTHONPYCACHEPREFIX` (`_PYTHON_ENV_PREFIXES`) are stripped from official Kiro/ACP child environments by the parent-side `scrub_agent_subprocess_env()` in `AcpClient._spawn()` and `AcpRuntime.spawn()`. The POSIX wrappers also receive `strip_python_env=True`; `/api/models`, `whoami`, and `/usage` apply the same parent helper. Parent enforcement is mandatory on Windows because Kiro's built-in-sandbox delegation returns the raw argv and Windows has no POSIX `env -u` launcher. It also removes `_SENSITIVE_ENV_PREFIXES` and `_AGENT_DENIED_ENV_KEYS`, making wrapped and delegated Kiro spawns inherit one policy. They are deliberately **excluded** from `_SENSITIVE_ENV_PREFIXES` so Kiro Crew's OWN sandboxed Python children (cron scripts, app backends, code-review workers) keep them: they `import kiro_crew` via `PYTHONPATH`, and on the packaged app they must keep writing bytecode outside the signed bundle via `PYTHONPYCACHEPREFIX`. Rationale per key: Kiro Crew exports `PYTHONPATH` at its own site-packages, and a foreign MCP server bundling its own interpreter/deps would otherwise prepend Kiro Crew's site-packages to `sys.path` and load Kiro Crew's fastmcp/cryptography instead of its own — an ABI collision / init hang. `mcp_gateway/gatewayd.py`'s `env_target_resolver` pops the same `_PYTHON_ENV_PREFIXES` list (not a hand-listed subset) before spawning a pooled MCP backend, for identical reasons — including keeping `PYTHONPYCACHEPREFIX` out of a Python-based pooled backend's env, so it can't mirror its stdlib into the shared bytecode cache either. The scrub sites share one source of truth and cannot drift out of sync again. `PYTHONPYCACHEPREFIX` is exported by the desktop app at `<data home>/cache/pycache` to keep the embedded interpreter's bytecode out of the codesigned bundle; inherited into the agent subtree it makes every foreign interpreter (uv-managed pythons, ephemeral venvs run by the agent's bash) mirror its whole stdlib + site-packages under the crew home instead of writing `__pycache__` beside its own sources, and because each ephemeral root mints a fresh path-keyed mirror the cache grows without bound. What the gateway's own tree still legitimately writes there is bounded by `pycache_gc.prune_pycache` (TTL + total-size cap, run from `session.py`'s periodic sweep at most once per `PYCACHE_GC_INTERVAL_SECS`).

**Scoped user-bus locator forward (`XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS`)** — the cgroup v2 ceiling wraps every agent-influenced spawn in `systemd-run --user --scope` (`cgroup_scope_argv`, see `resource-protection.md`), and `systemd-run --user` needs the user session bus to create the scope. Callers that build the spawn environment from a strict **allowlist** instead of inheriting `os.environ` — `dashboard/handlers/source_providers.py` (`_PROVIDER_BASE_ENV_KEYS`, the authenticated `gh`/`glab` spawns) is the live example — do not carry the two locators, so `systemd-run` exits 1 with `Failed to connect to bus: No medium found` and the wrapped command never execs. `sandboxed_spawn_argv` therefore calls `cgroup_scope_bus_env()` after `scrub_env`, gated on the same `_probe_cgroup_scope()` result that decides whether to wrap at all.

A user-bus address inside the sandbox is an escape vector — it can ask the user systemd manager to start a unit that runs *outside* the namespace — so the forward is **paired with an `env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS` shim placed inside the scope** (immediately after `--`), which drops the locators again before the real command execs. `env` `exec`s in place, so `--scope`'s exec-into semantics, PID tracking, `killpg` and descendant scans are unchanged; it is resolved from an absolute path (`_ENV_BINARY_CANDIDATES`, never a caller-influenced `PATH`); and with no `env` binary the layer **fails closed** — the locators are not forwarded at all, so the wrapper fails loudly rather than handing the child a reachable bus.

The strip is deliberately **scoped to the keys this layer injected**, not applied unconditionally — mirroring why `_PYTHON_ENV_PREFIXES` above is conditional rather than part of `_SENSITIVE_ENV_PREFIXES`. `scrub_env` does not (and never did) strip the bus locators, so callers that inherit `os.environ` — including the kiro-cli agent spawn — already pass them through: sandboxed agent shells legitimately run `systemctl --user` and the `kirocrew pod` CLI, which are bus-dependent. Unconditional stripping would silently remove that capability across every spawn site. **Documented residual:** an inherited-environment child therefore still reaches the user bus, exactly as before this layer existed. Closing that is a separate, wider change; this layer's invariant is only that it never *widens* bus reachability — a caller that had no bus keeps none.

**Fail-closed default when no backend**: when no sandbox backend is available, `wrap_argv()` **raises `RuntimeError`** by default rather than executing the agent unsandboxed — the secure default is to refuse, not degrade. The denial also emits a `denied` SEL tool-invocation event. Running unsandboxed is a deliberate opt-in via `agent.sandbox_allow_unsandboxed_exec=true`. The narrow exception is an explicitly classified official Kiro CLI spawn on Windows: it delegates to Kiro's built-in sandbox, so it is not the generic unsandboxed fallback.

**First-party fixed-argv carve-out**: the opt-in above conflated two decisions on a backend-less host — spawning Kiro Crew's OWN managed MCP servers (`kirocrew-core` / `-cron` / `-computer`, whose full argv is derived by `agent._kirocrew_mcp_invocation()` with no agent/repo/user-config input) and unconfining the `mode="strict"` hostile-input paths (the worktree handler's repo-controlled git `include.path`, Papyrus' crafted-`.tex` chokepoints). `wrap_argv(first_party_fixed_argv=True)` narrows that: a spawn whose caller vouches the argv is package-derived proceeds unconfined **only when ALL of** (1) the flag is set — a reviewed property, structurally ratcheted by `test/test_spawn_audit.py::FIRST_PARTY_SPAWNS` (a new site passing the kwarg without an allowlist entry fails CI); (2) the unavailability class is `no_backend` — a `transient` probe failure still raises (it self-heals and must not buy a bypass) and `foreign_sandbox` still raises (the host's sandbox is fine; the remedy is config, not bypass); (3) no governance `sandbox.min_level` floor is active (`_governance_sandbox_floor_active()`, the same read `_clamp_sandbox_mode` uses — a governed host keeps fail-closing for first-party spawns too). The allowed path applies the standard env scrub (via the trusted absolute-path `env` binary; where none exists — Windows — the chokepoint's `scrub_env` on the child environment is the guarantee), warns loudly once per process, and emits a SEL tool-invocation event with a **distinct third outcome, `unconfined`** (best-effort async write — unlike the rare one-shot `denied`/nested-passthrough audits, this fires per managed probe per discovery cycle on the gateway event loop, where a synchronous critical flush would stall the loop) — deliberately neither `denied` (nothing was refused) nor the nested-passthrough `allowed` (nothing confines the spawn); SEL failure there is log-and-proceed, matching the `mode="off"` delegation precedent. `sandbox_allow_unsandboxed_exec=true` remains a strict superset: with it set, behavior is byte-identical to before for all callers. The only current first-party site is the managed-server MCP probe, and it sets the flag only when the spec's command+args+env **equal** the freshly re-resolved managed invocation and that invocation is a resolved console-script binary (the `python -m kiro_crew` interpreter fallback never qualifies: `-m` prepends the child's CWD to `sys.path`, so an untrusted working directory could shadow the package) (env compared against the package-derived `_managed_mcp_env()`, since a spec-carried `LD_PRELOAD` changes what code runs for the same argv) — a customized command, args, or env under a managed name compares unequal and keeps the full opt-in requirement.

**The default is platform-independent, and the wizard is how the opt-in is discovered.** The fallback is `False` on every platform — deriving it from `sys.platform` would hand every backend-less host (every Windows host) an unconfined spawn that no operator declared, which is exactly the deny-by-default authorization the `mode="strict"` callers depend on (an agent-selected repo's `include.path` reaching `~/.aws/credentials`, a crafted `.tex` typesetting a secret into a PDF). What was wrong was only *discoverability*: the refusal was reachable but the remedy was not. So `kirocrew setup` runs `_setup_sandbox_consent()`, which asks `detect_backend()` — platform knowledge stays with the probe, not the config layer — and on `"none"` states what becomes unconfined (`~/.aws`, `~/.ssh`) and prompts, defaulting to **no**. It writes the key only on an explicit yes; declining, a bare Enter, and a non-interactive EOF all leave the config untouched, so the effective default stays fail-closed. It never re-asks once the key is present in either state.

**Nested-sandbox passthrough**: when `wrap_argv()` is called from a process that is *already inside* a KiroCrew sandbox (script-cron ticks, sandboxed agent children, app backends, pooled MCP servers), it returns the argv unchanged (one-shot info log) instead of trying to wrap again. Nested sandboxing is impossible on **both** backends — the Linux launcher's seccomp-BPF filter denies `unshare`/`setns`, and macOS Seatbelt refuses `sandbox_apply` with EPERM from inside an existing sandbox even under an `(allow default)` outer profile — so a nested wrap would fail with EPERM and the fail-closed `RuntimeError` above would brick **every** in-sandbox MCP spawn (the probe error was raised on each `ctx.call_tool` and silently swallowed by the caller). This is **not** a fail-open path: the outer namespace + seccomp still confine every descendant, so passthrough spawns within the existing isolation boundary. In-sandbox detection is env-marker based and **deny-by-default** (`_inside_kirocrew_sandbox()` / `_IN_SANDBOX_MARKER`): the gate keys **solely** on the explicit, single-purpose `KIROCREW_SANDBOX_ACTIVE=1`, which is exported at exactly two sites, each immediately after that platform's credential-env scrub: the Linux launcher `main()` (at the same site as `KIROCREW_HOST_PID`) and the macOS `env` prefix built by `sandbox_exec_argv()`. It deliberately does **not** key on `KIROCREW_HOST_PID` — that variable is dual-purpose session-identity plumbing, and gating a security-relevant passthrough on a variable set for unrelated reasons would be a latent bypass. No unsandboxed code path sets the marker. The passthrough is SEL-audited on **every** invocation via `log_tool_invocation(outcome="allowed", metadata={"reason": "nested_sandbox_passthrough"}, critical=True)`, mirroring the `denied` event on the fail-closed path so the security decision is tamper-evidently recorded. `critical=True` gives it the same write reliability as the `denied`/`delegated` audits — the event is written **synchronously** after draining the async backlog, so a slow or wedged background writer cannot silently drop passthrough records. It stops short of full audit-or-deny (re-raise on SEL failure) deliberately: unlike `_delegate_to_kiro_internal_sandbox` — which on audit failure falls back to KiroCrew's own seatbelt, an equally-safe audited layer — a nested passthrough has **no** safe alternative (seccomp denies the re-wrap by design), so failing the spawn on a SEL filesystem error would couple every in-sandbox MCP call to SEL health and reintroduce a prior in-sandbox spawn outage. On a hard write failure it therefore logs loudly and proceeds: the child is confined by the outer namespace + seccomp whether or not the record lands.

**Passthrough tier comparison (downgrade detection)**: the marker alone proves *a* Kiro Crew sandbox is active, not *which tier* it was built at, so without a tier record the passthrough is tier-blind: an in-sandbox caller requesting `strict` under a `standard` outer sandbox silently runs at `standard`. Both launcher sites therefore export a companion `KIROCREW_SANDBOX_LEVEL=<standard|cc|strict>` (`_IN_SANDBOX_LEVEL_VAR`) beside the marker, with the same non-droppable placement (after the Linux launcher's env-scrub loop; after the macOS `env -u` flags), and `cli.main()` drops an inherited copy at the same site where it drops the marker itself (a stale ancestor's value would otherwise be read as the active tier). The passthrough resolves the requested mode to a tier via the shared `_mode_to_level()` helper and compares it against the active tier on the `standard(1) < cc(2) < strict(3)` ordinal order; an absent or unrecognized level var (an outer tree launched by an older build) reads as `unknown`, which carries no ordinal claim, so no downgrade can be proven against it, the passthrough is unaffected, and nothing crashes. Every passthrough audit event carries `requested_tier`, `active_tier`, `tier_known`, and `tier_downgrade` in its SEL metadata — `tier_known` separates "proven no downgrade" from "unprovable" — so a downgrade is *visible in the audit log* rather than inferred. On a proven downgrade (`requested > active`) the passthrough additionally emits a per-call `SECURITY:` warning naming both tiers and the executable, and prefixes the returned argv with the requested tier's `env -u` scrub (`_sandbox_env_unset_args` — a delta in practice, since the outer launcher already removed the shared prefixes): the one slice of the stricter tier that IS enforceable without a nested wrap (agent-denied credential env keys a `standard` outer launcher never scrubbed). The `env` binary is resolved only at a trusted absolute path (`_unset_env_argv`); when none exists the scrub is skipped with a loud warning rather than resolving `env` through a PATH this environment controls. It deliberately does **not** fail closed: refusing the downgrade breaks every in-sandbox caller that legitimately requests `strict` from a `standard` app-backend sandbox (Dev Fleet Sync/Provision), and the file-level residual gap is exactly what the audit records.

**macOS marker site and the kernel cross-check**: the macOS seatbelt path previously set **no** marker — it is exported only by the Linux namespace launcher — so the passthrough above did not apply on macOS at all. `_probe_sandbox_exec()` therefore failed whenever KiroCrew was already confined, `detect_backend()` cached that EPERM as `"none"`, and the fail-closed branch rejected **every** spawn with "No OS-level sandbox backend is available on this host" — false on a host whose `sandbox-exec` works unnested, and severe in practice (~40 `MCP probe failed` entries at gateway boot, app backends unable to start, Dev Fleet / Files `git` failing). `sandbox_exec_argv()` now sets `KIROCREW_SANDBOX_ACTIVE=1` in the same `env` prefix that already performs the credential-env scrub, as an assignment placed **after** the `-u` flags so they cannot drop it. Because `_sandbox_env_unset_args()` derives that scrub from the **same** `_SENSITIVE_ENV_PREFIXES` / `_AGENT_DENIED_ENV_KEYS` / `_PYTHON_ENV_PREFIXES` logic as the Linux launcher, a marked process always has an environment KiroCrew already sanitised — which is what makes the passthrough safe for the callers that use `wrap_argv()` directly rather than `sandboxed_spawn_argv()`.

On macOS the marker must additionally **agree with the kernel**, and the two cover each other's blind spot. The marker proves KiroCrew built the outer sandbox and scrubbed the environment on the way in, but an env var alone could be forged or inherited. `_macos_sandbox_state()` asks the kernel directly via `sandbox_check(pid, NULL, SANDBOX_FILTER_NONE)`, which is OS-authoritative and unspoofable but cannot identify *whose* profile is active, so it can never grant the passthrough on its own. It is deliberately **tri-state** rather than boolean: a definite `False` (kernel says not sandboxed) alongside a present marker proves the marker was forged into an unconfined process and **vetoes** the passthrough with a `SECURITY:` warning, whereas `None` (symbol unavailable, ABI change, restricted dyld, or non-darwin) says nothing at all and must **not** retroactively invalidate a marker the Linux path honours unconditionally. The state is `lru_cache`d — a process cannot leave its sandbox.

**Foreign outer sandboxes are still refused.** Reaching the fail-closed branch while the kernel reports this process *is* sandboxed means the confiner is one KiroCrew did **not** build — kiro-cli >= 2.13's own internal seatbelt (see the mutual-exclusion rule above) or an operator-wrapped gateway. Those are refused, because macOS exposes no supported way to identify which profile is active (the path-scoped `sandbox_check` form that could prove the outer profile denies credential reads is variadic and returns `-1` through ctypes on arm64), and the env scrub that makes the marker case safe may never have run. What the detection fixes there is the **diagnosis**: `_inside_macos_sandbox()` lets the error state that the host's sandbox is not broken and point at the config-level remedy — disabling kiro-cli's internal sandbox so KiroCrew's own profile owns isolation, which **keeps** isolation rather than weakening it — instead of the previous claim that the host has no backend, which sent operators hunting for something that was never missing. It deliberately does not steer them to `sandbox_allow_unsandboxed_exec`, which permits unwrapped spawns even when no sandbox confines the process at all.

**No-isolation fallback is loud (SEC-009)**: *on the opted-in path only* (`sandbox_allow_unsandboxed_exec=true`), `wrap_argv()` runs the agent with no isolation (graceful — the host is not bricked) but never degrades silently: it emits a one-shot loud `SECURITY` warning. A second, distinct flag `agent.sandbox_allow_no_isolation=true` (config-modal editable) acknowledges the risk and demotes that message to info level — it governs *log level only*, not whether execution is permitted (that gate is `allow_unsandboxed_exec`).

**macOS sandbox mutual exclusion**: kiro-cli ≥ 2.13 ships an *internal* agent sandbox in the binary itself, toggled by the `"sandbox"` key in `~/.kiro/settings/amazon-internal.json` (the kiro-cli backend's own settings dir — distinct from KiroCrew's data home `~/.kiro/crew`; the filename is the literal kiro-cli ships). Its in-process seatbelt init cannot nest inside KiroCrew's sandbox-exec wrap: the macOS kernel returns EPERM even under an `(allow default)` outer profile, so **exactly one sandbox layer can be active per kiro-cli spawn**. `wrap_argv()` enforces mutual exclusion on macOS: when `kiro_internal_sandbox_enabled()` is true and the spawn is kiro-cli (argv basename, same convention as `_resolve_kiro_bin`), the seatbelt wrap is skipped and kiro's internal sandbox owns isolation (`_delegate_to_kiro_internal_sandbox()`); when it is false, KiroCrew's seatbelt engages as always. Invariants: (1) this is **not** the forbidden silent unsandboxed fallback (SEC-009) — delegation is config-driven and deterministic, never a reaction to a wrap failure; the child still runs under an OS sandbox; the decision is logged loudly once per process and every delegated spawn emits a SEL audit event (`outcome="delegated"`, `critical=True`) on an **audit-or-deny** basis: if the audit event cannot be written, the delegation is refused and the spawn falls back to KiroCrew's own seatbelt (safety over availability while SEL is broken); (2) the env scrub (`_sandbox_env_unset_args`, shared with `sandbox_exec_argv`) is applied identically on the delegated path; (3) only kiro-cli spawns may delegate — all other agent-influenced spawns keep KiroCrew's wrap regardless of the settings file; (4) the settings read routes through `hooks.safe_read_file` (`is_sensitive_path` on the resolved target + `O_NOFOLLOW` — a symlinked settings file pointing at a sensitive path is refused) and fails toward `False` on any failure (absent/malformed/non-dict JSON, refused read, home-resolution failure → KiroCrew's sandbox stays on); it is uncached so a settings flip applies to the next spawn; (5) macOS-only — Linux namespace isolation is unaffected.

**Windows Kiro internal-sandbox delegation**: Kiro Crew has no native Windows OS wrapper, but the official Kiro CLI backend has its own sandbox. `wrap_argv()` therefore delegates before backend detection when, and only when, the reviewed caller passes `is_kiro_cli=True` **and** `kiro_internal_sandbox_enabled()` confirms that sandbox is actually on — the same capability read the macOS rule above uses. Classification alone is not enough: it says the spawn is entitled to delegate, not that a layer exists to delegate to, and a Windows host with Kiro's sandbox disabled would otherwise receive an unwrapped argv while the SEL record claimed a delegated sandbox. A classified spawn whose capability read is `False` falls through to the ordinary Windows no-backend policy and fail-closes. This is a positive capability grant from `ACP_BACKENDS_INTERNAL_SANDBOX`; `_spawns_kiro_cli()` basename inference and `is_kiro_cli=None` never grant it on Windows. The main ACP client/runtime and the three fixed one-shot Kiro reads pass the classification explicitly. Extra Kiro Crew path restrictions (`extra_hidden_dirs` / `extra_visible_dirs`) disable delegation because Kiro's sandbox cannot prove it enforces them. Every delegation is `outcome="delegated"`, `critical=True`; an SEL failure returns to normal Windows no-backend policy and raises. All other ACP backends, scripts, hooks, third-party MCP probes, Papyrus/Polly commands and future unclassified spawns remain fail-closed unless the operator sets `agent.sandbox_allow_unsandboxed_exec=true`. Because Windows cannot prefix `env -u`, every delegated production spawn passes `scrub_agent_subprocess_env()` as its explicit child environment.

**Boot must isolate the fail-closed raise**: because the `RuntimeError` above can fire per-spawn, callers that launch multiple child processes at boot must catch it. `apps/backend.py:start_enabled_app_backends()` wraps each `start_app_backend()` in try/except so one app that cannot be sandboxed (e.g. on macOS 26 where `sandbox-exec` is gone) is logged + `error`-audited + **skipped** (never spawned unsandboxed), and the gateway (Slack + dashboard + every session) still boots — matching the fail-isolated posture of the admission re-vet and MCP reconcile branches in the same loop.

**Why standard is safe**: The hook layer (`is_sensitive_path()`) still blocks direct file reads of `~/.aws/*` and `~/.ssh/*`. Denied commands block `cat`/`head`/`tail`/`python open()` on those paths. `redact_credentials()` catches any credential patterns that leak through tool output. Three independent layers must all be bypassed simultaneously.

Config: `agent.sandbox` in `config.json` — `"auto"` (standard), `"strict"`, or `"off"`.

**Callers must pass the configured tier explicitly.** `wrap_argv`'s `mode` parameter defaults to `"auto"`, which coincides with the shipped `agent.sandbox` default but is **not** the same thing: it ignores what the operator actually configured. Where `agent.sandbox` is an explicit `"off"` (isolation deferred to kiro-cli's internal sandbox), a spawn that omits `mode` requests isolation the operator did not ask for. Explicitly classified Windows Kiro spawns delegate at either tier, but the configured value still keeps every one-shot read from being *stricter* than the long-lived chat session it accompanies across platforms. The interactive ACP spawns thread the value through their `sandbox_mode` constructor argument; one-shot `kiro-cli` reads call `sandbox.configured_sandbox_mode()` (owning module: `sandbox.py`) instead of relying on the default. This is deliberately **not** a change to `wrap_argv`'s own default, which must stay fail-secure for callers that genuinely want a tier independent of config — the prerequisite probes' `strict`, the credential-free registry clones. See `modules/acp-client.md` for the affected sites and the user-visible symptoms.

Wired into `AcpClient._spawn()` — all kiro-cli processes are sandboxed. Parent KiroCrew process is unaffected. Zero new dependencies (stdlib + system binaries only).

**Linux namespace sandbox**: Fork child → child calls `unshare(CLONE_NEWUSER)` → parent writes identity UID/GID map (`uid uid 1` / `gid gid 1`) to `/proc/<child>/{setgroups,uid_map,gid_map}` → child calls `unshare(CLONE_NEWNS)`, sets mount propagation private (`MS_REC|MS_PRIVATE`), bind-mounts empty dirs over credential paths (per mode), scrubs sensitive env vars (`AWS_SECRET*`, `SSH_AUTH_SOCK`, etc.), and execs the agent. Two-pipe synchronization ensures correct ordering. The child retains the real UID/GID so all toolchains (JVM ByteBuddy, Gradle, npm, etc.) work without workarounds. Implemented as a Python launcher script (`_build_launcher_script()`) spawned by `namespace_argv()`.

**Linux capability probe mirrors the split sequence**: `_probe_unshare_once()` performs the *same* fork → `unshare(CLONE_NEWUSER)` → parent-writes-maps → `unshare(CLONE_NEWNS)` handshake as the launcher above, because the two flags do **not** behave identically when combined. A single `unshare(CLONE_NEWUSER | CLONE_NEWNS)` is satisfied atomically and **succeeds** on hosts where the split sequence fails: with Ubuntu's `kernel.apparmor_restrict_unprivileged_userns=1` (default since 23.10, and the discriminator is that **sysctl being 1**, not whether AppArmor is loaded — Debian 13 ships AppArmor and is unaffected), creating a user namespace transitions the process into a restricted AppArmor profile carrying no `CAP_SYS_ADMIN`, so the *second* unshare returns EPERM while the identity map writes succeed. The probe therefore previously reported such hosts as `namespace`-capable and every real spawn died with `sandbox: unshare(NEWNS) failed: errno 1` — verified on Ubuntu 24.04 and 26.04. The probe's `reason` names the failing step (`unshare(CLONE_NEWUSER)` / a `/proc/<pid>/...` map write / `unshare(CLONE_NEWNS)`) so callers can distinguish mechanisms that share an errno: a NEWNS denial is the AppArmor userns restriction, whereas NEWUSER with ENOSPC/EUSERS is a hardened `user.max_user_namespaces=0`. Classification is unchanged — EPERM stays **permanent** (an AppArmor denial will not clear on retry) and only `_TRANSIENT_PROBE_ERRNOS` are transient; a child that vanishes mid-handshake is treated as transient without widening that set. All verdict logic runs in the parent, driven by the child's pipe reports, so tests cover every branch without forking; the handshake is bounded by a timeout and the child is reaped on every path so the background warm thread can neither wedge nor leak.

**Ubuntu userns remedy — a per-application AppArmor profile installed by `kirocrew service install`** (`service/apparmor.py`): the probe above makes the restriction *visible*; this makes it *fixable* without weakening the host. Ubuntu's sanctioned mechanism for an application that legitimately needs unprivileged userns is a per-app profile granting `userns`, not a kernel-wide sysctl rollback — `/etc/apparmor.d/` on a stock install already ships exactly this for `bwrap-userns-restrict`, `chrome`, `chromium`, `brave`, `buildah`, `ch-run`, `QtWebEngineProcess`, `1password` and `Discord`.

- **Gated on the detected mechanism, never on distro ID.** All of: AppArmor present in `/sys/kernel/security/lsm`, `kernel.apparmor_restrict_unprivileged_userns` **existing and equal to 1**, and `apparmor_parser` ≥ 4.x (the `userns` rule's minimum). Any miss skips silently and the install continues, so Debian, Arch, RHEL, Amazon Linux and macOS are unaffected no-ops. Keying on `/etc/os-release` would both miss Ubuntu derivatives (Pop!_OS, Mint, Zorin, elementary) that inherit the restriction and wrongly target Debian 13, which ships AppArmor *without* it.
- **A NAMED profile ATTACHED to the resolved launcher script, `AppArmorProfile=` deliberately absent from the unit** (#3463). Two earlier designs were wrong. Attaching to the gateway's *interpreter* is wrong in both directions: `~/.kiro/crew-venv/bin/python3` is a **symlink** to the system interpreter and AppArmor matches the path the kernel resolves, so a venv-path attachment silently never matches, while attaching to the resolved `/usr/bin/python3` would grant unprivileged userns to **every Python process on the host**. A NAMED profile with no attachment, applied purely via `AppArmorProfile=-kirocrew-userns` in the unit, looked safer and shipped first (#1210) — but #3463 traced a live failure through `/proc/<pid>/attr/current` and the kernel audit log and found that directive labels only the literal top-level unit PID (`change_onexec` "converted to stacking"); the gateway's sandbox probe runs in a forked-not-exec'd child reached through the launcher's own exec chain, and that PID was still `unconfined` when it called `unshare()` — reproduced identically across a systemd-managed service, a bare foreground launch, and `aa-exec -p` (which stacks the top PID correctly and still fails downstream). Worse, installing a path-attached profile *and* keeping `AppArmorProfile=` in the unit makes the directive's `change_onexec` silently win over the kernel's automatic path attachment, so the two are mutually exclusive in practice. The fix: attach the profile **by path to the fully-resolved launcher script** (`kirocrew_bin()` — the same path `ExecStart` uses, e.g. `~/.kiro/crew-venv/bin/kirocrew`; not the interpreter, not any symlink in the chain) and drop the directive entirely. Kernel-side automatic attachment applies at every `execve()` in that chain and is inherited by a forked-not-exec'd child, which is the propagation the directive was missing. `validate_exec_path()` (shared with the launcher profile below) enforces this cannot be a shared interpreter, cannot live under a world-writable directory, and must be owned by the account the *service* runs as.
- **`flags=(unconfined)` and a single `userns,` rule** — the profile restricts nothing else; it exists only to carry that one grant, the same shape `/etc/apparmor.d/chrome` uses.
- **The abi is detected from the policy files present** (`/etc/apparmor.d/abi/`, highest numeric wins, omitted when none exist), not from the parser version: Ubuntu 25.10 ships `apparmor_parser` 5.x but only `abi/3.0` and `abi/4.0` on disk, so pinning the abi to the parser major makes the profile fail to load with `Could not open 'abi/5.0'`.
- **Validate before loading, verify enforcement after.** The generated profile is parsed with `apparmor_parser -Q --skip-cache` (`--skip-cache` because writing `/var/cache/apparmor` needs root and this runs before any privileged step) and is NOT installed if it fails to compile — loading a broken profile is how a service becomes unstartable. After `apparmor_parser -r`, enforcement is confirmed by transitioning into the profile with `aa-exec -p` and running a namespace probe, because the installing process is not itself confined by a profile systemd applies to the service, so probing in-process would report the unpatched host. Three constraints shape that check. It needs **privilege to enter** the profile — `aa_change_onexec()` into a named profile is not permitted for an unconfined user and `aa-exec` does not fail loudly when it cannot transition, it execs unconfined, so an unprivileged attempt returns a false negative. It must **not execute anything user-writable as root**: every tool (`apparmor_parser`, `aa-exec`, `setpriv`, `python3`) is resolved from a fixed list of trusted system directories and required to be root-owned and not group/world-writable — never through `$PATH`, and never `sys.executable`, since the venv interpreter is user-writable and running it under `sudo` would be a local privilege escalation — and the payload is a constant stdlib snippet that does not import `kiro_crew`, so user-writable site-packages never runs with privilege. And the probe itself must run **unprivileged**, or it proves nothing: root may be permitted to create namespaces regardless of the restriction, so `setpriv` drops back to the invoking uid/gid inside the profile before probing. A missing trusted tool is reported as inconclusive rather than as a failure, and a profile that loads but does not take effect is worse than none, so an unconfirmed verification says exactly that instead of claiming success.
- **The profile is loaded BEFORE the unit is started.** A path attachment applies at the kernel's own `execve()` time, so it must already be loaded before the first exec of the launcher script or the first gateway process (and everything it forks) comes up unprofiled. `linux.install()` therefore writes the unit, loads the profile, and only then runs `daemon-reload`/`enable`/`restart`.
- **Fail-soft throughout, and symmetric on removal.** No step here can fail the service install; every path returns an outcome the CLI prints (`⚠️` on failure) and continues. `service uninstall` unloads (`apparmor_parser -R`) and deletes the profile, so a host is left as it was found rather than carrying an orphaned grant. Privilege reuses the existing `sudo install` / `sudo systemctl` path the unit write already needs — no new escalation, and no KiroCrew or LLM-influenced code runs under sudo.
- **Verified end to end on Ubuntu 26.04** with the sysctl at 1: inside the profile `detect_backend()` returns `namespace`; outside it, on the same host at the same moment, `none` with `unshare(CLONE_NEWNS) failed with errno 1 (EPERM)`; and `apparmor_restrict_unprivileged_userns` remains `1` — the grant is app-scoped and the kernel-wide protection is untouched.
- Other ways unprivileged userns can be denied are **not** addressed by this profile and have different remedies (and different errnos): `user.max_user_namespaces=0` denies NEWUSER with ENOSPC/EUSERS; Debian's legacy `kernel.unprivileged_userns_clone=0`; a kernel without `CONFIG_USER_NS` (EINVAL/ENOSYS); and a container whose seccomp filter denies `unshare`, which is fixed with container flags, not host config. The probe's step-aware reason is what makes these distinguishable at diagnosis time.
- **A DIRECT launch (AppImage / desktop app) needs a SECOND, separately-named profile** — `/etc/apparmor.d/kirocrew-launcher`, installed by `kirocrew sandbox install-profile`, kept distinct from the service profile above even though both are now path-attached. The reason is not attachment-vs-not (both attach); it is that the service profile's target is *automatically resolved and always known* (`kirocrew_bin()`, the same path `ExecStart` uses, loaded before the unit starts), while a direct launch has no unit to load anything before, no reliably-known target without user input (`$APPIMAGE` / `--path`), and cannot transition itself into a profile at all: entering a named profile needs `aa_change_onexec`, which an unprivileged unconfined process is not permitted to do, and `aa-exec` does not fail loudly when it cannot transition — it execs unconfined, so a re-exec would appear to work while changing nothing (`sudo aa-exec` does transition, but would run the gateway as root). An attachment is applied by the kernel at exec time with no cooperation from the process, and is inherited by the backend; it is the mechanism stock Ubuntu already uses for `chrome`, `brave`, `1password` and `Discord`. An AppImage is a single self-contained file used by nothing else, which is what makes it a safe attachment target.
- **The attachment target is validated, because an attachment is a permission grant keyed on a path.** `validate_exec_path()` resolves the path first (AppArmor matches what the kernel resolves, so validating the pre-resolution path would let a symlink in a safe directory smuggle a grant onto `/bin/sh`) and then refuses: a **world-writable component anywhere in the chain up to `/`** (a writable *ancestor* is enough — rename the parent and the same absolute path resolves to an attacker's file; this also covers an AppImage's own `/tmp/.mount_XXXXXX`, a fresh random path per launch), a **shared interpreter** (`/usr/bin/python3`, `/bin/sh`, `node`, …), a path containing **glob metacharacters**, which AppArmor interprets inside an attachment even when quoted, and **any target not owned by the expected account**. That last rule is what makes the check sound: the interpreter regex is a blocklist, and a blocklist of shared runtimes is incomplete by construction — it names python, perl, ruby, node and the shells but not `java`, `mono`, `dotnet`, `php`, `lua`, `wine`, `R` or `qemu-*`, so `--path /usr/bin/java` would have granted unprivileged userns to every Java process on the host. Requiring ownership converts that leaky list into a complete invariant, since a root-owned executable in a system location is by definition shared with every user of the machine. "Expected account" defaults to the invoking process's own uid (the AppImage/launcher case: an unprivileged user runs `kirocrew sandbox install-profile` on their own account) but is an explicit `expected_uid` override for the service case (#3463): `kirocrew service install` may itself run as root or under `sudo`, while the venv launcher script it attaches to is owned by the human the *service*'s `User=` names — a different account from whoever is executing the installer, so the check verifies against that account, not the installer's own euid. Stock Ubuntu's `chrome`/`brave` profiles do attach to root-owned binaries, which is not a contradiction: a packager knows the path is one specific application, whereas this command is handed an arbitrary `--path` and cannot. Packaged profiles remain the answer for a system-wide install, and an administrator who deliberately runs the AppImage case as root can still attach to a root-owned path — the rule exists to stop an unprivileged user over-granting by accident. The default target for the launcher case is `$APPIMAGE`; a foreground `kirocrew gateway` has no safe target at all and is directed to `service install` instead. Both profiles share one gate, one parser resolution, one compile check and one enforcement probe (`verify_enforcement(..., profile_name=…)`), so they cannot drift apart in what counts as a supported host or a working grant.
- **A path attachment fails silently when the path changes**, which is the one failure mode the kernel reports no error for: a moved or renamed AppImage simply stops matching. `kirocrew sandbox status` compares the installed attachment against the current launch and reports a stale one as not covered, and the desktop app logs the exact remedy command at spawn time (`website/electron/sandbox-profile.js`) rather than attempting to escalate — `sudo` needs a TTY a GUI does not have. Install also warns when another profile in `/etc/apparmor.d` already attaches to the same path, since a hand-written profile is the workaround users find first and two profiles claiming one attachment is ambiguous.

**Edition-neutral executable resolution**: `namespace_argv()` (Linux) and
`sandbox_exec_argv()` (macOS) resolve argv[0] through
`PlatformContext.agent_executable` before applying KiroCrew's outer sandbox.
The public `DefaultAgentExecutableResolver` is identity, so ordinary PATH
resolution and an explicit `KIROCREW_KIRO_BIN` override behave unchanged. An
edition companion may replace a managed launcher with the direct executable it
ultimately invokes when nesting two OS-isolation layers would fail. This seam
cannot disable sandboxing: the resolved executable is always placed *inside*
the same namespace/Seatbelt wrapper. A transient resolver failure falls back to
the original executable while preserving the outer sandbox; a platform
composition failure propagates fail-closed. The capability probe
(`_probe_sandbox_exec`) still runs only the trusted fixed `/usr/bin/true` target
under `(allow default)`, never an edition-resolved or user-writable executable.

### XPIA Hardening (`security.py` + `hooks.py`)

**Sensitive path protection** — blocks at the hook layer before tool execution:
- `is_sensitive_path(path)` — checks `fs_read`/`ReadFile` targets against sensitive dirs
- `path_contains_sensitive(dir)` — the **reverse direction**: True when a protected location lies UNDER the given directory (the home dir itself, or any ancestor of `~/.ssh`/`~/.aws`/the crew data home). For bulk operations rooted at a directory — e.g. the Notes builtin's `git add -A` over an attached vault (see [md-notebook.md](md-notebook.md)) — where `is_sensitive_path` on the root passes but the sweep would stage a credential store wholesale. List-based prefix comparison against the known sensitive roots (no filesystem walk, O(sensitive entries) on any tree size); shares `_candidate_forms` / `_home_dir_targets` with `is_sensitive_path` so the symlink/casefold/`KIROCREW_HOME` hardening cannot drift between the two directions
- **Symlink resolution (CWE-59)**: `is_sensitive_path()` resolves symlinks before matching — it checks multiple candidate forms (`os.path.realpath` + `Path.resolve`, plus the lexically-normalized path as a fail-safe when resolution can't complete) and returns True if ANY lands in a sensitive location, `casefold`-comparing against sensitive dirs anchored at BOTH the logical home and its realpath (defeats a home-prefix OS symlink like macOS `/var`→`/private/var`). So a workspace symlink pointing at `~/.aws/credentials` (absolute or `../../.aws/credentials` traversal) cannot be read through the link
- **Relative-traversal block (verb-agnostic)**: home-anchored/absolute references to a sensitive dir are caught by the primary matcher (`_get_sensitive_re()`), but relative-traversal forms (`../../.aws/credentials`) escape it. `is_sensitive_bash_command()` therefore blocks **any** command whose tokens name a sensitive dir via dot-slash traversal (`_RELATIVE_SENSITIVE_RE`), regardless of verb — so `dd`/`base64`/`xxd`/`head`/`tail`/`cp`/`ln` are all covered (it was previously gated on `ln`/`cp` only, letting the others slip past). Returns "command references a sensitive credential path via relative traversal"
- `is_sensitive_bash_command(cmd)` — regex matches `cat`, `head`, `tail`, `less`, `cp`, `scp`, `python open()`, pipe redirects targeting sensitive paths
- **Separator-run collapse (shell grammar only)**: a Win32 shell opens the store `%LOCALAPPDATA%\kiro-cli` names when handed `%LOCALAPPDATA%\\kiro-cli`, so a repeated separator carries no meaning there while the matchers spell a single one. `is_sensitive_bash_command()` therefore repeats all three first-pass checks — the path matcher, the trust-root extraction control, and the relative-traversal matcher — over separator-collapsed copies of the subject, covering every run length at linear cost (admitting a run into the patterns instead measures as a watchdog-crossing hang on this gate). All three, never a subset: with the extraction control omitted, `tar -xf evil.tar -C $HOME//.kiro/crew` writes governance files through the doubled separator. The collapse runs only after the unmodified subject misses, so a form needing the run intact (a UNC `\\server\share` anchor) keeps its match. Because it runs only then, the shared helper's own check of the UNMODIFIED value is a provable duplicate on this path — pass 1 put those exact bytes through these same three matchers and missed — so the shell caller opts out of it (`value_already_scanned=True`) and checks only the collapsed copies. That is a cost fix rather than a coverage change: the sensitive-path matcher over a long newline-free line is the most expensive check on this gate, so running it twice doubled the wall time of a path a linearity test guards, taking a 20 KB subject from 2.4s to 4.2s and overshooting that test's ceiling on CI. Detection is unmoved and pinned by test in both directions — the single-separator spelling the skipped check would have caught is still caught by pass 1 itself, and the doubled spelling is still caught by the collapsed copy, as is the trust-root extraction control that travels with them. The default keeps the value-check, so the source-literal path below is unaffected and a new caller is self-sufficient unless it opts out deliberately. `subject_is_shell_grammar=False` skips this pass and only this pass, for a caller whose subject is **source code** rather than a shell command line: in source a backslash run is an escape — `\\` is one backslash, `\.` a literal dot — so collapsing strips the escape and manufactures a path the subject never contained, and a `re` pattern that redacts a fenced store, or a docstring merely naming one, reads as an access to it. Every other pass still applies, and the skip is keyed on the subject rather than on any single check, so a caller either has shell grammar and gets all three or does not and gets none. The cron script-body scan (`mcp_cron._vet_script_contents`) is the one caller that passes it
- **Command-line SUBJECT for a source body** (`_source_command_subjects`): three passes differ from the rest in needing a subject that IS a command line, for two different reasons. The two traversal passes (`_check_alt_traversal_reaches_fence`, `_check_find_traversal_reaches_fence`) walk shell STRUCTURE, under budgets whose exhaustion correctly refuses (`_ALT_MAX_STAGES`, `_FIND_SUBSTITUTION_BUDGET`), because a command line carrying 512 pipeline stages is not one anybody types and dropping the stages past the cap made the cap the bypass. A source body breaks that premise rather than the budget: `_alt_collect_stages` splits on newline / `;` / `|`, so **every line of a Python file counts as a pipeline stage** and a body of a few hundred lines exhausts the ceiling while carrying no shell content whatsoever. The refusal is then keyed on the body's SIZE — `"x = 1\n" * 600` is refused, and an ordinary script is banned for its length on every fire — while a SMALL malicious body is still fully inspected, so what the ceiling costs is the feature and not the fence (an availability defect, not an open path). `is_sensitive_source_body` therefore hands those two passes the command strings the body CONTAINS, via the internal `_traversal_subjects`, and a third pass joins them for a different reason: the env-credential rules (`_check_env_credential_access`) are ORDERED-EXISTENCE patterns describing one pipeline, so over a document they match pieces lying arbitrarily far apart and produce a false DENIAL rather than a refusal. A 48 KB body drew that denial without containing any such pipeline — the pattern's opening env accessor appears near the top, a pipe character somewhere after it, one of its filter words inside a comment, and the vendor prefix later still — which no single line and no 40-line window reproduces. Scoping those rules loses nothing on the shape they describe, and the two KINDS of pass get DIFFERENT subjects because they need opposite things: a traversal pass gets each command string on its own (`_traversal_subjects`), while the env rules get all of them CONCATENATED in source order (`_env_subject`), since an ordered-existence rule needs the pieces together. A body can assemble its command from fragments — `"env | gr" + "ep AWS_SEC" + "RET_ACCESS_KEY | cu" + …` — where no fragment matches and the split also carries the credential NAME past the cron gate's bare-name matcher, so the whole DOCUMENT misses that shape both before and after this change; joining closes it. Joined with nothing between them, because that is what `+` does at runtime, and collected in SOURCE order rather than `ast.walk` order — walk order is breadth-first, so a left-nested `+` chain yields its right operands first and joining it reconstructs nothing. Fusing unrelated literals can only ADD a denial. Two conditions make the carve-outs sound and BOTH are checked rather than assumed: the body must PARSE, and the literal walk must have actually COMPLETED. Those are different questions — `visit` is recursive, a legitimately deep expression exhausts the interpreter's limit, and `_sensitive_run_in_source_literals` reports that as `parsed=False` rather than raising — so a body that parses but overflows the walk falls all the way back to the whole document with pass 1b included. Conflating them shipped once and was caught in review: `x = 1+1+…+1` (folded iteratively by the PEG parser while the AST walk overflows) followed by `open(r"…\\kiro-cli\\c.json")` was neither literal-scanned nor collapsed, reopening #6350 inside a script. The remaining passes are left on the whole subject on purpose — passes 1 to 3 and the IMDS check match text or judge tokens with neither a structural budget nor an ordered-existence shape, so a document cannot assemble a verdict out of pieces no command line holds together. This is a change of SUBJECT, not the "spend the budget then answer smaller" shape whose three fail-open instances were removed alongside `_AltWorkBudget` — each subject is analysed completely, to the same depth, under the same budget, the first denial wins so adding subjects can only ADD denials, and a caller that supplies none keeps the whole-subject behaviour byte for byte (the shell path is unchanged and pinned by test in both directions, including the padded-stage attack). Every `str`/`bytes` constant is collected, f-string fragments and docstrings included, decoded exactly as the fence scan decodes them; nothing is filtered as "cannot be a command" because a subject naming no traversal simply matches no rule, so over-collecting costs work and can only add denials while under-collecting is a missed read — a whitespace-only value is the one exclusion, provable rather than heuristic. A command assembled from fragments at runtime is explicitly not reconstructed, the same limit the `re`-authenticity guards already record; the fragments are each still inspected. The body is parsed ONCE through `_parse_source_body`, shared with the literal fence scan so the two cannot disagree about what "unparseable" means, and an unparseable body has no strings to hand over and keeps the whole-document scan with pass 1b and both budgets — never quietly exonerated. `_SOURCE_COMMAND_SUBJECT_CAP` bounds the one dimension this opens up, the subject COUNT: per-subject cost is bounded (measured 150–620 µs) but the count is author-controlled, so a generated body carrying tens of thousands of literals would pay it tens of thousands of times on the gateway's single asyncio loop — the wedge shape the other budgets exist to stop. Measured against the cron scripts of one real install the largest body (1170 lines) carries 441 constants and the median ~140, so the cap sits at 1024, roughly 2.3× the largest real body and ~0.6 s of worst-case work, and exhausting it REFUSES for the same reason `_ALT_MAX_STAGES` does. **Residual, same premise one gate over**: `_find_substitution_openers` counts every backtick as a substitution opener, and a markdown code span in a docstring is a backtick pair — so a docstring with 66 code spans reads as 66 nested substitutions and still refuses. Shell backticks cannot nest without escaping, so the opener count over-states the nesting depth the budget actually guards; correcting that proxy is a separate change and is named here rather than silently carried
- **Escape-aware counterpart for source subjects** (`_sensitive_run_in_source_literals`): skipping the collapse on a source body would reopen the doubled-separator bypass *inside* a script, because the run still exists in the DECODED literal — `open(r"…\\kiro-cli\\c.json")` hands the OS two backslashes and Win32 collapses them, while the raw source matches no fence. So the pass is replaced rather than removed: the same three checks run against each decoded string literal. Decoding alone cannot decide, which is why this is **sink-aware** — a regex escape and a path separator are the same character in a decoded value, so `re.compile(r"%LOCALAPPDATA%\\kiro-cli")` and `open(r"%LOCALAPPDATA%\\kiro-cli\\c.json")` are indistinguishable by any transform of the value and differ only in the call that receives it. A literal is exonerated only when it provably flows into the **pattern operand** of a pattern-consuming call (`_SOURCE_PATTERN_SINKS`); an unknown call, a name bound first, or no call at all all keep the deny verdict, so an unenumerated sink over-blocks rather than opening the fence — the direction `_TRUST_ROOT_READ_LISTERS` argues for. The operand matters because the allowlisted calls are not uniformly safe: `re.sub(pattern, repl, string)` returns its subject verbatim and its replacement substantially so, so a fenced path in either slot would flow on to a real sink through a call that merely looks harmless. Only `args[0]` / `pattern=` describes a regex. Occupying that slot means BEING the operand rather than merely reaching it: the position test resolves to the top of the argument subtree, so an expression FEEDING the operand satisfies it while the fenced literal sits underneath, and evaluating that expression runs code before `re` ever receives a pattern — `re.compile(FENCED + Reader())` hands the expanded path to `Reader.__radd__`, `%` formatting reaches `__rmod__`, and every other operator protocol is the same shape, so enumerating operators would be another allow-by-default blocklist. The exoneration therefore requires the literal ITSELF to occupy the slot, positionally or by `pattern=`, which also subsumes the walrus and f-string spellings that previously needed their own reasoning. The pattern slot of a SUBSTITUTING member (`re.sub`, `re.subn`) carries a further hazard: its `repl` may be a FUNCTION, and `re` hands that function the `Match`, which carries `.re` — so `Match.re.pattern` returns the verbatim pattern literal. The compiled-name escape analysis cannot reach it, because that tracks only `_COMPILING_SINK` results while a Match is never bound by the exonerated statement, and the recovery read can be spelled to defeat any enumeration (`getattr(m, "r" + "e")` builds the attribute name from a concatenation, an aliased `g = getattr` hides the callee). Chasing the spelling is therefore the wrong layer, exactly as it was for the compile result: the exoneration is withdrawn at the SLOT unless the replacement PROVABLY cannot be called, meaning a `str` or `bytes` constant. A Name or Attribute may be bound to a function, a lambda plainly is one, a `Starred` puts the replacement at a position nobody can know statically, and an absent argument leaves nothing to prove — each fails closed. The motivating redactor passes a string replacement and is unaffected. Scoping that withdrawal to the substituting members alone was still too narrow, because a `Match` also leaves an exonerated slot as a RETURN VALUE: `re.match`, `re.search`, `re.fullmatch` and `re.finditer` each hand one back, and nothing tracks it — the compiled-name analysis follows only `_COMPILING_SINK` results, so a Match bound to a name, iterated by a `for`, or captured by a comprehension reaches no guard at all, and `_pattern_reextracted` cannot see the recovery because `getattr(m, "r" + "e")` builds the attribute name from a `BinOp` rather than a `Constant`. Those four are therefore simply ABSENT from the sink set, so deny-by-default refuses a fenced literal in their pattern slot outright. A withdrawal rule for them was tried first and removed as dead code: the only shape it could still exonerate was a DISCARDED result — the call in statement position, its value received by nothing — which no real script writes, and no benign-corpus body uses these four at all. `re.findall` and `re.split` remain in the set: they return `str` and `list`, which carry no reference back to the pattern. The COMPILED object still needs the explicit rules, because there the exoneration is earned by `re.compile` and only then is the Match produced, and `_SAFE_COMPILED_PATTERN_METHODS` admitted the entire matching API on the METHOD NAME alone: `p.sub`/`p.subn` pass their replacement the Match (so the provably-non-callable test applies, at position 0 because the pattern is bound in the object rather than passed), and `p.match`/`p.search`/`p.fullmatch`/`p.finditer` return one (so the discarded-result rule applies), while `p.split`/`p.findall` stay safe. The accepted cost is a wider over-block than before — a fenced literal used purely to TEST for the store, `if re.search(FENCED, line):`, is now denied — which is the same direction as the three over-blocks already recorded here. `re.escape` is deliberately NOT in the set even though it is an `re.*` call: it consumes plain TEXT and returns it escaped for onward flow, so it fails the set's own admission rule and would exonerate a literal that continues to a real sink. The exoneration also keys on the *spelling* `re.<func>`, so it is withdrawn for a body that rebinds the name at all — and the spellings do not all reach the AST as a `Name` node: `import evil as re`, `re = …`, `class re`, a parameter or loop variable bind through nodes, while `except E as re:`, `case re:`, `case [*re]` and `case {**re}` bind through plain STRING attributes (`ExceptHandler.name`, `MatchAs`/`MatchStar.name`, `MatchMapping.rest`) that a Name-only walk cannot see. A WILDCARD import carries no alias naming the module yet can bind it anyway: `from evil import *` binds a set this tree cannot enumerate, `re` possibly among them, so matching only the aliases that NAME `re` left the explicit `from evil import thing as re` forfeiting while the wildcard — which can do strictly more — did not, an allow-by-default enumeration inside a deny-first checker. The forfeit condition is therefore an UNKNOWABLE binding set, which closes the class rather than adding a spelling to it; `from re import sub` names `sub` and rebinds nothing, so it stays allowed. It is withdrawn equally for a body that reassigns an ATTRIBUTE of the module (`re.compile = open` leaves the module bound while the call it spells now opens a file), including when that mutation is spelled as a CALL — `setattr(re, "compile", open)` / `delattr(re, "sub")` reach neither the Name nor the Attribute branch, so they are matched on the call itself. Recognising only those two callees was the wrong SHAPE for the same reason the recovery guard was: it is an allow-by-default blocklist inside a deny-by-default checker, so an ordinary `helper(re)` whose body does `m.compile = open` reached no branch at all and the module still read authentic. What the callee does with the object is not readable from this tree, so the rule INVERTS: any ARGUMENT resolving to the module forfeits the exoneration, keyword arguments included. Only the arguments are inspected, never the call's `func` — `re.sub(...)` and `re.compile(...)` name the module there, and those are precisely the calls the exoneration exists to permit. Every guard here is nonetheless a STATIC read of the parse tree, so a body able to run code defeats all of them at once: `exec("re.compile = open")` carries the rebinding inside a STRING, reaching no Name, Attribute, Subscript or argument the tree can be asked about. The string is opaque by construction, so no enumeration closes that class either — the presence of `exec`/`eval`/`compile`/`__import__` as a bare NAME withdraws the exoneration instead, matched on the name rather than the call so that binding it first (`e = exec`) forfeits equally. The builtin `compile` is matched only as a Name; `re.compile` spells its name in an Attribute's `attr` STRING and is not a Name node, so the ordinary redactor is unaffected. Measured on those spellings, the authenticity defect was a layer-contract violation rather than a demonstrated end-to-end leak — nine exploit bodies were already refused by the downstream escape analysis — so this restores defence in depth at the layer that claimed it rather than closing an open path. Mutation is also followed through an ALIAS: `m = re` (or `import re as m`) binds the SAME object, so `m.compile = open` replaces exactly the attribute `re.compile` spells while an `re`-keyed check sees an untouched module. Every name bound from an alias is collected first, and a reference that escapes as a VALUE rather than through such a binding is no longer trackable at all: `holder = [re]` then `m = holder[0]` puts the same object behind a subscript no static walk can resolve, so `m.compile = reader` rebinds exactly what `re.compile` spells while an alias set keyed on bare Name assignments records nothing — and a container display is only one spelling, alongside a tuple, a dict value and a conditional expression. Enumerating those shapes would rebuild the allow-by-default blocklist the call branch already had to abandon, so the rule closes the class instead: a module reference may only be READ through an attribute or bound as a tracked bare alias, and every other mention forfeits. The indirect routes that carry no alias name at all — `vars(re)["compile"] = open`, `re.__dict__[…]`, `sys.modules["re"].compile = open` — are matched on the SUBSCRIPT, deny-first: a subtree naming `sys.modules` cannot be proved not to hand back the module. A namespace MAPPING is the same capability one level further out and is NOT reachable by inspecting the subscript at all: `globals()["re"] = Fake` rebinds the name while its subscript's `value` is the bare `globals()` call, which names no alias, no `modules` attribute and no `"re"` constant, so every binding branch above is bypassed while the module still reads authentic. It is therefore matched on the NAME — `globals`, `locals`, `vars` — because the mapping can be bound first (`g = globals()` then `g["re"] = Fake`) leaving the subscript's own value an unresolvable local; `locals()` at module scope IS the global namespace and `vars()` with no argument is `locals()`, so the three spellings forfeit together. The position must also be PROVABLE: `re.sub(*seq)` makes `args[0]` a starred node whose contents land at unknowable positions, so an unprovable position is not treated as the pattern slot. The two halves are exposed as a single entry point, `is_sensitive_source_body(text)`, which owns the composition — skipping pass 1b is sound only because the literal scan replaces it, so the flag that skips it is internal and a caller cannot take one half without the other. Both `str` and `bytes` constants are inspected, `bytes` decoded latin-1 (total over a byte range, one code point per byte, so a separator run survives unchanged), because `open` and `os.open` accept a bytes path and a `rb\"…\"` literal otherwise reaches the same sinks unexamined. A string constant in statement position is skipped ONLY when it is not a docstring: Python evaluates and discards a bare string expression, so that one reaches no sink, but it RETAINS a module, class or function docstring as `__doc__`, which a body can read back and hand to a sink (`open(f.__doc__)`). Docstrings are therefore scanned. The three checks run against the literal's own value AND its separator-collapsed copies, in that order: `_separator_collapsed_variants` yields nothing when the separators are already single, so iterating it alone left such a value unexamined by this layer and dependent on a later pass — defence in depth sharing the earlier layer's blind spot. Checking the value first makes the layer self-sufficient, and it must stay uniform: exempting docstrings would reopen a single-separator `open(f.__doc__)` path. The cost is that a prose-only docstring naming the store is refused even though it reads nothing, which is recorded as an accepted over-block with its own test rather than left in the benign corpus. **Deferred, same root cause one gate earlier**: `llm_helpers.py`'s tool-input scan runs the shell matcher over every string extracted from `tool_input`, including a file-write tool's `content`, so an agent is still refused *writing* the redactor body that cron may now *run*. A second sibling gate, `skills_script_validator.py`, scans generated skill scripts as RAW TEXT with neither the separator collapse nor the decoded-literal check, so the doubled spelling this change closes for cron bodies still passes there. Both call sites are out of this change's scope and are named here rather than silently carried. Comments never reach the check, the parser having discarded them. Exoneration further requires the matched `re.*` call to be the OUTERMOST expression the literal reaches — a call whose result is consumed by another call or has an attribute read off it hands the verbatim pattern onward (`open(re.sub(FENCED, …))`, `re.compile(FENCED).pattern`) — and it is withdrawn for the whole body when that body reads a `pattern` or `re` attribute anywhere, which is how the cross-statement `p = re.compile(FENCED)` then `open(p.pattern)` escape is closed. That read has a CALL spelling too: `getattr(p, "pattern")` carries the attribute name in a string argument, so it parses to an `ast.Call` and an Attribute-only walk never sees it — the same call-spelled blind spot `setattr`/`delattr` exploited against the authenticity check, and left the dotted and called forms of one read disagreeing. Both attribute names are matched on the call as well, including the dunder-getter forms (`p.__getattribute__("pattern")`, `object.__getattribute__(p, "pattern")`, `operator.attrgetter("pattern")`) whose `func` is itself an Attribute. But enumerating recovery spellings is the wrong SHAPE for a deny-first checker: the ways out of a compiled object are open-ended — an aliased `g = getattr`, and every stringify form (`"%s" % p`, `"{}".format(p)`, `f"{p!r}"`, `str`/`repr`/`vars`) — so a blocklist sits one unenumerated spelling away from reopening the fence. The guard therefore INVERTS: a compiled pattern may be used through its own matching API (`search`, `match`, `fullmatch`, `split`, `findall`, `finditer`, `sub`, `subn`) and nothing else, and any other read — passed to a call, formatted, subscripted, stored in a container, returned — forfeits the exoneration for the whole body. The accepted cost is that a harmless unenumerated attribute read (`p.flags`) withdraws it too. Only `re.compile` results are tracked: the other sinks CONSUME a pattern and return an ordinary value, so tracking them made `redacted = re.sub(F, "<X>", s)` followed by `str(redacted)` read as a re-extraction and refused the very redactor shape this change exists to permit. Because that tracking watches NAMES, a compile result bound anywhere a name cannot follow — a subscript, an attribute, a tuple element — leaves the escape check watching nothing while the literal stays exonerated, so the untrackable binding itself withdraws the exoneration rather than the checker attempting alias analysis into containers. Enumerating binding SHAPES is still not enough, because a result that is never bound reaches no binding node whatsoever: `return re.compile(FENCED)`, `keep(re.compile(FENCED))`, `[re.compile(FENCED)]` and a bare `yield` each leave the tracked set EMPTY, and an escape check handed an empty set cannot fail — so the literal is exonerated with zero tracking behind it and ANY recovery spelling then works, including one that defeats a literal attribute-name match (`getattr(p, "pat" + "tern")` builds the name from a concatenation). Chasing the recovery call is the wrong layer, so the rule inverts: exoneration requires the compile result to be the DIRECT value of an assignment whose every target is a plain Name — precisely what the tracker can follow — and every other position forfeits. The slot is likewise forfeited when a WALRUS binds the literal inside it: `re.compile(p := FENCED)` puts the literal in a genuine pattern operand while also binding it to a name that outlives the call, so a later `open(p)` receives the verbatim fenced spelling and the "a pattern operand goes nowhere else" premise fails. Three over-blocks are accepted deliberately here, on the same reasoning as the rebinding trade: the literal must sit DIRECTLY in the pattern slot, so the idiomatic module-constant-then-compile form is denied; a chained-but-harmless `re.compile(FENCED).search(s)` is denied because an attribute read is indistinguishable from a `.pattern` re-extraction; and a docstring naming a fenced path is denied even where `__doc__` is never read. Each errs toward the false positive rather than toward the fence. A body that does not parse yields no literals, and the caller then runs the raw scan **with** the collapse, so an unparseable body is never quietly exonerated; a body so deeply nested that the traversal exhausts the recursion limit is reported the same way rather than raised, so a legitimate deep expression degrades to the textual scan instead of failing the gate
- **Normalizer second pass (verb-independent)**: the regex first-pass matches raw shell text, so it sees only the path spellings it is authored for — two textually different strings naming the same file are not decidable by a regex over an unnormalized command line, and the set of equivalent spellings (dot segments, `..`, repeated slashes, `$HOME` vs the resolved home, quote splitting) is open-ended by construction. `_check_sensitive_via_normalizer()` therefore tokenizes via `normalize_shell_command()` and routes **every** path-like operand through `is_sensitive_path()` — the same normalizing checker the file gate uses — so one implementation is authoritative on both surfaces and a newly registered keystone leaf is protected on both by registration alone. The pass runs regardless of verb, mirroring the verb-independent backstop the sensitive-dir matcher applies: naming a sensitive path is itself the signal, and normalization is the only layer able to decide equivalence, so restricting it to a verb allowlist would leave a spelling such as `~/.kiro/crew/./live_target.json` unchecked for every verb outside that list. `_NORMALIZER_READ_VERBS` / `_LINK_CREATE_VERBS` are consulted only to skip the command name itself, never to decide whether operands are checked. `key=value` operands are split on the first `=` and the value checked as well: `of=/path` does not resolve as a path, and `--output=/path` is otherwise dropped by the flag skip. **Attached redirections** (`>~/path`, `>>~/path`, `2>~/path`) are kept as a single token by `shlex.split`; the leading operator prefix is stripped via `_REDIR_PREFIX_RE` before the path portion is checked, so `printf x >~/.kiro/crew/./live_target.json` is blocked just as `echo x > ~/.kiro/crew/./live_target.json` (with a space) is
  - **Not covered**: a bare relative operand (`live_target.json` run with the cwd inside the data home). `is_sensitive_path` is called without a `base_dir`, so such a token resolves against the gateway process cwd rather than the command's — a command line inspected before execution does not carry its cwd. Closing it requires a fail-closed decision for relative tokens whose basename matches a keystone leaf
- `hooks.on_tool_call` runs **both** `is_sensitive_path` and `is_sensitive_bash_command` on the **normalized** tool title regardless of the kiro-cli `Reading: `/`Running: ` display prefix. The claude-agent-acp adapter sets a file-read tool's title to the bare path and a Bash tool's title to the bare command (no prefix), so gating either check on the prefix would let credential reads through on an alternate ACP backend. `is_sensitive_path` resolves the title as a path (a bare `~/.aws/credentials` matches; a `cat ~/.aws/credentials` command resolves to a non-sensitive path and is caught by `is_sensitive_bash_command` instead).
- Sensitive paths: `~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.gpg`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`, `~/.kube/config`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, `~/.kiro/crew/.env`, `~/.kiro/crew/sel_hmac.key`, `~/.kiro/crew/trust`, `~/.kiro/crew/security_events.jsonl`, `~/.kiro/crew/app_admission.json`, `~/.kiro/crew/workflow_library`, `~/.kiro/crew/run`
- **Crew data-home secret/trust-root leaves are covered under EVERY known home prefix.** Since the data home moved from top-level `~/.kirocrew` to `~/.kiro/crew`, each Kiro Crew secret / governance trust-root leaf (`.env`, `browser-cookies.txt`, `playwright-storage-state.json`, `sel_hmac.key`, `trust`, `security_events.jsonl`, `app_admission.json`, `security_policy.json`, `profiles`, `policy_cache`, `admission_policy.json`, `denied_commands.json`, `crons.json`, `cron-history`, `workflow_library`, `oauth_endpoints.json`, `live_target.json`, `token_signing.key`, `refresh_chains.json`, `.local_secret`, `routing`, `run`) is expanded onto `_SENSITIVE_HOME_DIRS` under each entry of `_CREW_HOME_PREFIXES = (".kiro/crew", ".kirocrew")`. So the same leaf is read+write-blocked in (1) the current home `~/.kiro/crew` and (2) a not-yet-migrated pre-move legacy `~/.kirocrew`. The migration force-deletes `~/.kirocrew` once the move completes — there is no rollback copy to gate. A new secret is added to `_CREW_SECRET_LEAVES` once and is covered in both locations.
- **Identity/auth SQLite store (keystone leaves `data.sqlite3` + WAL/SHM/journal sidecars)** — the store holds live bearer tokens, so a read impersonates the user against the model service and a write forges the identity rows. The kiro-cli and amazon-q copies are fenced by DIRECTORY (`identity_stores.fenced_home_dirs()`), which covers each store's sidecars and temporaries for free. The crew data home cannot be fenced that way — reading `config.json` and `sessions.db` there is routine and intended — so the store is named as a leaf on `_CREW_SECRET_LEAVES`, using `identity_stores.AUTH_SQLITE_DB` rather than a fresh literal so the fence cannot drift from the readers that resolve the same store, and the name is fenced before a writer for that location exists (the treatment `agentcore-inbound` gets). The `-wal`/`-shm`/`-journal` sidecars are named beside it because a file leaf matches its exact name only and a sidecar carries the store's credential bytes — `kiro_cli` states the same fact from the other side, that identity rows read as absent when the `-wal` sidecar is missing; `.tmp`/`.lock` publish artifacts in the same parent are already covered by `_KEYSTONE_ARTIFACT_SUFFIXES`. Scoped to the `_CREW_HOME_PREFIXES` entries and deliberately NOT matched by basename: `data.sqlite3` is a generic filename, so a basename rule would refuse an unrelated application database anywhere under the home directory. Every legitimate reader (`kiro_usage_api`, `kiro_cli`, `kiro_prerequisite`) resolves its path through `identity_stores` and opens it directly rather than through this gate, so no reader is affected. A `-name` traversal from an unfenced ancestor (`find ~ -name data.sqlite3`) names no path either half of the gate can match and is the same class for every keystone leaf, not this one — closing it is the traversal pass's job, which this fence makes coherent.
- **Meetings owner-edit root.** `apps/meetings/data/edits` is a directory leaf on the same read+write sensitive floor. The minutes editor returns owner-authored text verbatim, so its sidecars may contain private corrections or credential-shaped examples; meetings agents must neither inspect them with `fs_read` nor overwrite them with `fs_write`. Keeping the root outside `meetings/<id>/`, then registering the whole directory, makes the existing hook gate the enforcement boundary and also protects atomic-write temporary siblings. The Meetings backend opens these files directly, so save, overlay, revert, and meeting deletion are unaffected.
- **SEL audit trust root (Talos finding cdf82704)** — `~/.kiro/crew/trust/sel_hmac.key` (the HMAC-SHA256 signing key for the tamper-evident audit chain, gated via the whole-dir `trust` entry; the legacy pre-migration `~/.kiro/crew/sel_hmac.key` leaf stays gated too) and `~/.kiro/crew/security_events.jsonl` (the audit log itself) are on the sensitive floor. The key lives in a dedicated owner-only `trust/` directory OUTSIDE the log's directory so write access to the log dir does not imply re-signing power. Without this floor an audited agent could `fs_read` the key and forge/rewrite the chain, or rewrite the log directly, erasing evidence of its own actions. The gateway's own SEL writer/reader (`sel.py`, `dashboard/session_health.py`) opens these files directly and does **not** route through `is_sensitive_path()`, so audit logging is unaffected. Residual: the key still lives in the agent's namespace; a deeper out-of-process signer (key never exposed to the agent) is documented future hardening.
- **App admission trust root (keystone invariant)** — `~/.kiro/crew/app_admission.json` (the sole fleet-controlled App Kit admission ceiling loaded by `apps/admission.py`) is on the sensitive floor, read- and write-blocked through the shared file gate. Because an **absent** policy falls open (admit), a governed agent that could `fs_write`/`tee`/`rm` this file could delete or neuter its own admission ceiling and re-admit a banned/unsigned app. Adding it to `_SENSITIVE_HOME_DIRS` also arms the bash write/extract matcher (`tee`/`rm`/redirect). The gateway's own loader (`apps.admission.load_app_admission_policy`) opens it directly via `path.read_text()` and does **not** route through `is_sensitive_path()`, so legitimate policy loading is unaffected.
- **Cron store (keystone leaf `crons.json`, #4812)** — the cron store holds access-control state, not just scheduling data: `session_key` decides which chat session may manage a job through the MCP cron tools and where its output is delivered, `approval_mode` is a per-job auto-approval decision, and `command`/`script` decide what gets executed on the host on a schedule. While the file sat outside the protected leaves, an auto-approved shell could reassign ownership, flip a job to auto-approve, or rewrite a scheduled job's command with an ordinary file edit — an open side door around the MCP tools' deliberate cannot-write-`session_key` rule and the `self-protection-cron-adopt` denied command, because those controls match command strings while the state lives in the file. The gap was a class, not a one-off: a command-string matcher cannot hold a file-backed invariant, so the structural control (the gate) is where the state lives now. Both `crons.json` and the `cron-history` sidecar directory (per-job records plus the index) are on `_CREW_SECRET_LEAVES`, read+write-blocked under every `_CREW_HOME_PREFIXES` entry on both the tool path and every shell form. The gateway's own writers (`CronService` via `atomic_write`, `CronHistoryStore`) open the paths directly and do **not** route through `is_sensitive_path()`, so scheduling and history are unaffected. Cost, accepted deliberately: a human hand-edit through an agent shell is now refused — the same trade-off every other keystone leaf makes; the store's malformed-entry tolerance still covers out-of-band edits made outside an agent session.
- **Saved workflow library (keystone directory `workflow_library`)** — a valid definition is executable source and its presence means the user explicitly promoted it into a durable named capability. Owner-only modes do not isolate a same-UID agent process, so leaving the records under an ordinary workflows directory would let an auto-approved file edit plant a definition that the library would list and run without the confirmation flow. The dedicated `<KIROCREW_HOME>/workflow_library/` directory is therefore on `_CREW_SECRET_LEAVES`, read+write-blocked for agent file tools and every shell form. Classifying the whole directory also protects atomic-write temp files. Run snapshots remain under the configurable `workflows.dir`; only explicitly promoted definitions use this fixed trust root. Dashboard and workflow-service readers/writers open it directly, so human listing, saving, editing, and invocation are unaffected.
- **Runtime exec dir trust root (run-marker sandbox-escape)** — the `run/` dir under the crew home is on the sensitive floor (under every `_CREW_HOME_PREFIXES` entry) because the gateway executes its contents **outside** the agent sandbox: the sandbox launcher scripts (`sandbox.py` execs `python <home>/run/kirocrew_sandbox_*.py`) and the remote-instance run-marker `gateway-<port>.bin` (`instances/run_marker.py`), whose recorded launcher path the SSH token-mint **and** the `restart_remote` action read and `exec` on the remote host. Without this floor entry a prompt-injected / sandboxed agent that could `fs_write` (or `tee`/redirect) a marker — or a launcher — into `run/` could point it at an attacker-controlled binary that the next routine token refresh or restart execs unsandboxed (owner + `-x` checks don't help; agent writes run as the same user) — a reachable sandbox escape. Classifying the whole dir also arms the bash write/extract matcher. The gateway's own writers (`instances/run_marker.write_marker` via `atomic_write`, and the sandbox launcher writer) open these paths directly and do **not** route through `is_sensitive_path()`, so gateway startup/spawn is unaffected.
- **Live-target pointer (keystone leaf `live_target.json`)** — `~/.kiro/crew/live_target.json` decides which checkout the gateway `execve`s into at startup (Dev Fleet "Make live"), so a writable pointer is arbitrary code execution under the gateway's own identity, and a readable one tells an attacker which checkout to aim at. Added to `_CREW_SECRET_LEAVES`, so it is read+write-blocked under every `_CREW_HOME_PREFIXES` entry through the shared file gate. Only the human-driven dashboard cutover action (`_make_live` in `dev_fleet/live.py`) writes it, via `live_target.write_target()`. The gateway's own startup reader (`live_target.maybe_reexec` called from `cli.py`) opens it directly rather than through the gate, so live-target resolution is unaffected.

- **Channel routing state (keystone leaf `routing`, holding Teams' `teams_service_urls.json`)** — the store is DELIVERY ADDRESSING, not a secret: it maps each allow-listed Teams identity to the conversation that identity was last seen in, and `teams/transport.py`'s `resolve_configured_target` resolves an explicit `user:<upn>` send target through exactly that map. While the file sat outside the protected leaves, a prompt-injected agent with file-write access could point one operator's UPN at a different person's conversation and have the next cron result, subagent-completion notice or `send_message` delivered there. The two attestations on the inbound path do not close it: the JWT's own `serviceurl` claim binds the address only for the activity carrying it, `connector_host_allowed` re-checks the host wherever the Connector token is attached, and neither can distinguish one legitimate conversation id from another on a shared Connector host. Reading is fenced with writing because the file enumerates the operator's UPNs and the conversations they use. **A DIRECTORY leaf, not the file, and that is load-bearing:** a file leaf matches only its exact name, while `atomic_write` publishes through a `tempfile.mkstemp` sibling (`tmpXXXXXXXX.tmp`) in the same parent — so with the store loose in the data-home root an agent watching that directory could overwrite the temp file in the window before `os.replace` and have the rename publish its own routing. A directory entry covers every child, random temp names included; the same residual is why `trust`, `profiles` and `cron-history` are directories. Note the general form is NOT closed by this entry: a keystone leaf named as a FILE (`crons.json`, `security_policy.json`, …) still has an uncovered `atomic_write` temp sibling, which is a matcher-level question rather than a per-store one. `ServiceUrlStore` opens its path directly (`atomic_write` / `read_text`) rather than through the gate, so proactive routing across a restart is unaffected. There is deliberately no migration from the pre-`routing/` location: reading the old, agent-writable path would reopen exactly the hole this closes, and the store is a warm start that degrades to in-memory by design.
- **Computer-use primary enable (keystone leaf `computer_use.json`)** — the on/off switch for native desktop GUI automation (see [computer-use.md](computer-use.md)) is `~/.kiro/crew/computer_use.json`, added to `_CREW_SECRET_LEAVES` so it is read+write-blocked under every `_CREW_HOME_PREFIXES` entry, on both the tool path (`is_sensitive_path`) and every shell form (`is_sensitive_bash_command` — `cat`, `>`, `tee`, `rm`, plus `tar -C` / `unzip -d` extraction into the trust root via `_EXTRACT_INTO_TRUST_ROOT_RE`). **It is deliberately NOT in `config.json`**, and the precedent is the denied-command opt-out immediately below: `is_sensitive_write_path("~/.kiro/crew/config.json")` is `True`, but `is_sensitive_bash_command("echo x > ~/.kiro/crew/config.json")` is `None` and `is_denied(...)` is `None` (at the time that precedent was set `_WRITE_PROTECTED_BASH_LEAVES` was `('.data-home-ready',)` only; it now also carries the two Ops Mission Control authorization inputs described below, and `config.json` is still deliberately absent from it), so a `config.json` toggle would be flippable by a prompt-injected agent through any redirect. A primary enable for full desktop observation plus input synthesis is a **security ceiling**, the same class as the deny opt-out, so it lives on the keystone. Reads fail soft to `{}` → **disabled**, and `is_enabled()` is a strict identity test against `True` (a hand-edited `"enabled": "false"` or `1` does not enable desktop control). The only writer is the dashboard PUT handler, which does not route through the agent tool gate; `enable_state.load_state()` opens the file directly, so legitimate reads are unaffected. The file carries no separate pointer opt-in: an earlier revision documented an `allow_pointer_move` flag (plus a `capabilities.computer_use_pointer` governance row) as a second consent gate for the one click path that warps the operator's REAL mouse pointer (`click_method: "global"`), and both were removed by product decision — `PolicyConfig.from_state` reads only `allowed_apps` / `extra_denied_apps`, so writing the flag has no effect and must not be re-documented without being re-implemented. That path is instead contained by requiring the model to NAME the method (`auto` never resolves onto it) and by a dedicated SEL `tool_kind` on every use.

- **Browsing has no keystone capability gate, deliberately (see [browser.md](browser.md)).** Presence of the `playwright-cli` binary on `PATH` makes the capability available, so there is no flag file to protect: the CLI exposes no capability gating to subset once an approved shell turn runs it. Presence is NOT an approval signal. Every dashboard invocation follows the ordinary shell approval ladder, and only an explicit trusted pattern, session trust, or auto-approve grant may skip the prompt. This prevents an unrelated existing install — or a planted launcher in an agent-writable PATH directory — from manufacturing its own grant. Uninstalling revokes availability. Because browsing is a shell command, it is governed on the `commands` plane and an `mcp`-scope deny does not reach it.

- **Operator OAuth consent-endpoint extension (keystone leaf `oauth_endpoints.json`)** — the security module's OAuth banner-safety contract (`security.oauth_url_contains_credential`, and `_exfil_url_warning` under `allow_oauth_entropy=True`) exempts standard front-channel params (`state`, PKCE, …) from the base64-blob/query-length heuristics only at an exact-match `(host, path)` in the code-owned `_OAUTH_AUTHORIZATION_ENDPOINTS`. `~/.kiro/crew/oauth_endpoints.json` (`{"additional_authorization_endpoints": [{"host", "path"}]}`) is the operator's escape hatch for identity providers outside that launch set (Okta orgs, Auth0, self-hosted OIDC, tenant-scoped Entra paths): `security._load_operator_oauth_endpoints()` unions strictly validated entries with the builtin set at check time (`_approved_oauth_authorization_endpoint`, memoized on the file's stat so a hand-edit takes effect on the next check without a restart). **Enforcement point:** the dashboard's live MCP OAuth banner validates URLs with this same gate — `_emit_mcp_oauth_request` in `chat_runner.py` calls `security.oauth_url_contains_credential` directly — so an operator endpoint entry governs the banner path as well as every other consumer wired to the contract gate. Each entry widens a trust boundary, so the file is on `_CREW_SECRET_LEAVES` (full read+write keystone block on both the tool path and every shell form) — an agent must not be able to author its own exemption — and there is deliberately no dashboard writer; the operator hand-edits it out-of-band. Every read fails soft to the EMPTY set (missing/unreadable/corrupt/non-object file, mirroring `computer_use.enable_state.load_state`), invalid entries are skipped individually with a warning (no wildcards, schemes, ports, userinfo, percent-escapes, IP literals, `..`, whitespace, or backslashes; hosts are lowercase-normalized DNS names with a letter TLD, paths exact and case-sensitive), and the entry list is truncated at 50 before validation so a mangled file cannot amplify. HTTPS-only / no-explicit-port / exact-match stay enforced by the gate logic and are NOT relaxable via the file, and the exemption grants exactly what the builtin set grants — fixed-credential patterns, heavy percent-encoding, userinfo, fragments, backslashes, and unknown-param heuristics remain unconditional. The markerless bare-secret entropy heuristic follows the same exact endpoint/parameter scope instead of scanning entropy-bearing recognized parameter values first; parameter names, unknown parameters, and non-query components remain in its scan target. That exemption is additionally bounded to the shapes the protocol itself can emit (`_oauth_entropy_value_is_protocol_shaped`, judged on EVERY decoded form: it percent-decodes until the text stops changing, bounded by `_MAX_URL_DECODE_PASSES`, and refuses a value still decodable at the bound, so `%252F` cannot launder the standard alphabet past a single decode): base64url emits `-`/`_` and never `+`/`/`, and an S256 `code_challenge` is base64url of a 32-byte digest, so it is exactly 43 characters. A base64-standard-alphabet run — the shape of an AWS secret key — therefore cannot ride `state`, `nonce`, or `code_challenge` into the blanked set. The residual is narrower but real: a markerless 40-character credential that happens to be alphanumeric is indistinguishable from ordinary base64url state entropy and is accepted only at this boundary; general output redactors retain the heuristic. An approval that came from an operator entry (not the builtin set) emits a best-effort `oauth_endpoint_extension_used` SEL event, deduped per process per endpoint. **Rejections name the endpoint, never the values:** `security.sanitized_oauth_endpoint(url)` returns the lowercase host + path of a rejected authorization URL (query/fragment/port/userinfo are never included; both components are scanned at every percent-decode layer up to the gate's own `_MAX_URL_DECODE_PASSES` budget — a credential-bearing or budget-exhausting path self-redacts to the shared tag, a credential-bearing host makes the helper return `None`, a non-ASCII host is surfaced in IDNA A-label form; both components are length-capped; unparseable URLs return `None`). The banner path (`_emit_mcp_oauth_request`) surfaces that pair in the rejection text — which also spells the `{"additional_authorization_endpoints": [{"host", "path"}]}` entry shape — and inside the `error` meta field the dashboard's failed banner actually renders; no additional meta keys are emitted because no shipped surface reads any. So the user can tell WHICH endpoint tripped the scanner and what to write into `oauth_endpoints.json`, without the rejection ever echoing state/PKCE material (#7578).

**Privacy-safe OAuth rejection diagnostics.** `security.diagnose_oauth_url_credential()` returns `None` for an accepted URL or an `OAuthUrlCredentialDiagnostic` for the first rejecting sub-check. The record carries only a stable `rule`, a URL-component category, an optional code-owned standard query-parameter name, and a shape profile: total length plus counts of ASCII uppercase, ASCII lowercase, digits, percent signs, URL punctuation, and all other characters. `oauth_url_contains_credential()` retains its boolean caller contract and logs that same bounded signature when it rejects. The diagnostic path does not change a rule, add a bypass, retry, or retain a URL. The URL and parameter value are never returned, logged, persisted, hashed, sampled, or represented by a prefix/suffix; malformed, credential-shaped, and unrecognized parameter names are omitted rather than echoed. This is sufficient for a controlled mint loop to distinguish standard OAuth entropy false positives (for example, `credential_scan_bare_secret_raw` on `state` versus `exfil_query_length`) without creating a second credential-bearing sink. An entropy-bearing recognized parameter value at an approved endpoint produces no diagnostic on entropy alone, while the same shape in an unknown parameter retains the stable `credential_scan_bare_secret_raw` rejection signature.

**Windows UNC trusted-root gate** (`unc_probe_allowed` + `validate_file_path` in `hooks.py`) — a UNC path names a HOST, so resolving or stat-ing untrusted UNC-shaped text (`\\evil\share\x.png` or `//evil/share/x.png` echoed in any message or query) makes Windows open an outbound SMB connection to an attacker-named host. `validate_file_path` therefore consults `unc_probe_allowed` **before any resolution** on Windows — the ordering is the control, since `realpath` on UNC text is itself the probe — and the gate's comparison is purely lexical (`normcase`/`normpath`), never touching the network. Filesystem access is restricted to UNC paths under three trusted roots, all admitted on the same basis (directories this gateway itself writes to): (1) the crew **data home** — on a roaming profile the home directory is itself a UNC share, the one legitimate source of UNC attachment paths; (2) the **temp directory** — channel-side image staging; (3) the **kiro agents directory** (`<kiro home>/agents`) — `apps.bridges._register_agents` and `agent.rebuild_agent_config` write the managed specs there, and it is a *sibling* of the data home on the same share, so before #6721 its absence made `_read_agent_spec` silently read every user-level agent spec as absent on a UNC home. The prefix comparison is separator-boundary-anchored (a sibling share on the same host, or an `agents-evil` neighbour directory, is refused), a root that is not itself UNC-shaped admits nothing (the roots cannot become a bypass on an ordinary local home), and the agents root is **memoized per configuration** (keyed on the raw `KIRO_HOME` and the accessor identity): `kiro_agents_dir()` resolves `KIRO_HOME` with filesystem I/O — on a UNC-shaped override, an SMB touch — so it must not run per gate check on hot/async validation paths, and a computation failure memoizes the root as absent fail-safe (the gate stays total; recovery is an env change or restart, and the degraded state is the pre-#6721 status quo). The **project-level** agents dir (`project_agents_dir`) is deliberately NOT admitted: an arbitrary project directory is not gateway-written, so admitting it would be a genuine trust-boundary widening rather than a repair.

**Write-only config protection** (`is_sensitive_write_path` in `security.py` + `hooks.py`) — runtime config files are protected against *modification* by agent tools while staying *readable*:
- `~/.kiro/crew/config.json` and `~/.kiro/crew/config.local.json` are in a write-only tier (`_WRITE_PROTECTED_HOME_PATHS`, expanded under every `_CREW_HOME_PREFIXES` entry so the pre-move legacy copy is covered too), deliberately NOT in the read+write `_SENSITIVE_HOME_DIRS` list above — the dashboard file viewer, `cat`, and knowledge indexing legitimately read config.
- `is_sensitive_write_path(path)` is a superset of `is_sensitive_path(path)`, sharing the same `_path_in_home_dirs` resolve/casefold core so the two gates can't drift. `hooks.on_tool_call` denies a file-EDIT tool call (ACP `edit` kind) whose `path`/`file_path` resolves to a config file.
- Empty/unknown ACP tool kinds are intentionally left to the load-time clamp backstop rather than hard-denied, to avoid over-blocking config reads that arrive without a kind (governance's shape inference can apply both read+write scopes because it is a permissive policy intersection; this gate is a hard deny). Bash writes (`tee`, `>`, `sed -i`) likewise fall to the clamp.
- The operator edits config out-of-band via the dashboard config API / CLI, which do not route through this gate.

**Array-nested target paths bind on both planes (issue #6558).** A batch-shaped tool carries its real targets inside an array argument (`{"operations": [{"mode": "Line", "path": …}]}`). The sensitive-path keystone in `hooks.py` (`target_paths` / `TargetPaths`) was made nesting-aware first; the governance INTERSECTION plane (`platform/governance.py` `_tool_arg_paths` / `classify_tool_args`) previously read only the TOP level, so a nested path produced no `(scope, item)` pair, `gate_decision` hit its permit-by-default `if not pairs` branch, and an operator ceiling denying `filesystem.read`/`filesystem.write` outside the workspace never bound on the nested spelling. The bounded, depth-aware, iterative walk now lives in ONE shared lower-level module, `kiro_crew.platform.tool_paths` (stdlib-only, imports neither `hooks` nor `governance`, so there is no cycle — `hooks` imports `governance`), and BOTH planes delegate to it. The third extractor, `hooks._SEARCH_DENY_ARG_KEYS`, stays flat by design (documented residual below) and is out of scope.
- **Truncated-scan policy on the permit-by-default plane.** The shared walk is bounded (`_TARGET_PATH_MAX_PATHS`=256, `_TARGET_PATH_MAX_NODES`=10_000) and reports a `truncated` flag. The `hooks` keystone fails SAFE by hard-denying any truncated scan. The governance plane is permit-by-default and must NOT blanket-deny an ungoverned standalone host, so on truncation it emits the filesystem scope(s) the tool kind implies (`edit`→`filesystem.write`; `read`→`filesystem.read`; unknown-kind-without-command→both) against a synthetic, never-permittable item (`_TRUNCATED_SCAN_ITEM`, containing a NUL byte so no allow-list pattern can match it). Effect: a prefix-bounded ALLOW-mode ceiling that confines the scope to a workspace DENIES the unverifiable call (closing the "bury the path past 10_000 nodes to escape the ceiling" fail-open), while an ungoverned scope still permits it (permit-by-default preserved). (A catch-all ALLOW pattern — `**`/`/**`/`*` — does match the marker via fnmatch and permits, but such a ceiling confines nothing and is unconstrained anyway, so this is consistent with its own posture rather than a bypass.) A DENY-mode ceiling that blocks only specific paths permits the marker — a targeted deny is not a general confinement and a partial scan cannot prove the buried path hit that one pattern; the always-on resolved keystone remains the authoritative guard for the sensitive tiers there. This resolves issue #6558 open-question-2 (option (c)); rejected: (a) permit-as-before keeps the fail-open, (b) unconditional deny over-blocks ungoverned hosts and every unrelated scope.

**Data-home completion-marker protection** (`.data-home-ready`) — the marker whose presence makes `~/.kiro/crew` authoritative (migration is skipped once it exists, and a leftover legacy home is treated as debris). Because its mere *presence* is the trust signal — and, unlike config files, **no load-time clamp neutralizes a planted value** — a prompt-injected agent that could create it in a pre-migration home would make the next boot skip migration and ignore the legacy home's governance policy + secrets (deleting it forces a needless re-migration). It is therefore protected on *both* enforcement layers:
- **File-edit tool gate**: the marker is in `_WRITE_PROTECTED_HOME_PATHS` (under every `_CREW_HOME_PREFIXES` entry), so `is_sensitive_write_path` denies an ACP `edit`-kind write to it while reads stay allowed.
- **Bash gate**: the marker leaf is also in `_WRITE_PROTECTED_BASH_LEAVES`, and `is_sensitive_bash_command` matches it **verb-independently** (any command naming the home-anchored marker path, including a trailing-`/` subpath so `mkdir -p …/.data-home-ready/x` — which also materializes it — is caught). This mirrors the verb-independent backstop the sensitive-dir matcher uses, so quoted redirects / `cp` / `python open()` / novel write verbs cannot bypass an enumerated allowlist. Bash *reads* are incidentally blocked too — harmless, since the marker holds no secret (it is deliberately NOT in `_SENSITIVE_HOME_DIRS`, so file-read tools and `is_sensitive_path` are unaffected) and the only legitimate readers (`kirocrew doctor`, the migration code) use Python `os` calls, not bash.
- The migration code stamps the marker directly in Python (not via a tool/bash), so legitimate stamping is unaffected. As with credential paths, the bash gate is home-anchored, defense-in-depth. A `cd`-into-home + bare-relative-leaf write is no longer a blind spot for a POSIX-spelled path: `is_sensitive_bash_command`'s second pass tracks the working directory across `&&` / `;` / newlines / subshells, resolves relative operands against the directory a preceding change-directory verb moved to, and recognises that verb in every shell's spelling (`cd`, `pushd`, `chdir`, `sl`, `Set-Location`, `Push-Location`) with cmd.exe and PowerShell home anchors (`%USERPROFILE%`, `$env:USERPROFILE`) folded to `~`. A separate monotone taint pass then denies any read that follows entry into a fenced directory, and no later token can clear that taint — `popd` / `Pop-Location` are deliberately not modelled, so undoing the move does not walk the denial back. What the walk still cannot see is a relative leaf spelled with NATIVE WINDOWS SYNTAX: the tokenizer runs in POSIX mode, where a backslash is an escape and a single `&` backgrounds rather than sequences, so `cd ~ & type .aws\credentials` reads as neither a separator nor a boundary. A third pass answers that without modelling any grammar at all, the same move the monotone taint pass makes for a sensitive target: it asks whether an entry into the HOME directory was seen anywhere in the raw command, and whether a fenced path spelled RELATIVE to it appears after that. It answers both by CUTTING the command into words, NORMALISING each word as a path, and comparing the result — not by matching spellings. That distinction is the whole design: `%USERPROFILE%\.`, `~/`, `C:.aws`, `C:/` versus `C:\`, `a\..\`, `a\b\..\..\` and `.aw^s` all name one file, and path identity is a computation (collapse `.`, net `..` against depth, unify separators, apply cmd.exe's `^` escape) that a pattern can only ever approximate one enumerated spelling at a time. Normalising instead closes the cmd.exe `&` sequencer, its `^` escape at every position, its glued `/D` switch, delayed expansion (`!USERPROFILE!`), drive-relative prefixes, no-op `.` and cancelling `..` chains, and a PowerShell pipeline (which, unlike a bash one, does not fork the directory) with one bounded function. The escape is applied per WORD, never to the whole command, which is what makes it safe: a word is what the shell hands the program as a single argument, so removing an escape from it cannot rewrite an unrelated argument — while `^^` still collapses to one literal caret, so `.a^^ws` stays the distinct file it is. A `..` that climbs above the starting directory marks the path as having escaped and is excluded, because it names a different file than the fenced one. It stays grammar-free because a word ends at an operator but WHICH operator is never asked; only target selection stays inside one operator-delimited run, while the fenced-path search crosses every boundary, since a monotone scan may only widen. Cost: naming a fenced relative path after entering the home directory is denied even when the command would not have read it, which is the posture the absolute-path pass already takes.

**Spec Builder's decision record** (`trust/spec-builder-decisions.json`) — the app
refuses a second answer for the same normalized question. Each record is bound to a
fingerprint of the rendered id, title, and order-independent option set, so reordering the
same choices cannot reopen a settled question while an agent reusing an id for a new question
does not inherit the old answer. A claim is first persisted as a pending outbox
entry and is marked final only when the chat runner reports that the model consumed the
prompt. Immediately before model dispatch, the row moves durably from `pending` to
`relayed`; a failure to persist that boundary refuses dispatch. A crash before consumption
leaves either state for the recovery flow; an already-persisted chat row is
reused rather than appended twice, but is not itself mistaken for proof of model
consumption. The detail GET reports `decision_recovery_pending` for either durable
outbox state; it never dispatches an agent turn. The SPA follows that signal with the CSRF-protected
`POST /api/apps/spec-builder/specs/{name}/recover-decision`, which performs the
replay and lets the next detail poll observe the running turn. Immediately before
any replay, the backend revalidates the question fingerprint and offered option
against the normalized current state. A mismatched
`pending` row with no chat marker is removed rather than relayed or finalized. A
`relayed` or chat-marked row is retained fail-closed because a crash after model
consumption but before ledger finalization is indistinguishable from a pre-model crash.
Recovery skips a retained `relayed` row once its question is provably stale so that the
ambiguity marker cannot permanently starve a newer current answer behind it.
If a failed turn requeues the delivery with consumption callbacks, the generic queue
editor refuses to replace that entry: those callbacks can settle only their original text.
App tokens cannot send or recover these human-authored turns; each denial is recorded in SEL.
The durable prompt is rebuilt from the backend-validated title and selected option; its
bound includes both normalized fields so replay cannot truncate the immutable answer.
Every Spec Builder dispatch boundary (decision answer, ordinary message, and execution
handoff) also re-reads each indexed name for the spec directory after its last await and
compares every live slot's task identity and monotonic turn generation with the initial
busy scan. The generation survives normal teardown clearing `slot.task` back to idle, so
this catches an alias turn that starts and finishes during validation as well as an alias
the agent adds mid-turn; the synchronous final check and task publication are one
event-loop step. Create registration uses the same normalized directory identity while
holding that directory's turn lock and refuses an index entry for any second name that
already points at it. Filesystem equivalence is checked by directory identity, so Windows
and case-insensitive macOS variants must not mint two slots that dispatch agents into the
same files; macOS arbitration folds case conservatively before the index transaction so a
create cannot race delete cleanup. An upgrade-state alias, agent-written alias, or sole
index path rewritten to a filesystem-equivalent spelling fails closed when it differs
from an immutable lexical key already present in the protected ledger. Detail reads,
new-spec registration, decision claims, and deletion can therefore neither mint a second
answer record nor strand the first one under an unreadable spelling. Decision claims
validate aliases and persist the answer from one protected-ledger snapshot, and refuse an
unreadable snapshot rather than retrying the write from different state. A handoff that
already armed its bounded nudge loop unwinds that loop and its execution claim when the
final alias check refuses dispatch. A process-owned generation, rather than agent-writable
index status or timestamps, authenticates the handoff's pre-dispatch claim. Handoff checks
that generation inside the directory turn lock before making the durable `executing` claim,
after authorization, and again after its final alias scan. Stop revokes the generation before
waiting for the lock and refuses new handoffs for that creation until it commits, so a Stop
that overlaps startup prevents the older request from dispatching or a newer request from
restarting behind it. Revocation remains provisional while Stop or Delete validates and
tears down the captured creation: both execution and ordinary-turn tokens are removed only
by a successful authoritative commit. A stale or failed control restores them; if a handoff
already observed the provisional revocation and unwound, a supervised settlement restores
its durable status to planning rather than leaving a dead `executing` claim. Rollback also
reconciles ordinary published turns whose completion callback fired while their token was
provisionally revoked, so an already-idle slot cannot retain an exclusive claim indefinitely.
The client creation claim is validated before that barrier is published, and the barrier
only revokes the matching verified name/slot creation, independent of its mutable directory
spelling, so a stale control cannot cancel replacement startup while a valid Stop cannot
miss it after a rewrite. The final alias scan also requires the current slot entry to retain
its captured slot identity and original lexical directory, whether the rewritten path is
equivalent or different. Every turn awaiting that scan also holds a process-owned
pre-publication token; Stop and Delete provisionally revoke matching tokens by normalized
directory, verified name, or slot before waiting for any directory spelling, so a control
that completes through a rewritten entry cannot be followed by an older task publication.
Pending tokens and handoff execution claims also exclude a different identity view by
normalized directory, verified slot, or name, so
rewriting the index during either request's final scan cannot start a second generation under
a different lock. An exact published identity still accepts established same-slot queuing.
Ownership transfers to the published slot task and follows queued successor turns until
the slot is idle, and an autonomous handoff retains ownership across idle gaps for the
lifetime of its armed nudge loop. Stop and Delete capture both the claimed slot and its
loop identity before revoking them, so an index rewrite cannot make a running turn or a
later nudge unreachable through the new slot key. The process retains the creation's first
authenticated directory as well as its slot key, so a generic embedded-chat turn is still
reachable when the agent rewrites its name, directory, and slot together. An observed creation
with no remaining valid index binding is included in dispatch admission and every authenticated
Stop/Delete teardown; a successful Delete releases every captured slot witness across old names,
not only the name on the current row. A surviving observed name remains its creation's control
endpoint even when the raw row removes, corrupts, or replaces its slot key, so an unrelated teardown
cannot misclassify and archive it as a global orphan. Index workers publish observed-name and
observed-directory witness maps by whole-map replacement; event-loop admission and teardown
readers therefore traverse stable snapshots rather than dictionaries a worker thread can resize.
Durable nudge loops participate in
dispatch admission after restart and are matched by verified name ownership or their original
sentinel directory; cold-start name and directory witnesses survive a missing or invalid raw
slot key, and an empty global scan never treats an unrelated empty sentinel as a direct match.
A loop whose name, directory, and slot no longer match any valid index
entry is treated as an orphan: dispatch fails closed and Stop/Delete captures it, because no
replacement entry can safely claim exclusive ownership of that unattended run. When no index
entry remains to provide a Stop/Delete URL, Create opens a service-owned maintenance transaction
even when AutoNudge is disabled. The transaction is serialized with service startup and peer
cleanup, persistently pauses each orphan, waits for both the captured firing callback and a timer
replacement installed during that pause, then re-reads and archives its worker before any
worktree, spec-directory, or index side effect. The inactive loop
remains as a restart-durable recovery marker until every worker archive succeeds, so a timeout or
crash cannot make a retry forget the old turn. A failed final loop-store removal restores the
in-memory marker and emits no removal event, so the next cleanup can retry the durable delete.
This also covers a direct embedded-chat turn with no loop. Cleanup or transcript-archive failure refuses Create so the old worker cannot overlap the
replacement, and successful recovery removes the loop and releases the old process witness before
the new creation mints its slot key. Detail status also follows a restored
loop by its sentinel
directory, keeping Pause visible when the current row carries a rewritten slot key. Once a
process observes a per-creation slot key, an agent-written different key for that name cannot
change the live slot resolver. Detail and destructive controls use that authenticated key for
the worker while retaining the raw key only as the compare-and-swap identity of the mutable
index row. App-owned deletion or create rollback releases every captured spelling so a
same-name recreation can mint a new worker. A legacy entry
without `slot_key` is upgraded
atomically to its name-derived identity only when this process has not already observed a
per-creation key for that name; removing a live worker's key therefore fails closed instead of
being misread as an upgrade.
New-spec registration also returns 503 before mutation or seed dispatch when the protected
decision ledger cannot be read, because transient unreadability cannot prove an alias safe.
Pre-consumption automatic retries carry
the settlement callback on their process-local queue entry, including across repeated
retries; a gateway restart drops that callback deliberately and the durable pending entry
re-arms it on replay. This file is therefore an **input to the
refusal and recovery path**, not a setting. An agent able to write it could erase an entry
to make a settled decision answerable again, forge one to lock a decision the user never
answered, or plant a pending prompt for the backend to relay. It lives under the
whole-directory `trust` entry rather than getting a leaf of its own, because gating the
leaf alone left its parent replaceable: a directory under `workspace/` is not itself a
sensitive path, so one `ln -s` naming it redirected every read and write — the app opens
the path directly, as keystone writers must, so it would have followed the link. It is
also deliberately NOT a field on the app's `index.json`, which is agent-writable by
design.

**Ops Mission Control authorization inputs** (`apps/ops-mission-control/data/rotation.yaml`,
`apps/ops-mission-control/data/incidents/index.json`) — two app-owned files that are
write-protected on both layers for the same reason as the marker above, and with the same
read/write asymmetry. They are not settings and no load-time clamp neutralizes a forged value:
they are **inputs to an authorization decision**.

- `rotation.yaml` is the committed on-call schedule. An agent that rewrites it to name its own
  login makes `rotation.authorize_action` → `_definitely_off_shift` accept a forged shift and
  execute an off-shift production write against a teammate's incident tooling.
- `incidents/index.json` is the incident store. `/incident/action` reads the incident by id and
  hands `incident.signal` to the same gate, whose act-rules key on `source` / `resource` /
  `labels` — so an agent that rewrites the record can pair a resource an operator's rule
  authorizes with a *different* provider target, and the gate approves one signal while the sink
  mutates another. Resolving the signal server-side (the fix used for the same defect on
  `/incident/claim`) cannot help, because the store IS the server's copy.

Both stay **readable** on the tool path, and that is load-bearing rather than incidental: every
teammate's instance must read the schedule to answer "am I on call?", and the index is the board
the dashboard renders. Neither holds a secret, so neither is in `_SENSITIVE_HOME_DIRS`. Bash
*reads* are incidentally blocked by the verb-independent leaf matcher, which is harmless for the
same reason it is harmless for the marker — the legitimate readers are the app's own Python code.
The gateway's writers (`store.claim` / `update_fields`, and `ledger_sync`'s `git checkout` on the
schedule) open these paths directly and do not route through the tool gate, so the app and team
sync keep working.

**Downloaded model weights** (`<data home>/models`) — write-protected on both layers, with the same read/write asymmetry, because the weights are an **input to a trust decision** rather than a setting. `stt.models` (and `embeddings`) verify a file against a pinned sha256 and then hand its **path** to a native loader that re-opens it by name, so a writable directory leaves a window between the digest and the open. Re-hashing cannot close it — the loader re-opens by name — and neither can a metadata memo, since `os.utime` is available to anything that can write the file. Removing the writability is what closes it.

- **Tool path**: `models` is in `_WRITE_PROTECTED_HOME_PATHS` under every `_CREW_HOME_PREFIXES` entry. A directory rather than a leaf, so the trailing separator the pattern already accepts covers everything beneath it — the trust decision needs *any* file the loader might open, not one filename. Reads stay allowed (`is_sensitive_path` is `False`): the settings surface and `kirocrew doctor` both report what is installed, and weights hold no secret.
- **Bash gate, anchored**: the same entry in `_WRITE_PROTECTED_BASH_LEAVES`, matched verb-independently.
- **Bash gate, anchor-INDEPENDENT**: `_WHISPER_WEIGHT_NAME` (`ggml-*.bin`) is matched as a bare path **segment**, the same tier as `_BARE_TOKEN_PROTECTED_LEAVES`, because the anchored entry alone falls to a single `cd`: `cd ~/.kiro/crew/models; cp evil.bin ggml-base.bin` names no home, no crew prefix and no separator, and decides what a C++ GGML parser reads. Anchoring is therefore not part of this contract. A **pattern** rather than the four catalog filenames, so a row added to `stt.models.CATALOG` is fenced without a second edit in `security`. It is deliberately wider than the crew home (an unrelated GGML checkout cannot be copied from the agent's shell either); that is a denial, the safe direction, and the file tools are untouched. `models` itself must **never** join this tier — the SCOPE note there forbids generic names, and an unanchored `models` would refuse a large fraction of ordinary commands.
- **Shared terminator boundary** (`path_end`): this tier's patterns accepted only `/`, whitespace, end-of-string or a quote after a fenced path, which made flush punctuation a bypass for **every** entry — `cd ~/.aws;`, `cd ~/.ssh;` and `cd ~/.kiro/crew/profiles;` were allowed while the same commands written with `&&` were blocked, purely because `&&` is preceded by a space. The class is now every character a shell treats as the end of a word (`; & | ( ) < > , :` and a backtick). Widening a deny boundary can only deny more; the rule it enforces is unchanged. This is what catches the archive form (`cd <models>; tar -xf evil.tar`), where the weight name is inside the tarball and so unavailable to a name match.

`test_security.py::TestModelWeightsAreWriteProtected` pins all of it, including the false-positive cost of both widenings.

**App-sources checkouts** (`app-sources/`) — the persistent tree every installed app *executes*
from (`apps.registry.app_source_dir` → `<data-home>/app-sources/{name}`). The entry is a whole
DIRECTORY rather than a leaf, which `_path_in_home_dirs` already supports: it matches the entry
and its `entry + os.sep` prefix, so every file under every checkout is covered without
enumerating them.

This is the strongest instance of the write-only class, because the protected file *is* the
executed code rather than an input to a decision about it — an agent with ordinary file-write
tools could edit an installed app's source, which then runs with that app's privileges on the
app's next launch. Nothing downstream neutralizes it: unlike `config.json`, whose inflated values
the load-time clamp below rewrites, a modified checkout is simply run. Provenance does not catch
it either — `install_from_registry` records `_resolved_clone_commit` (the tree's real `HEAD`), and
an agent write dirties the worktree without moving `HEAD`, so a modified tree still reports the
pinned SHA.

- **File-edit tool gate only**, deliberately: `app-sources` is in `_WRITE_PROTECTED_HOME_PATHS`
  but NOT in `_WRITE_PROTECTED_BASH_LEAVES`. That matcher blocks on a command *naming* the path,
  which would deny bash reads too — and unlike the marker and the two Ops Mission Control files,
  reading app source is a routine, high-volume operation (the dashboard file viewer lists
  `app-sources` as a browsable root, knowledge indexing walks it, and reading an app's code is how
  anyone debugs one). Reads stay allowed on both paths; `app-sources` is not in
  `_SENSITIVE_HOME_DIRS`. This leaves shell writes on the same footing as `config.json`'s, where
  the tool gate is likewise the enforcement point.
- The gateway's own installer is unaffected: `_clone_build_app` clones, builds and prunes through
  direct Python/subprocess calls, which are not agent tool calls and never reach
  `hooks.on_tool_call`.
- An installed app's *data* directory (`apps/{name}/data/`) is a different tree and stays
  writable — apps persist state there through the agent's own tools.

**Load-time resource-limit clamp** (`config/loader.py`) — defends against a config-loader bound bypass: the dashboard config API rejects out-of-range writes, but a direct edit of `config.json` (any process as the same OS user, or a prompt-injected agent with file-write access) bypassed that gate.
- `KiroCrewConfig.load()` calls `_clamp_security_bounds(data)` on the disk-read path (before caching) so cache hits and the `GET /api/config/kirocrew` serialization both report clamped values.
- Clamped knobs: `agent.subagent_auto_max` ≤ `SUBAGENT_AUTO_MAX_CEILING` (64), `agent.max_subagents` ≤ 64, `agent.subagent_max_turns` ≤ `SUBAGENT_MAX_TURNS_CEILING` (200), `session.pool_size` ≤ `POOL_SIZE_MAX` (10). Mins match existing runtime floors (0/1); `bool` and non-int values are left untouched for dataclass coercion.
- The ceilings live once in `config.loader` and are imported by the API write-gate (`dashboard/handlers/core.py`) and the runtime pool cap (`session._MAX_POOL`), so the write-gate, runtime cap, and load-time clamp cannot drift.
- A clamp is logged at WARNING and recorded as a `config_bounds_clamped` SEL tamper event (best-effort, never fatal — config loading must not raise). This neutralizes any inflated on-disk value regardless of how it was written.

**URL exfiltration detection** — scans LLM output before posting to Slack/dashboard:
- `scan_exfiltration_urls(text)` — flags the payload not the destination (host-agnostic except the two narrow carve-outs below)
- Detects: long query strings (≥200 chars), base64 blobs (40+ chars), heavy URL-encoding, AWS access key IDs (`AKIA`/`ASIA`), SSH keys, private key headers, Slack tokens
- Hard credential markers (`_HARD_CREDENTIAL_RE`) are scanned across the **full path AND query**, not just the query after `?`, so a secret embedded in the URL path (`http://host/AKIA…`, no `?`) is caught (Talos 78224f3f). `_URL_RE` matches DNS names, **raw IPv4 literals** (incl. IMDS `169.254.169.254`), and **bracketed IPv6 literals** so a raw-IP exfil destination is not silently skipped. `_URL_RE`'s path/query group starts with `[/?]`, so a query attached **directly to the host with no path segment** (`https://host?leak=<secret>`) is captured and scanned too — previously that group required a leading `/`, so such a URL yielded no path/query group and both scan/redact bailed on `qmark == -1`, skipping the query entirely (exfil bypass). The base64-blob/query-length heuristics stay query-only (long base64 path segments — CDN asset ids, git object hashes — are benign); the S3-presigned exemption is applied before the path scan. Per-URL classification is a single shared helper (`_exfil_url_warning`) used by both scan and redact so the two paths cannot drift. **Exact-host heuristic exemption**: a companion `CredentialPolicy` may supply a set of trusted-tenant hosts (`_exempt_exact_hosts()`; the public Default returns an empty set) that skip **only** the base64-blob and query-length heuristics — the ones that false-positive on legitimate long base64 document pointers (e.g. SharePoint `nav=` links). Hosts are matched **case-insensitively** (both the captured host and the set members are lowercased, per RFC 4343) and **exactly** (not by suffix, so a shared multi-tenant domain does not exempt every tenant). The hard-credential floor (`_HARD_CREDENTIAL_RE`) **and** the heavy percent-encoding detector (`_EXFIL_PERCENT_RE`) stay **unconditional** — an AWS key / SSH-or-PEM header / Slack token / URL-encoded payload on an exempted host is still flagged and redacted. **Self-emitted Slack app-create link carve-out**: `kirocrew manifest --url` (`cli_setup.py`) and `GET /api/slack/manifest` (`handlers/messaging.py`) both hand the user Slack's new-app deep link with the bundled app manifest percent-encoded into `manifest_yaml`. That payload is ~1.9 KB, so the query-length heuristic classified the link as exfiltration and the user was shown `[REDACTED: suspicious URL to api.slack.com]` instead of the link `docs/guides/slack-setup.md` tells them to click. `_is_kirocrew_slack_app_link()` skips **only** the base64-blob and query-length heuristics, and it earns that by VALIDATING the payload rather than trusting the destination: exact `https` host `api.slack.com` + exact path `/apps`, no explicit port, the query's parameter set exactly `{new_app, manifest_yaml}` (a superset is refused — an extra parameter is the obvious smuggling shape), `new_app` exactly `1`, and the decoded manifest must `fullmatch` a pattern derived from `slack_manifest.stripped_template()` — the SAME render/strip procedure both emitters use, so the accepted payload cannot drift from the emitted one (`{{ALIAS}}` → a bounded alias group, every later occurrence a backreference so the alias cannot vary between the two places the manifest names it). **The alias does not ride free.** The helper returns the captured alias and the caller assigns it to `heuristic_query`, so the one caller-controlled span stays under the base64-blob heuristic; only the constant template bytes (which caused the false positive) are excluded. Zeroing the payload instead was a real bypass found in review on #2725: the alias slot accepted 64 chars of `[A-Za-z0-9_-]`, wide enough for a 40-char alphanumeric secret, which is exactly the run length `_EXFIL_PATTERNS` needs — `slack_manifest.ALIAS_MAX` (32) now makes such a run impossible AND the surviving span is still scanned, so an `AKIA…` id or a short `xox…` token parked in the alias is caught on the alias alone. **Residual, stated rather than implied:** an alias up to `ALIAS_MAX` chars resembling no known credential is exempt from the base64/length heuristics; this opens no NEW capability, because any URL at any host may already carry a query under `_EXFIL_QUERY_MIN_LEN` (200) chars without tripping either heuristic, so the span is strictly narrower than what is available without the carve-out. An unreadable template yields `None` and **fails closed** (full heuristics restored), because an install that cannot prove what its own manifest looks like must not exempt a 1.9 KB payload. This is deliberately NOT modelled as a host exemption: `_exempt_exact_hosts()` is companion-owned tenant trust, and adding `api.slack.com` there would exempt every URL at that host including a model-authored one — the same reasoning by which the OAuth carve-out refuses to exempt OAuth-shaped params wherever they appear. Because it runs at the heuristic-query selection step, every unconditional check still precedes it: `_HARD_CREDENTIAL_RE`, the canonical fixed-credential patterns, the multi-pass percent-decode and its fail-closed saturation branch, and `_EXFIL_PERCENT_RE` — so a secret appended to an otherwise-valid manifest is still caught (`test_credential_in_payload_still_redacted`). `test_security.py::TestKiroCrewSlackAppCreateLink` pins both directions, driving the **real emitter** (`slack_manifest.deep_link`) through the scanner rather than rebuilding the payload — a rebuild would let an emitter drift away from the validator with the tests still green, which is the same "no test exercised the real URL" failure that hid the original bug.
- `redact_exfiltration_urls(text)` — replaces suspicious URLs with `[REDACTED: suspicious URL to {domain}]`
- **Frontend mirror** (`website/src/utils/sanitize.ts::sanitizeExfiltrationUrls`): the browser-side redactor reuses the SIGNALS of `_exfil_url_warning` but layers them differently, and in the strict direction: on the backend base64 and length sit together on the waivable tier, both applied to `heuristic_query` after a span-subtraction step, whereas here every **pattern** signal is unconditional, running for every URL with no host and no carve-out able to escape it — `EXFIL_PERCENT_RE` (20+ consecutive percent-octets, mirroring `_EXFIL_PERCENT_RE`), `EXFIL_CREDENTIAL_RE` (AWS key id / SSH-or-PEM header / Slack token, mirroring `_HARD_CREDENTIAL_RE`) and `EXFIL_B64_RE` (40+ char base64 blob) — so the redactor flags every pattern an undifferentiated check would. **Two** signals false-positive on the prefilled GitHub issue link of the report (`…/issues/new?title=…&body=<prose>&labels=…`, rendered as `[REDACTED: suspicious URL to github.com]` in chat), and only ONE of them is waived. Aggregate query length (`EXFIL_QUERY_MIN_LEN`, 200) names no pattern at all — it fires on any richly-parameterised URL — and it is the check `isPrefilledIssueUrl()` waives. `EXFIL_B64_RE` is the second: `+` is the form-encoded spelling of a space, which is what `URLSearchParams` emits, so ~7 words of unpunctuated prose in a `+`-encoded `body=` are one 40+ char run in `[A-Za-z0-9+/=]`. That signal is left **in force on purpose and is not waivable**: narrowing the class to exclude `+`, or splitting the query on `+` before testing, would let an attacker `+`-chunk a 40+ char secret straight past it, which costs more than the false positive does. So the fix covers a prefilled issue query whose spaces are `%20`, or that otherwise carries a non-class byte inside every 40-char window (the shape the repo's own `MarkdownRenderer.longUrlLinkify` fixture has), and does **not** cover a purely `+`-encoded one — that spelling still renders as a placeholder, pinned as deliberate by `sanitizeExfiltrationUrls.test.ts`. `isPrefilledIssueUrl()` earns the length waiver by VALIDATING the URL rather than trusting its destination. That is closer to the backend's `_is_kirocrew_slack_app_link()` than to the companion-owned `_exempt_exact_hosts()` tier, but it is not the same move: the backend precedent keeps the caller-controlled span under **both** heuristics by narrowing the payload down to it, which is unavailable here because every GitHub prefill parameter value is caller-controlled and there is no constant-template span to subtract — so what is waived here is one signal rather than one span. Destination trust is unavailable too, because `github.com` is a public multi-tenant **write** sink: a submitted prefilled issue lands in whichever repository the URL names, an attacker's own included, so no destination trust is available to grant. Validated: exact `https` scheme, host exactly `github.com` (lowercased per RFC 4343, never by suffix, so `github.com.evil.example` is a different host), **no explicit port**, path `fullmatch`ing `/<owner>/<repo>/issues/new` where each segment is dot-SEPARATED, so neither may end with a dot or contain `..` (a traversal spelling browsers normalise away before sending, so it names a path GitHub never served) while a single leading dot stays legal because `.github` is an ordinary repository name, and every query key drawn from GitHub's documented prefill set (`EXFIL_ISSUE_PARAMS`) — a superset is refused whole, since an extra parameter is the obvious smuggling shape, and an empty or differently-cased key is one GitHub would not prefill from either. Every unaccounted-for component fails closed, restoring the length check. **Residual, stated rather than implied:** a query of any length at that one validated shape, carrying no 40+ char base64 run, no 20+ consecutive percent-octets and no literal credential marker, renders unredacted. That opens no new encoding capability, because a query under 200 chars at ANY host already rides through both without the carve-out; what the carve-out adds is length, at one shape whose every span is either a fixed literal or a GitHub-defined parameter. The exposure also begins at the CLICK rather than at the submit: following the rendered link hands the whole query string to github.com's servers and logs before the user decides whether to file anything, and only after that does the submitted issue land in whichever repository the URL named. The frontend does not percent-decode, so its markers are literal only, and `URL_RE`'s path/query group still requires a leading `/` (`(\/[^\s)"'>]*)?`) — so `https://host?leak=<secret>` yields no group and its query is never scanned. That is the bypass the backend closed by starting `_URL_RE`'s group with `[/?]`; it is pre-existing here, untouched by this carve-out, and the reason this bullet claims signal parity rather than wiring parity. It introduces **no regex lookbehind** (the file is in the eagerly-loaded entry chunk; at the `safari14` esbuild target a lookbehind literal is rewritten to `new RegExp()` and throws at module load, blanking the dashboard) and keeps the JWT credential-pattern parity pinned by `test/test_redaction_mirror_parity.py`. Both directions are covered by `website/src/test/sanitizeExfiltrationUrls.test.ts`, which pins each validated span as load-bearing and each pattern signal as unwaived.

**Credential output redaction** — catches raw credential patterns in LLM/tool output:
- `redact_credentials(text)` — scans for plaintext AND base64-encoded credentials
- Plaintext patterns: `AKIA`/`ASIA` access key IDs, `SecretAccessKey=`, `aws_secret_access_key=`, `SessionToken=`, `aws_session_token=`, PEM private keys (`-----BEGIN [A-Z ]*PRIVATE KEY-----`), Slack tokens (`xoxb-`/`xoxp-`)
- **Full-block PEM redaction** (`05687e60`): the PEM sub-alternative spans the ENTIRE key block (header + base64 body up to the END marker), not just the header phrase. Because `redact_credentials()` replaces the matched SPAN, a header-only match left the secret base64 body verbatim on every output surface. The body class is `[\s\S]*?` (not base64-only) so encrypted keys — whose `Proc-Type:`/`DEK-Info:` headers carry `:`/`,` — are fully spanned; a truncated block (no END) consumes only subsequent PEM body lines (each must start with a newline), so a `BEGIN` header mentioned inline in prose matches only the header and does not swallow trailing lines to end-of-string. **Round-3:** the trailing `(?=\r?\n[A-Za-z0-9+/=])` lookahead alternative lets the run cross a SINGLE blank line when the next line begins with base64 material — RFC 1421 ENCRYPTED PEMs place a MANDATORY blank line between the `DEK-Info:` header and the base64 body, and without this lookahead the per-line "must contain a base64 char" rule stopped at that blank line and leaked the whole encrypted body (for both a truncated key and a complete encrypted key whose body exceeds the full-block cap). Because the lookahead consumes nothing, TWO+ consecutive blank lines still terminate the run, so trailing prose is preserved (no over-redaction)
- **Third-party provider families**: ~12 distinctive fixed-prefix token formats added beyond AWS/Slack — GitHub (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` PATs + `github_pat_` fine-grained), GitLab (`glpat-`), Stripe (`sk_live_`/`rk_live_`/`_test_`), SendGrid (`SG.`), OpenAI (`sk-proj-`), Anthropic (`sk-ant-`), npm (`npm_`), PyPI (`pypi-`), DigitalOcean (`dop_v1_`/`doo_`/`dor_`), Google OAuth client secrets (`GOCSPX-`) — plus DB connection URIs with embedded credentials (`postgres`/`mysql`/`mongodb`/`redis`/`amqp` `://user:pass@`). Prefixes are case-sensitive with minimum lengths set slightly below real token lengths (over-redaction on a prefix match is the safe direction)
- **JSON-aware key-value matching**: key-value patterns allow an optional quote (`[\"']?`) between the key name and the separator (`[:=]`), matching both bare `aws_secret_access_key=VALUE` and JSON `"aws_secret_access_key": "VALUE"` formats. The value class uses `[^\s"',}]+` (bounded, stops at JSON structural delimiters) rather than greedy `\S+`, preventing over-capture in compact JSON that would swallow adjacent fields and mask subsequent credentials
- **JWT / JWE / OAuth Bearer tokens** (cc1d6bdd; JWE hardening a8e5fe6a; JSON-aware Bearer): JWTs (`eyJ<header>.<payload>.<sig>` — `eyJ` is the base64url of the `{"` header prefix) and HTTP `Authorization: Bearer <token>` headers. The `eyJ` segment quantifier is `(?:\.[A-Za-z0-9_-]*){2,4}` so it redacts both a 3-segment signed JWT (JWS) and a 5-segment encrypted JWT (JWE, RFC 7516 — `header.encrypted_key.iv.ciphertext.tag`) as one whole token — including `dir`/`ECDH-ES` JWEs whose Encrypted Key segment is EMPTY (`header..iv.ciphertext.tag`); the earlier fixed 3-segment pattern truncated a JWE and leaked its ciphertext + tag. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url prefix); the Bearer header name + scheme are matched case-insensitively via scoped `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230 §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is case-insensitive (RFC 6750 §2.1) — so lowercase `authorization: bearer …` from `requests`/`net/http`/HTTP2 frame logs is redacted too. The header/scheme separator is JSON-aware: an optional quote may precede the `:`/`=` and the token (`(?i:Authorization)["']?\s*[:=]\s*["']?(?i:Bearer)…`), so a serialized `{"Authorization": "Bearer <tok>"}` in a structured-log/JSON request dump is redacted, not just the raw HTTP header. Both are scoped tightly — the JWT segment class `[A-Za-z0-9_-]` cannot cross the literal `.` separators, and the Bearer token class (`[A-Za-z0-9._~+/-]+=*`, RFC 6750 `b64token`) stops at whitespace/quotes — so neither over-captures. A `Bearer` header carrying a JWT redacts as a single match (the Bearer alternative's class subsumes the JWT), while a bare JWT is caught independently (defense in depth). Bare `eyJ…` with no `.`-segments and the word `Bearer` without the `Authorization:` prefix are NOT redacted (no false positives). **Two-segment dashboard link token**: `dashboard.token_auth.generate_token` emits `base64url(payload).base64url(hmac_sig)`, which is TWO segments, so the `{2,4}` quantifier never matched it and the token fell through to the pass-3 bare-secret heuristic, whose run class `[A-Za-z0-9+/]` is standard base64 and excludes base64url's `-`/`_`. Redaction therefore depended on the alphabet of a random HMAC signature. That rate is derivable, so it is stated as a closed form rather than as a sample: HMAC-SHA256 is 256 bits and base64url-unpadded gives 43 chars, of which the first 42 each carry a full 6 bits (uniform over the 64-char alphabet, exactly 2 of which are `-`/`_`) while the 43rd carries only the leftover 4 bits (256 - 42*6) in the HIGH bits of its 6-bit group, low 2 bits zero, so it spans exactly the 16 alphabet indices divisible by 4 (`048AEIMQUYcgkosw`), never `-`/`_` at 62/63 (verified by encoding all 256 possible final digest bytes). Hence P(no `-`/`_`) = `(62/64)^42` = 26.4%, so roughly a quarter of tokens had only the signature replaced and the payload claims stayed verbatim in a URL that still looked complete but no longer authenticated; the other ~74% were emitted with no redaction at all. (An earlier 400-signature estimate published 29% here. Two 5000-mint runs land at 26.0% and 27.1%, straddling the closed form, so the published figure was a small-sample artefact.) The link token now has its OWN alternative, `(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])`, ordered AFTER the `{2,4}` one so a real JWS still redacts whole instead of matching `header.payload` and leaving `.signature` exposed. The `{2,4}` floor was deliberately NOT relaxed to `{1,4}`: the alternative has no left boundary and its post-header segments allow an EMPTY match, so `{1,4}` matches ordinary code and prose (`keyJson.get(raw)` becomes `k[REDACTED: credential](raw)`, and a JWT quoted at the end of a sentence loses its trailing period). The segment lengths come from the generator rather than from guesswork, because a length FLOOR alone is beatable by a verbose enough identifier: at `{40,}` the 40-char `eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue` matched. `token_auth._sign` is HMAC-SHA256 base64url-unpadded, so the signature is EXACTLY 43 chars for every token ever minted, a property of the digest and not of the payload, so it is pinned as `{43}`; `test_link_token_signature_is_43_chars` fails loudly if that digest changes rather than letting redaction silently stop matching. `generate_token` always emits `sub`/`exp`/`session_exp`/`iat`/`nonce`/`gen` with a 16-hex-char nonce and float timestamps (`app`, `prompt` and `extra` only add), so payload length is not fixed: it scales with `len(sub)` and with the repr width of each float timestamp, which base64 quantises into 4-char steps. The floor is therefore derived rather than sampled: a 1-char `sub` (the narrowest a caller passes), `gen=0`, and all three timestamps at their shortest 12-char repr (an exactly-integral `time.time()` in the current 10-digit epoch era) measures 145 chars past `eyJ`, leaving the `{96,}` floor 49 chars of headroom. ONLY that derived floor is pinned; live payload sizes are not, because the spread moves with float reprs and caller mix (measured 168-185 for the mandatory-only callers, and 192-223 for the two that also pass `app=`, which adds an `"app"` claim). `test_link_token_payload_clears_the_96_char_floor` pins the derived floor so a shorter claim set fails loudly instead of silently disabling redaction. The left boundary includes `.` so an attribute access (`obj.eyJsonReader.readValueFromInputStream`) is excluded. A false positive here is not purely cosmetic: `chat_runner.py` redacts file-diff chip bodies IN PLACE before persistence ("so both the live and persisted views are clean"), so a bad match is written to the persisted view with no recovery path. Artifact content and compressed history are redacted on the serialization/output path instead (`handlers/artifacts.py::_serialize`), so those surfaces are not rewritten on disk. The product's own link delivery is unaffected by construction: `slack/allowlist.py::send_dashboard_link` builds the presigned URL and posts it via `slack.post_message` without calling `redact_credentials()` (and `slack/client.py` does not redact internally), so `!dashboard` and `kirocrew token` are outside this path. What the fix closes is an agent-authored URL carrying a live token into chat, which is the XPIA data-exfiltration row of the threat table above
- Base64 detection: finds 40+ char base64 chunks, decodes them, checks if decoded content matches any credential pattern
- **Bare label-less secret-key detection** (`bf7b1baf`): a 40-char AWS *secret access key* (the value paired with an `AKIA`/`ASIA` ID) is a bare base64 run with NO prefix and NO `key=` label, so the fixed-format patterns above miss it when it appears standalone (echoed alone, in a log line, in a JSON array element). A third redaction pass adds an entropy + structural heuristic: `_BARE_SECRET_RUN_RE` isolates each `[A-Za-z0-9+/]{40,}` run (word-boundary look-arounds so surrounding prose is preserved), then `_looks_like_secret_key()` applies every gate below — a token must clear ALL of them (design bias is toward NOT redacting: a false negative reverts to prior behavior, a false positive corrupts benign output). Gates, ordered by measured cost per rejection (cheapest first): (1) length is EXACTLY 40 (AWS secret-key length); (2) contains lower + upper + digit (rejects all-lower prose, ALL-UPPER constants, base32, digit runs); (3) not an all-hex run (`_HEX_ONLY_RE` rejects 40-char git SHAs and 32/64-char md5/sha256 digests — verified even for mixed-case hex that would otherwise clear the entropy gate); (4) the longest run of consecutive lowercase letters ≤ `_SECRET_MAX_LOWER_RUN` (5), decided by `_lowercase_run_exceeds(token, cap)`, which stops as soon as a run reaches `cap + 1` instead of measuring the longest run in the whole token; (5) vowel ratio ≤ `_SECRET_MAX_VOWEL_RATIO` (0.30); (6) Shannon entropy ≥ `_SECRET_ENTROPY_MIN` (4.3 bits/char — real random keys average ~4.78 and rarely drop below ~4.4, while camelCase identifiers and file paths cluster at 4.0-4.3; the canonical AWS example scores 4.66); (7) does not base64-decode to ≥85% printable ASCII (`_decodes_to_printable_text` leaves encoded-text blobs to the decode-and-scan pass). **The order of gates 4-7 is a performance property, not a correctness one:** all four are pure predicates that return `False` on failure, so every permutation produces the same verdict on every input — which is exactly why a behavior test cannot pin it. Entropy used to run first and was therefore paid on every window that cleared gates 1-3, even though the two structural gates reject more per microsecond. Measured over the windows reaching this point: lowercase-run 1.65 µs at 66.5% rejection, vowel 2.89 µs at 62.3%, entropy 8.48 µs at 54.5%, decode 3.01 µs at 0%. Ordering by cost per rejection cut `redact_credentials()` on a 51 KB payload from 69.8 ms to 27.0 ms with byte-identical output. `TestSecretGateOrderIsCostOrdered` counts gate evaluations and fails if the order regresses to entropy-first; do not reorder these four back without re-measuring. Both structural gates apply to EVERY token: unlike a naive design, the presence of `/` or `+` is **not** a free pass to redact, so a 40-char mixed-case file path (e.g. `src/main/java/com/Example/FooBarBazClas1`) — which contains `/` yet is built from dictionary-word segments with long lowercase runs — stays intact. The pass scans the ORIGINAL text (stable offsets) and skips any run already redacted by pass 1/2. Tests (`test_security.py::TestBareSecretKeyRedaction`) prove true positives on real secret shapes and NO over-redaction of git SHAs, UUIDs, sha256/md5 hex, base32, prose, code identifiers, or slash-delimited file paths. **Glued-secret sliding window**: `_looks_like_secret_key()` only accepts an EXACTLY-40-char token (gate 1) — its documented boundary assumption — but `_BARE_SECRET_RUN_RE` captures the *longest* base64 run, so a real 40-char secret glued to an adjacent base64 char with no delimiter (`X`+secret, secret+`A`, `SECRET=`+secret+`ABC`, secret+`X`+secret) forms a 41+ char run that fails the exact-40 gate and would leak verbatim. Pass 3 therefore gates each captured run through `_contains_bare_secret()`, which slides a 40-char window across the run and redacts the whole run when ANY window clears every gate; this stays linear (the regex yields disjoint spans). The sliding window does not over-redact >40-char benign camelCase identifier runs (no window within them looks like a secret)
- Applied on **every** output path — each boundary where agent output reaches a human or an external service. The authoritative list is the `redaction_paths` control in `security_posture.py` (see "Security Posture Detail Registry" below), which is what Settings → Security renders; do NOT restate the count as a literal here (this line read "ALL 5 output paths" long after the real number had multiplied, and the dashboard's hardcoded pill inherited that stale 5)
- **Deny-surface tool titles** (`dashboard/chat_runner.py`): `event.title` prefers the model's own `description` field (`_select_tool_title`), so it is agent-controlled display text. Every permission-deny surface — the 🚫 blocked transcript row (broadcast AND persisted to the ConversationLog) and the SEL audit `tool_name` — renders it only through `_redact_display_text()` (both redactors, idempotent, byte-identical for clean titles). The two deny shapes are rendered in exactly one place each: `_reject_invalid_tool()` (name validation failed) and `_reject_hook_error()` (PreToolUse fire raised), beside `_reject_hook_blocked()` (hook exit-2 block), so a permission path added later cannot publish the raw title by omission. `test_dashboard_approval.py::TestDenyRowTitleRedaction` pins each path behaviorally plus a structural zero-raw-interpolation guard
- **Cross-chunk streaming redaction** (`StreamRedactor`): per-chunk redaction misses a credential split across a token/streaming/Slack chunk boundary (a chunk ending `...AKIA` and the next starting `IOSFODNN7...` each individually escape `redact_credentials()`, so raw fragments reach WebSocket/SSE/Slack consumers). `StreamRedactor` is a rolling-buffer redactor: it withholds the trailing run of credential-class characters (`_CRED_CLASS` — letters/digits + URL/base64/connection-string punctuation, the possible start of a not-yet-complete credential) until a non-credential-class terminator arrives or the stream ends, then rejoins and redacts before emitting on the wire. Holdback is bounded by `_STREAM_HOLDBACK_MAX = 512` (larger than the longest fixed-format credential) so a split token is always rejoined; `flush()` redacts the buffered remainder at segment/stream end. Adds at most one chunk of latency. **Streaming JWT/JWE ceiling** (round-2 + round-3): JWTs (esp. RS256/ES256 with embedded claims) routinely exceed 512 chars, so a terminal token longer than the DoS floor would otherwise be bisected — the first `len-512` chars emitted raw before `flush()` redacts only the held tail. When the withheld tail matches `_PARTIAL_JWT_TAIL_RE` (`eyJ…` optionally followed by up to FOUR `.`-separated base64url segments — `{0,4}`, so a 5-segment compact JWE escalates too, matching the batch JWE ceiling — anchored to buffer end) the cap is raised to `_STREAM_HOLDBACK_JWT_MAX = 4096` so the whole token is rejoined before emission; the 512-char floor still applies to every non-credential run. **Split-Bearer holdback** (a8e5fe6a): an `Authorization: Bearer <token>` header spans whitespace (not in `_CRED_CLASS`), so the cred-class run alone would commit the `Authorization: Bearer ` prefix and leak the token on the next chunk. `_BEARER_ANCHOR_PARTIAL_RE` (case-insensitive, JSON-aware, `\Z`-anchored, matching any prefix of an in-progress `Authorization: Bearer <token>`) makes `feed` pull the commit index back to the anchor start (`i = min(i, anchor.start())`), holding header + token together, and escalates the cap so an opaque OAuth/refresh/SSO Bearer token >512 chars (no `eyJ`) is not bisected either. **Fail-closed ceiling** (round-3): when a credential-anchored tail (JWT/JWE/Bearer) exceeds the 4096 ceiling, `feed` FAILS CLOSED — it redacts+emits the confirmed-safe prefix, appends `_REDACTED_CREDENTIAL_TAG` (`[REDACTED: credential]`, shared with the batch redactor), and DROPS the oversized tail rather than bisecting it; a plain cred-class run with NO credential anchor is still committed verbatim (bisected — no data loss, DoS bound intact)
- Defense against write-then-execute attacks: even if the LLM tricks kiro-cli into running a credential-extracting script, the output is scrubbed before the LLM can use it in follow-up messages

### Production npm Vulnerability Gate (`scripts/check_npm_audit.py`)

Every publication runs one blocking production-dependency control in
`.github/workflows/dependency-vulnerability.yml`. It deliberately does NOT run per pull request: the
audit reaches the npm registry, whose slow hours made it the one red X on otherwise-green PRs
(re-run by hand until it passed) — and a gate people learn to re-run until green is not a gate. It
runs where a vulnerable dependency would actually ship, so nothing vulnerable is published, and a PR
that adds or bumps a dependency is checked by the release or nightly that would carry it.

The two callers hang it off different layers on purpose:

- **`release.yml`** — the release wheel and desktop builds depend directly on the gate, so all
  publish, sign, and GitHub Release jobs are transitively unreachable when it fails.
- **`nightly.yml`** — every job that ships bytes to a nightly-channel user (`publish-cli`, the six
  `publish-linux-*` callers, `publish-windows-x64`, `publish-docker`, `sign-and-notarize`) depends on
  the gate; no build job does. main has no dependency gate of its own, so without this a
  high/critical production vulnerability landing on main shipped to nightly users unaudited until
  the next tagged release. Gating the builds instead is what once failed the nightly for hours at a
  stretch — hanging it off publication means a slow registry delays publishing an already-built
  nightly, and a re-run publishes the same artifacts once the audit answers.
  `test_dependency_vulnerability_gate.py` pins both halves: every publish job gated, no build job
  gated.

The gate audits all lockfile-backed Node applications independently:

- `website/package-lock.json`
- `website/electron/package-lock.json`
- `site/package-lock.json`

CI pins Node `24.19.0`, then invokes the exact npm package `npm@10.8.2` through `npx` with
`audit --omit=dev --package-lock-only --ignore-scripts --audit-level=high --json`. It neither
installs project packages nor runs project lifecycle scripts. High and critical production
findings block; information, low, moderate, and development-only findings do not.

**Transient-failure contract.** The audit is an idempotent read, so a stall or connection fault is
retried rather than failed on the first try. The pinned npm is resolved once up front
(`npx --yes npm@10.8.2 --version`, verified to print exactly the pinned version) so the download a
cold runner pays is never charged against an audit's own timeout. Each attempt is bounded by
`AUDIT_TIMEOUT_SECONDS` (180s); an attempt that times out, raises a subprocess error, or exits with
a status other than npm's documented audit results 0/1 **and** carries one of npm's connection-level
markers on stderr (`ETIMEDOUT`, `ECONNRESET`, `EAI_AGAIN`, `E503`, ... — `TRANSIENT_STDERR_MARKERS`)
is retried up to `AUDIT_ATTEMPTS` (3) times with a short backoff. Every attempt of every audit in a
run draws on one shared wall-clock budget (`AUDIT_TOTAL_BUDGET_SECONDS`, 720s, under the job's 15-minute ceiling): no attempt gets
more than the time left, and no retry starts unless the budget still holds its backoff plus a full
attempt's ceiling, so retries cannot outgrow the job's own `timeout-minutes`. Exit 0/1 are never treated as transient
whatever stderr says (1 is the audit answering "vulnerable"), and every other failure below is
definitive and never retried. Exhausting the attempts or the budget fails closed, naming the attempt
count so a persistent registry outage reads as one rather than as a flaky gate.

**Fail-closed contract.** A missing `npx`, missing manifest or lockfile, a warm-up that does not
yield the pinned npm, a transient failure that outlives the retries or the budget, a non-transient
subprocess error, an exit status other than npm's documented audit-result statuses 0/1, empty or
malformed JSON, npm
`error` response, unsupported audit report version, inconsistent counts/status, broken advisory
reference, or high/critical record without a stable advisory identity fails the job. Exit 1 is
accepted only with a structurally valid report that contains high/critical findings. String `via`
references are recursively resolved to leaf advisories, cycles and missing references are errors,
and findings are deduplicated by lockfile, affected package, and advisory. npm registry/advisory
availability is consequently an explicit release dependency: an outage blocks rather than skips
the control.

**Exception contract.** `.vulnerability-exceptions.json` is validated before any audit against the
contract represented by `.vulnerability-exceptions.schema.json` and the stricter date checks in
the gate. The root has exactly `version: 1` and `exceptions`; each exception has exactly:

| Field | Contract |
|-------|----------|
| `package` | Exact npm package name; wildcards are forbidden. |
| `advisory` | Exact canonical `GHSA-xxxx-xxxx-xxxx` or fallback `npm:<numeric source>` identity. |
| `paths` | One or more exact audited lockfile paths from the list above; no duplicates. |
| `reason` | Trimmed 20–500 character risk justification and mitigation. |
| `owner` | Accountable GitHub `@user` or `@org/team`. |
| `expires` | Real ISO `YYYY-MM-DD` date, no more than 30 days ahead at validation time. |

An exception matches only the package + advisory + lockfile tuple; it cannot suppress another
package, advisory, or project. Duplicate scopes, unknown fields, unsupported paths, malformed
identifiers, or an expiry more than 30 days ahead invalidate the complete file. An expiry date is
valid through that UTC date; beginning the next UTC day, the stale entry fails the entire gate even
if its advisory is no longer reported. Renewal requires a reviewed edit that moves the date back
within the 30-day window and confirms the owner, reason, and mitigation remain current. Remove an
entry as soon as the dependency is fixed; Git history is the approval record.

Run the same control from the repository root with:

```bash
python scripts/check_npm_audit.py
```

The command contacts npm's registry/advisory service. Unit tests mock the subprocess boundary and
cover malformed output, operational failures, report resolution, schema constraints, expiry, and
exact-match exception behavior without network access.

### GitHub AI Review Human Overrides (`.github/workflows/`)

Human judgment is the final authority over the Fable 5 and GPT 5.6
AI-review results. A repository member with `write`, `maintain`, or `admin`
permission can record a false-positive, not-applicable, or accepted-risk
decision with:

```text
/ai-review override <fable|gpt|all> <current-sha>: <reason>
```

The decision is intentionally explicit and commit-scoped. The handler resolves
the current PR head and accepts a 7–40-character SHA prefix only when it matches
that head; the trusted record stores the full SHA. Any subsequent push therefore
invalidates the decision and causes normal AI review on the new commit.

**Trust boundary** — `.github/workflows/ai-review-human-override.yml` runs on
`issue_comment`, so GitHub loads it from the default branch. It never checks out
or executes PR-controlled code. Before changing a result it requires:

1. The exact command shape above and a non-empty, at-most-500-character reason.
2. A current-head SHA match.
3. The commenter to have `write`, `maintain`, or `admin` collaborator
   permission. PR authors receive no exemption.

After validation it posts a `github-actions[bot]` comment whose hidden marker
binds `{target, full head SHA, actor, source comment id}`. Reviewer workflows
trust only this bot-authored marker; a raw author or third-party comment cannot
turn a gate green. The handler has only review-control permissions
(`actions:write`, `checks:write`, `pull-requests:write`, and
`contents:read`), and receives no `id-token` or `contents:write`.
`pull-requests:write` is required for the handler to create the trusted record
on a pull request; `issues:write` alone does not make that write reliable for a
GitHub Actions installation token.

For Fable 5 and GPT 5.6, the handler re-runs the existing PR workflow. The
re-run resolves the trusted marker before acquiring AWS credentials, skips the
model invocation, updates the existing summary with a human-override banner,
and exits its original gate successfully. Either event ordering — an override
recorded before a reviewer starts, or one arriving during model execution —
leaves the SHA-scoped human decision authoritative.

The marker-keyed comments expose the override command to repository
writers. GPT 5.6 also normalizes each current-commit result into a
top verdict plus one sentence: `✅ no blocking findings`,
`🔴 changes requested (blocking)`, an incomplete state, or a human-override
state, so a green verdict from the previous commit is never left looking
current.

When no current-SHA override is active, GPT 5.6 injects a bounded
ADJUDICATION LEDGER into the review prompt: the bot-authored override
records, plus the marker and finding-title lines of review-disposition
comments whose authors' current collaborator permission is `write`,
`maintain`, or `admin` (verified per login against the collaborators
permission API — the same check the override handler applies to its actor).
Prior review bodies are never injected. The ledger is nonce-delimited,
capped at 6,000 bytes, and explicitly untrusted data: it can downgrade the
repetition of an adjudicated finding class to advisory, and it can never
waive a new defect or authorize a green verdict.

GPT makes exactly two model calls. Pass 1 discovers candidates across the
full diff; pass 2 attempts to falsify each candidate and emits the only
verdict exposed to the comment and gate. Pass 2 also drops or downgrades a
candidate whose proposed fix violates the FIX BAR, a BLOCKING candidate that
cannot be anchored to an AUTOSDE rule or residual defect class, and a
relocated variant of a ledger-adjudicated class; an adjudication goes stale
for lines the current head materially changed. A prior disposition never
hides a currently provable new defect. Any failed call makes the review
incomplete and leaves no current-SHA reviewed marker, so the gate fails
closed.

### Pull Request Readiness (`.github/workflows/` + `prepare-pr`)

`.github/workflows/pr-readiness.yml` publishes one current-revision answer for
the repository's fan-out of CI and AI reviews. The commit status context is
`PR Readiness`; the PR carries exactly one matching managed label:
`readiness: checking`, `readiness: action required`, or `readiness: passed`.
The workflow creates missing labels idempotently, replaces the prior readiness
label, and removes readiness labels when the PR closes. A passed label means
the automated lanes passed for that SHA; it does not represent human approval.
Making `PR Readiness` a required status remains an explicit branch-protection
or ruleset setting outside the workflow.

The aggregate covers the latest PR run for CI, Build,
Code Review, Opus 4.8 Review, GPT 5.6 Review (the reconciled result of its three
calls), and Design Review, plus the managed dynamic CodeQL workflow conclusion.
Grading the CodeQL
workflow conclusion, rather than its neutral summary check, preserves failures
from any managed Analyze job. Fork PRs cannot receive repository secrets or
OIDC credentials, and this repository's managed default-setup CodeQL workflow
is not scheduled for fork heads. The secret-backed AI reviews therefore run for
forks from the trusted base branch via the `fork-*` pipeline and are graded from
the head SHA's check-runs, leaving CodeQL as the only lane explicitly ineligible
for a fork. Missing or running eligible lanes
produce `checking`; blocking workflow/check failures produce
`action required`; drafts remain `checking`.
Design Review completion is required, but its verdict and
infrastructure conclusion are advisory. It emits one `PASS | CONCERNS | BLOCK`
verdict and no separate blast-radius rating, and it owns the long-term
reversibility (one-way-door) lens. Mergeability, behind-base state,
and human review decisions are not part of this event-driven aggregate because
they can change without an aggregate refresh event; branch protection and the
live `prepare-pr` status check own them.

Every event resolves the PR's current head through the GitHub API. An event
carrying an older expected SHA is ignored, so a late
run cannot relabel the new revision. A code-free `pull_request_target` handler
updates same-repository and fork PRs from the trusted base workflow. Actions
that start or restart validation for the same SHA, including a PR description
edit that re-runs Code Review, force the aggregate to `checking` before run
lookup so an older successful same-SHA run cannot keep readiness green. Trusted
base-repository `workflow_run` events refresh it as eligible lanes finish,
including the `fork-*` reviewer completions that carry a fork's verdicts.
Readiness-label events cannot recursively rerun or cancel a review: ignored label
events use a per-run concurrency key, so they cannot cancel an
active review or replace a pending authoritative reviewer event.

The bundled `prepare-pr` skill front-loads the same review contract before the
first push. Description/diff reconciliation and every allowed commit mutation
happen before review. After local gates, it dispatches two independent,
read-only subagents over the finished base-to-head diff: one owns correctness,
security, and platform compatibility; the other owns contracts, tests, error
paths, and the user workflow. Both use the canonical severity and output rules
from `.github/workflows/codex-review.yml`. Legitimate Critical/High findings are
fixed before publication; Medium/Low findings remain advisory unless a human
escalates them. If a blocker fix changes code, one focused verifier
checks that fix. The skill records the verifier-cleared SHA and fails closed if
HEAD changes before push; it does not start an unbounded local review loop.
During a post-submit round, it records one concise, marker-keyed GPT disposition
comment before re-pushing whenever findings were fixed or rebutted. That record
names the prior reviewed SHA, finding identity, outcome, and evidence so the
next reconciliation call can distinguish a real delta from a repeated argument;
the record remains untrusted evidence and does not carry an override forward.

`prepare-pr/scripts/pr_status.py` treats the aggregate status as authoritative
when present, including over stale failed or pending duplicate checks in
GitHub's rollup. Older PRs without the aggregate retain the fail-closed legacy
rollup behavior. Only the commit-status `context` named `PR Readiness` is
trusted as the aggregate; a same-named CheckRun cannot mask another failure.
Unresolved review threads are reported for visibility but are advisory rather
than an automatic readiness failure.

### Denied Commands (`security.py` + `hooks.py`)

First-class `DeniedCommandRule` records in `BUILTIN_DENIED_RULES` (`security.py`) — each a stable `id`, a Python regex `pattern`, a `category`, and a human `description` — blocking destructive and credential-exfiltrating operations. They are enforced **only** at Kiro Crew's own `hooks.py` PreToolUse gate (`HookManager.on_tool_call` → `PolicyAuthority.is_denied`), never by kiro-cli. They are no longer a raw `deniedCommands` array injected into a kiro agent JSON, so there is no `execute_bash`/`shell` tool-settings copy and no project-dir `agents/defaults.json` override for them. Built-ins are **default-ON but user-DISABLEABLE** from Settings → Security (see "Denied-command rules, opt-out state, and read-only auto-approve" below). Patterns for deployment-specific credential-vending CLIs are NOT in this catalog — a composed edition contributes those itself, either as an un-weakenable `SecurityOverlay` pattern or as a user-disableable rule through the `denied_rules` seam.

**Credential exfiltration blocks**:
- `.*echo.*\$AWS_SECRET.*`, `.*echo.*\$AWS_ACCESS.*`, `.*echo.*\$AWS_SESSION.*` — env var echo
- `credential-exfil-printenv-aws` — `printenv` (as a command word) naming a secret-bearing variable (`AWS_SECRET*` / `AWS_SESSION*` / `AWS_SECURITY*` / `AWS_ACCESS*`); `printenv AWS_REGION` is not a match. `credential-exfil-env-grep-aws` — an environment dump (`env` / `printenv` / `set` / `export -p` / `typeset` / `/proc/<pid>/environ`, as a command word) **piped** through `grep`/`awk`/`sed` selecting a name that can print a credential. The always-on keystone (`_ENV_CRED_SHARED_PATTERNS`) reuses the identical regex (`_ENV_DUMP_GREP_AWS_PATTERN`) **on the same `_deny_matcher`**, so narrowing one tier never leaves the block standing on the other, and the tier with no length cap cannot be the slow one. Two boundaries define the narrowing, and both were chosen because an attacker cannot rewrite around them:
  - **The selector.** The bare `AWS`/`AWS_` prefix (which selects every AWS variable, secrets included), a secret-bearing word, or a **truncation** of one that ends the operand — `grep` matches by substring, so `env | grep AWS_S` prints `AWS_SECRET_ACCESS_KEY`'s value exactly as `env | grep AWS_SECRET` does, and the truncations are derived from `_AWS_SECRET_WORDS` rather than listed. Requiring the operand to END at the truncation is what keeps `AWS_SDK_LOAD_CONFIG`, `AWS_SHARED_CREDENTIALS_FILE` and `AWS_STS_REGIONAL_ENDPOINTS` out. A named non-secret variable (`env | grep AWS_REGION`) and a digit-terminated prefix (`env | grep AWS1`, which no secret-bearing name contains) are not matches. `printenv` diverges here on purpose: it resolves EXACT names, so `printenv AWS_S` prints nothing and only whole words are denied.
  - **The command word.** The dump verb must both begin and end a word, so `unset`, `offset`, `pyenv`, `dotenv`, `src/environment` and `settings.py` are not dump verbs — but a `.` or `/` before it is deliberately allowed, because `/usr/bin/env`, `/bin/printenv` and `/proc/self/environ` are the same dumps under a path and are the most ordinary spelling of the command. `environ` and `typeset` are named in the verb list for that reason: a substring matcher caught them only by accident (`environ` contains `env`, `typeset` contains `set`), and `/proc/<pid>/environ` is the process environment while `typeset` with no operand prints every variable with its value, so bounding the verb without naming them would drop two real dumps. A quoted or substituted command word (`'env' | …`, `$(which env) | …`) is likewise still the dump, and the filter word is bounded the same way on its right rather than by requiring whitespace, so `env | 'grep' AWS_SECRET` is still a filter while `grepfoo` is not.

  The narrowing stops there. The gaps between the dump, the pipe, the filter and the selector are plain `.*` — **ordered existence within one line, with no statement or pipeline-stage scoping** — because a statement-scoped span has to treat `;` and `&` as separators and a regex cannot tell a separator from the identical character inside a quoted argument. `env | sed 's/;/x/' | grep AWS_SECRET_ACCESS_KEY`, `env | grep -E 'a&b|AWS_SECRET'` and `env FOO='a;b' | grep AWS_SECRET` are ordinary credential dumps whose only unusual feature is a quoted separator, and a span that stops there fails **open** on all three. A `|` between the dump and the filter is still required, which is what keeps `env` as a wrapper (`env FOO=1 cmd`), `set -e; grep AWS_ file.txt` and `cat .env; grep AWS_ config.py` out.

  The residual **over-block** is what refusing to guess costs, and it is pinned by test (`RESIDUAL_OVER_BLOCK`): a later statement's filter is attributed to the dump (`env | head -5; grep -r AWS_ src/`, `env | wc -l && grep AWS_SECRET f`), a later pipeline stage's text is read as the filter's operand (`env | grep PATH | echo AWS_SECRET`), and `env` as another tool's subcommand counts as a dump (`conda env list | grep aws`). Anchoring the verb to a command position would reclaim the last one and would also drop `sudo -E /usr/bin/env | grep AWS_SECRET`, since any wrapper prefix defeats that anchor. Deliberately **out of scope**: a dump REDIRECTED to a file and read back with no pipe (`env > f; grep AWS_SECRET f`). Correlating the sink with the reader needs a backreference the RE2-style engine these built-ins are authored for does not have, and blocking only the `grep` spelling would be no control at all — `awk`, `sed` and a plain `cat` of the same file read it just as well and are equally unmatched. `redact_credentials` (AKIA/ASIA plus high-entropy detection) is what stands between that shape and a chat surface.
- `.*python.*boto3.*get_credentials.*`, `.*python.*botocore.*credentials.*` — script-based extraction
- `.*curl.*169\.254\.169\.254.*`, `.*wget.*169\.254\.169\.254.*` — IMDS metadata endpoint (coarse literal-string match)
  - **Encoding-aware IMDS gate** (`_check_imds_access` + `canonicalize_ip`): beyond the literal-string denies above, every IP-like token in a bash command is canonicalized to dotted-quad and compared to `169.254.169.254`, so alternate encodings the OS resolver/`curl` accept are blocked too — single-integer (`2852039166`), hex (`0xa9fea9fe`), octal per-octet, IPv6-mapped (`::ffff:169.254.169.254`), and the inet_aton **2-part (`169.16689662`) and 3-part (`169.254.43518`) short forms** (decimal or hex trailing component). The 2-/3-part forms are resolved via `socket.inet_aton` (the same resolver `curl` uses), which also rejects out-of-range forms (`169.254.11207422`) so benign hosts are not over-blocked.
- `.*curl.*\$AWS_SECRET.*`, `.*curl.*\$AWS_ACCESS.*` — credential exfil via curl
- `aws s3 cp .* s3://.*`, `aws s3 mv .* s3://.*`, `aws s3 sync .* s3://.*` — file upload exfiltration
- `.*cat.*/\.aws/.*`, `.*cat.*/\.ssh/.*`, etc. — direct credential file reads

**Allowed operations** (not denied by this catalog):
- `ada credentials update` — NOT denied by this catalog. A composed edition may deny the credential-*vending* form through its own `SecurityOverlay`; where it does, the supported pattern is to run the vending command in your own terminal once and let `credential_process` in `~/.aws/config` refresh automatically for AWS CLI calls
- `ada profile add/list/print/delete` — NOT denied by this catalog either
- `aws sts assume-role` — cross-account access
- AWS CLI commands (`describe-*`, `list-*`, `get-*`, `filter-*`, `s3 cp`, `s3 ls`, etc.) — work via `credential_process`

**Destructive operation blocks**: `rm -rf`, `git push --force`, `aws * delete-*`, `aws ec2 terminate-instances`, `cdk destroy`, `terraform destroy`, etc.

**Self-protection global options**: the `restart`, `update`, `cloud` lifecycle, and `gateway restart` regex rules accept unquoted top-level CLI options between `kirocrew` and the subcommand, reusing the same linearized flag-run primitive as the AWS rules. Their additive argv-structural floor handles shell quoting for both flags and subcommands. This covers the repeatable verbosity spellings (`-v`, `-vv`, `--verbose`) and `--no-jail`; adding or quoting a valid global option or subcommand must not turn a denied self-management command into an allowed one.

**Quote-normalized segment view (`_deny_segment_views` + `_shell_tokens`, `security.py`)** — both deny tiers match TEXT, while a shell strips quoting, de-escapes backslashes, resolves ANSI-C / locale quoting, collapses empty-string splices and collapses whitespace runs before the program ever sees its argv. A rule authored as a command SHAPE was therefore defeated by re-spelling any single token: `rm -rf "/"`, `"rm" -rf /`, `rm "-rf" /`, `r''m -rf /`, `rm  -rf  /`, `rm -rf \/` and `rm -rf $'/'` all run exactly what `rm -rf /` runs, and none of them *contains* the rule's own text. Only the six self-protection rules and git-publish had an argv-structural floor closing this (above); the other ~130 built-ins — including every destructive-operation and credential-exfiltration rule listed in this section — were spelling-dependent. Pass 2 now evaluates each segment in **two views**: the raw text first (byte-identical to what it matched before), then a quote/escape-normalized re-join, appended **only when it differs**, so an unquoted command pays no second pass over the catalog. Four properties are load-bearing:

- **ANSI-C quoting is resolved as part of tokenization, on the RAW text; locale quoting is NOT the same thing.** `$'…'` is a quoting form whose value bash computes before the program sees it, so `rm -rf $'/'` and the hex-spelled flag `rm $'\x2d\x72\x66' /` both reach the `rm -rf /` rule, and a `$'…'`-wrapped nested payload is walked like any other. `_decode_shell_quoted_literals` replaces each `$'…'` span with `shlex.quote(_decode_ansi_c_body(body))`, so the decoded value stays ONE token even when it contains whitespace. Doing this BEFORE `shlex` is what makes it safe: `shlex` removes the quotes but leaves the `$` glued to the content, and at that point `$'/'` -> `$/` is indistinguishable from a variable reference such as `$HOME`, so a post-`shlex` `$`-strip would eat real variables and break the path normalizer's own `$HOME` expansion. Requiring the quote character means a bare `$HOME`, `${HOME}`, `$FOO` and `$(date)` are all untouched — pinned by `test_decoding_dollar_quotes_does_not_eat_variable_references`.
  - **`$"…"` is locale TRANSLATION and follows DOUBLE-QUOTE rules, which is measured, not assumed.** Treating it as ANSI-C was itself a bypass: bash gives `$"\r\mAA"` the word `\r\mAA`, byte-identical to plain `"\r\mAA"`, because inside double quotes a backslash escapes only `$`, a backtick, `"`, `\` and a newline — so `\r` is a literal backslash-r, not a carriage return. Decoding it as ANSI-C normalized that `\r` to whitespace and the command disappeared from the view, while the inner shell of `bash -c $"\r\m -rf /"` resolves the backslashes in its OWN lexing pass and runs the destructive command (measured: `bash -c $"\r\mAA"` executes `rmAA`). The `$` is therefore dropped and the double-quoted text left for `shlex`, which also keeps `rm -rf $"/"` reaching the rule since bash's operand there is `/`.
  - **Unicode escapes are decoded, at bash's EXACT widths, before the case fold — and that gap was not confined to this view.** `_decode_printf_escapes` handled octal and `\xHH` but not `\uHHHH` / `\UHHHHHHHH`, and the argv-structural self-protection floors already depended on it — so `kirocrew $'\u0074\u006f\u006b\u0065\u006e'` walked past the credential-mint rule while its `\x`-spelled twin was refused. Fixing the shared decoder closes both surfaces at once (`test_the_decoder_gap_also_bypassed_the_credential_mint_floor`). The widths are **exact and case-sensitive**: bash consumes at most 4 hex digits after `\u` and at most 8 after `\U`, so `$'\u0072f'` is `r` followed by a literal `f`, **not** a 5-digit code point. Reading more digits than the spelling allows is itself a bypass — the wrong character replaces the two the shell passes, and `rm -$'\u0072f' /` escaped the rule exactly that way. Because `is_denied` lowercases its input, and lowercasing destroys the `\u`/`\U` distinction, pass 2 splits its segments from the **original-case** text and `_deny_segment_views` decodes before folding case; the split is unaffected because no case mapping produces a separator (`test_case_is_preserved_until_after_the_escapes_are_decoded` pins both the commutation and that matching stays case-insensitive). A NUL **or a lone surrogate** is left encoded — the surrogate because it is not a character bash can pass either and a decoded one would reach the SEL record, whose JSON encoder raises on it, turning a denial into a crash.
  - **Residual on the floor path.** The self-protection floors take an already-lowercased string by contract (`_is_credential_mint(text_lower)` and siblings), so on that path a `\U`-spelled escape still arrives as `\u` and its 8-digit form is not decoded. That is strictly narrower than before — every `\u`/`\U` spelling was missed there previously, and the 4-digit `\u` form is now caught — but closing it fully means threading original case through the floor's whole signature surface, which is a separate change.
- **Additive, never a replacement.** The raw view is matched first and independently, for the same reason the self-protection floor keeps its regex: a payload the tokenizer cannot see into (`bash -c "…"`, `eval "$CMD"`) is still caught on raw text. A normalization failure can lose the EXTRA match but never the raw one, so it cannot turn a denied command into an allowed one. `_shell_tokens` additionally degrades to whitespace splitting with quote stripping when `shlex` rejects the input, so an unterminated quote (`rm -rf "/`) still normalizes rather than yielding no view at all.
- **Per SEGMENT, never per command.** Re-joining tokens with single spaces erases the separators that END a command, so a whole-input re-join would FABRICATE a command that was never run — `echo rm` + newline + `-rf /` is two commands and reads as `echo rm -rf /`. Views are built from `_split_segments` output so no boundary is ever crossed, **including inside a nested payload**, which is split the same way before being viewed; the heredoc frames pinned by `TestStdinProgramTextScoping::test_benign_neighbour_no_longer_reads_as_a_mint` are the concrete case this protects, and `TestDenyMatchingIsQuoteNormalized::test_the_view_never_crosses_a_separator` pins the property directly.
- **Nested shell payloads are viewed in their own right.** A shell's `-c` argument is a *command*, and `shlex` strips only the OUTER quoting level — so `bash -c 'dd "if=/dev/zero" of=/dev/sda'` re-joins with its inner quotes intact and the `dd if=` rule still misses it, while the unquoted inner spelling (`bash -c 'rm -rf /'`) was already caught on raw text. Each literal payload is therefore walked and viewed, reusing `_nested_shell_payloads` — the same extractor the self-protection floor uses, so the `-c` / `eval` / `env -S` / herestring / `$SHELL -c` spellings and the `bash -c -- <script>` form are recognized by construction rather than re-enumerated. The walk takes **no numeric depth cap**, for the reason `_self_token_frames` records (whatever the number, one more wrapper defeats it); it terminates structurally, because a payload is carried inside ONE token of its parent and is therefore strictly shorter than the parent's source text. Only LITERAL payloads exist to walk — `eval "$CMD"` carries no visible script and stays the raw tier's job. Found by the GPT 5.6 review lane on the PR that added this view.
  - **The GLUED `-c` spelling is a separate, independent scan (#8197).** `-c` takes a value, so a getopt-convention shell (`ksh`, `zsh`) ends option parsing at the `c` and runs everything glued after it — and the reading is deliberately over-approximated for shells whose parsers keep consuming cluster letters (bash, dash), because extraction must cover the strictest interpreter the command could reach. `sh -c'rg . <fenced-root>'` reaches the walk as ONE token (`-crg . <fenced-root>`) once `shlex` strips the quotes. `_SHELL_COMMAND_FLAG_RE` anchors the whole token as a bare flag cluster, so a token carrying the payload's own characters was rejected and the payload never yielded — unexamined by every consumer of the extractor, the self-protection floor included. The companion pattern `_SHELL_COMMAND_GLUED_RE` captures the glued remainder (non-greedy, splitting at the FIRST lowercase `c`, flag letters of either case before it) instead of weakening the flag pattern where it is used for pure flag detection — and the flag pattern itself deliberately stays lowercase-only, because widening it made an uppercase-clustered decoy (`-Cc`) the first flag stop and ate the stop through which a following `--command`'s payload was found (both directions found by review lanes on this change). The command flag, the herestring, and the glued spelling each get their OWN precomputed stop table (a shared table let whichever spelling came first EAT the stop through which a later spelling's payload was found), and an **every-carrier sweep** closes the within-class half for short-cluster `-c` carriers: one forward pass from the first shell token appends the payload of EVERY such carrier under the LOOSE recognition the deleted local extractor used (`_shell_c_carrier_glued`: any prefix before the first lowercase `c`, so `-1c…`, `-Cc` and a decoy like `-onoclobber` cannot eat a later carrier's stop), deduplicated against what the tables already yielded so exact-payload-list consumers are unchanged, keeping the function O(N) — each token's glued payload is extracted ONCE up front, because many shell tokens sharing one stop index would otherwise each copy the same substring (O(N·M), found by the GPT 5.6 lane). **The glued split is fold-ambiguity safe** (`_shell_c_carrier_payloads`): the deny tiers lowercase input before the walk, so `-Cc'<script>'` (a real zsh/ksh spelling) folds to `-cc<script>` and a first-`c`-only split misread the payload as `c<script>`, hiding a protected push behind one junk letter (GPT 5.6 CI lane; the folded spelling can also be written directly). Which `c` took the argument is unrecoverable after the fold, so every plausible split is yielded: the first `c`, plus each consecutive-`c` run's last and second-to-last split (covering payloads whose program starts with one `c`, like `cat`/`curl`). Split positions are bounded to the last `_CARRIER_SPLIT_WINDOW` (64) characters of the leading letter region — linear without a padding bypass, because the true split's distance to the region's end is the payload's first-word length (a real program name), while padding only adds fake splits farther out whose program words are flag-letter runs matching no rule; without the bound a ~3 KB alternating-`c` token made the candidate set quadratic and the synchronous deny scan outlived the loop watchdog (GPT 5.6 CI lane). Stated residuals: `--command` carriers stay first-stop-only (the sweep's loose recognition is scoped to short clusters; no supported shell has a `--command` option, so this is a lost over-approximation, not a bypass — and in the DENY tiers, which lowercase input first, `-Cc` folds to `-cc` and eats the `--command` stop exactly as it always has, a pre-existing residual the case-preserving protection cannot reach); herestrings keep a first-occurrence residual (`bash <<<'a' <<<'b'` yields `a`; a real shell applies the last redirect); and the sweep's carrier recognition is segment-wide rather than command-wide, so a `-c`-carrying token of a LATER pipeline stage (`sh -c 'cat log' | grep -c <pattern>`) yields a junk payload — accepted, since a junk payload re-tokenizes to text no rule routes, and over-approximation is this module's documented safe direction. A glued payload is SYNTHESIZED text (a substring, not a token), so per the synthesized-payload rule above the data-consumer exemption fails closed on it: `echo bash -c'rm -rf /'`, a pure print, is descended into and denied — the same accepted over-block direction as the herestring tail and the glued `env -S` argument. An all-alpha cluster (`-ecfoo`) is genuinely ambiguous post-tokenization — it satisfies the bare-flag reading (next token is the script) AND carries a remainder a getopt-convention shell would run — so BOTH readings are yielded. The alt-traversal pass's local copy of this handling (`_alt_shell_c_payloads`, added by the PR for #7309 when changing the shared extractor was out of its scope) is deleted with this: extraction is the shared extractor's alone (re-run ONCE on an assignment-substituted token list when a resolved program name is a shell the shared walk could not see literally), and the alt pass keeps only what the shared extractor cannot know — assignment-resolved program names and positional-parameter binding (`_alt_bound_shell_payloads`, which locates the command string through the same loose carrier recognition so the two cannot drift).
  - **A payload carried by a data consumer is not descended into.** `echo bash -c '<script>'` prints the script, so walking it would refuse a command that runs nothing. `_data_consumer_exempt` decides this — the same guarded exemption the self-protection floor uses, so a piped evaluator (`echo … | sh`), a substitution in program position, and an `awk`/`sed` script carrying an executing construct all withdraw the exemption. This is deliberately **not** implemented as "descend only when the launcher is in command position", the narrowing suggested alongside the advisory: the launcher is *not* in command position in `sudo bash -c …`, `timeout 5 bash -c …`, `nohup …`, `ssh host …`, `xargs …` or `env FOO=1 bash -c …`, all of which execute the payload, so a position rule trades one false positive for six bypasses (`TestDenyMatchingIsQuoteNormalized::test_executor_wrappers_are_still_walked` pins all six). Because the exemption governs only whether to DESCEND, the raw tier is untouched: the unquoted mention `echo rm -rf /` stays refused exactly as it was before this view existed.
  - **The exemption is decided per OCCURRENCE and fails closed, because a payload is not necessarily a token.** `_nested_shell_payloads` also returns SYNTHESIZED text — a `sed` `e`-flag replacement, the tail of a glued herestring (`bash<<<'<script>'`), a glued `env -S` argument, an `alias` assignment — which is a substring or a re-join rather than an element of the token list. Recovering a position with `list.index` therefore raised `ValueError` and propagated **out of the permission gate on legitimate input** (`sed 's/x/y/e' notes.txt`), which is a crash where a security decision belongs; the GPT 5.6 and Opus 4.8 lanes found it independently. The exemption is now applied only when the payload appears as a token AND *every* occurrence sits in the argv of a data consumer; a payload with no token position cannot be proven inert and is descended into. Deciding from a single recovered index would not be sound — a short synthesized payload can also be a coincidental substring of an unrelated token, and one wrong position could wrongly exempt a payload that really executes. Every window is additionally built inside a guard, so `_deny_segment_views` cannot raise at all: a failure drops that window and leaves the raw view standing (`test_view_construction_never_raises`).
- **No expansion — which is why `_shell_tokens` was factored OUT of `normalize_shell_command` rather than reused whole.** The view stops before `~`/`$HOME` for two reasons. Expansion is platform-dependent: it DELETES the literal `~` that `rm -rf ~.*` is authored to match, and on Windows yields a drive path (`c:\users\…`) that no POSIX-anchored rule matches — so `rm -rf "~"` would be caught on Linux by the sibling `rm -rf /.*` rule and missed on Windows. And a denied view becomes the security event log's `operation` field, so expanding here would write the operator's real home path into the audit trail on every such denial. Path IDENTITY (dot segments, `..`, `$HOME` versus the resolved home) stays with `_check_sensitive_via_normalizer` over the sensitive-path keystone — the layer that RESOLVES rather than matches. Both functions share one tokenizer, so token identity cannot drift between the argv view and the path view.
- **An empty-elided render is ADDED as a third view, never substituted for the plain join (issue #7500).** An empty-quoted word — `""`, `''`, `$''`, `$""`, or any concatenation of them (`""''`, `""""`) — is a real argv element that the shell does hand to the program, so `_shell_tokens` is correct to keep it and the payload walk still reads argv as written. What it cannot survive is the RENDER: a single-space join turns a zero-width element into a spurious extra separator, so `rm -rf "" /home/x` rendered as `rm -rf  /home/x` and the `rm -rf /.*` rule stopped matching its own target — the destructive command ran exactly as written and only the gate was fooled. The escape was pattern-DEPENDENT, not systematic: `chmod "" 777 /etc/passwd` stayed denied because the rule that catches it (`chmod.*/etc/.*`) tolerates the extra separator, which is why the repair belongs in the render and not in individual rules. **Both whitespace shapes are emitted**, because REPLACING the plain join loses denials that existed: a rule that REQUIRES an intervening token (`rm -rf .* ./data`) matched the double-spaced view and matches neither the elided one nor the command's canonical spelling, so `r""m -rf "" ./data` was refused before and became allowed — the additive-only property this helper documents is load-bearing, not tidiness (found by the GPT 5.6 lane, reproduced against the merge-base; pinned by `test_the_elided_view_is_added_and_never_substituted`). The fix is in the RECOGNIZER: `_shell_tokens`' documented contract — argv as a POSIX shell hands it over — is unchanged, so the path normalizer's ~19 consumers see identical tokens. The six self-protection rules were never fooled, because their argv-structural floor does not match a rendered line. Pinned by `TestEmptyArgvElementDoesNotBreakTheDenyView`.
  - **The git-publish floor's DETECTOR gap is closed for QUOTED empty words (issue #8115).** `git "" push origin main` was allowed: every git-publish rule is stripped from the regex tier and enforced solely by `_git_publish_floor_tags`, whose entry detector `_is_git_publish` reads the RAW command text in pass 1 and requires the program and the subcommand adjacent, while its normalizer second pass (`_is_git_push_via_normalizer`) broke its subcommand-seek loop on the interposed empty argv element — so the floor was never consulted at all. The normalizer pass now reads past words git cannot resolve a command name from — empty and whitespace-only tokens — while seeking the subcommand (no matching guard is needed in program position: a zero-width word never resolved to the program word, so the outer scan already steps past it). The widening is DETECTION-only and deliberately fail-closed OVER-detection: git does not ignore a zero-width word — it takes it as its command name and exits — so a spelling this newly reaches either fails to run a push at all or was already reached in its adjacent spelling; no runnable push gains an escape. What fires for the newly-reached spellings is the UNGATED anti-obfuscation branch, not the protected-branch rule: `_git_push_args` anchors on the raw split and does not skip the empty word, so the parse fails and the floor denies unconditionally (`_GIT_PUBLISH_UNGATED`) — the right treatment for a spelling git itself cannot run, and a mention-shaped spelling such as `echo git " " push origin main` inherits exactly the treatment its adjacent twin already had. `str.strip()`'s whitespace set is wider than POSIX IFS, deliberately: every extra skippable character is still a word git rejects as a command name, so the breadth only ever adds detection. The subcommand-position requirement is intact — `git "" stash push` stays a non-publish. **Residual: an UNQUOTED expansion that evaporates is a different shape and is NOT closed here.** `git $(echo '') push origin main` or `git ${UNSET} push origin main` hands the detector a non-empty token (`$(echo`, `${unset}`) that is neither a flag nor `push`, and the shell REMOVES the word entirely at run time — so git really pushes; the zero-width-word justification does not apply because no zero-width word survives to argv. Closing it needs the normalizer to model expansion — the same boundary the glue-evasion residual above records — tracked by issue #8459. Pinned by `TestEmptyArgvElementDoesNotBreakTheDenyView::test_the_git_publish_detector_skips_an_empty_word` (the flipped form of the pin #8114 left).
- **Residual.** A token split by BOTH quoting and a separator-shaped glue construct (`"rm"$(echo ' ')-rf /`) is in none of the views: the raw text is not contiguous and the glue lands on its own segment. The whole-string raw pass covers the glue-ONLY spelling (`git$(echo ' ')push`); closing the combination needs a normalizer that models substitution, which a re-join is not. A variable spelling of an operand (`rm -rf $HOME`) is likewise outside this view, by the design point above. And a quoted WHITESPACE-ONLY word (`rm -rf " " /home/x`, `rm -rf $'\t' /home/x`) still renders an extra separator and still escapes a command-shape rule. A render that dropped it would be additive like the empty-elided one and so could not lose a denial, but it is not the same claim: an empty element carries no characters, so a view without it is still the argv the shell hands over, while a whitespace-only element is a real operand naming a file that can exist, so a view without it is an argv **one operand short** of the one that runs — and `is_denied`'s exception machinery is matched against views (present; `_DENY_EXCEPTIONS` empty today), so the direction that widening opens is ALLOW. The naive alternative is unsound and must not be chosen either: whitespace-collapsing the joined line would merge a two-word filename (`rm -rf "a b"`) into two operands and match a rule against a command that was never run. Recognizing that shape wants rules matched against argv STRUCTURE rather than against a rendered line, which is what `_SELF_PROTECTION_FLOOR_PATTERNS` already does for the six self-protection rules (and what the git-publish floor now does for its own whitespace-only shape — `git " " push origin main` denies argv-structurally since issue #8115, without touching this rendered-line residual) — tracked by issue #8124. Pinned by `TestEmptyArgvElementDoesNotBreakTheDenyView::test_a_whitespace_only_word_is_a_documented_residual`, whose assertion flips when it is closed.
- **Line continuations are folded before the segment split, quote-aware (`_fold_line_continuations`).** A shell removes `backslash + newline` while lexing, so `"r\<nl>m" -rf /` runs `rm -rf /` — and `_split_segments` cuts on that newline, severing the continuation before any view is built, so every command-shape rule missed the spelling (pre-existing: the raw text does not contain the rule's own text either). The fold therefore runs on the input **before** the split; pass 1 still matches the completely unfolded text, so this only adds reach. Which contexts fold was **measured against bash** (`printf %q` on the resulting argv for `<spelling> BB`), not assumed:

  | spelling | bash argv | folded? |
  |---|---|---|
  | `A\<nl>A BB` | `<AA><BB>` | yes |
  | `"A\<nl>A" BB` | `<AA><BB>` | yes |
  | `'A\<nl>A' BB` | `<A\<nl>A><BB>` | no |
  | `$'A\<nl>A' BB` | `<A\<nl>A><BB>` | no |

  So the scan folds unquoted and inside double quotes and preserves single-quoted and ANSI-C spans; `$"…"` follows the double-quote rule because only `$'` opens a preserving span. It runs BEFORE the ANSI-C decode, which is the shell's own order — continuations go while lexing, the escape body is interpreted after — so a preserved `\<nl>` inside `$'…'` stays part of that literal. **The pre-existing `_shell_join_continuations` was deliberately NOT reused**: it is a bare regex that folds inside single quotes too, and its own comment scopes it to the self-protection floor's tokenizer input rather than to the matched text of the whole catalog, so reusing it would fold `echo 'r\<nl>m -rf /'` — which bash prints literally — into a denial. `test_the_blunt_floor_helper_is_why_the_fold_is_quote_aware` keeps that rejected reuse on the record.
  - **A nested payload gets the same pre-lex treatment, and the WHOLE command is walked for payloads.** The shell that runs a `-c` script folds *its* continuations before lexing it, so the payload is folded before being split; and because `_split_segments` is deliberately quote-unaware, a newline inside the quoted payload severs the command before the script can be extracted at all — `bash -c 'r\<nl>m -rf /'` arrives as the pieces `bash -c 'r\` and `m -rf /'`. The whole command is therefore also walked for payloads, with `emit_self=False` suppressing its own re-join, which is what keeps that walk from fabricating a command across its separators.
  - **The ANSI-C body decoder is a SINGLE left-to-right pass (`_decode_ansi_c_body`).** Bash resolves `\"` and `\'` inside `$'…'`, so `bash -c $'rm -rf \"/\"'` hands the inner shell the script `rm -rf "/"`; leaving the backslashes in made the nested view miss the rule. Sequential `str.replace` calls cannot do this safely: `$'\\n'` is an escaped backslash followed by the letter `n` — two characters — but resolving `\\` first and then looking for `\n` collapses it to whitespace and invents a separator bash never passed. Each escape is consumed atomically instead. Literal escapes (`\\ \' \" \?`) decode to their character, the control family (`\a \b \e \E \f \n \r \t \v`) keeps this file's long-standing normalization to a SPACE (the value here is that a token boundary appears where the shell puts one, and a literal control byte in a matched view would only travel into the audit record), numeric forms keep bash's exact widths — **octal is masked to ONE BYTE**, which is bash's semantics and was measured (`$'\555'` is `m`, `$'\777'` is 0xFF); converting the full value gave `$'r\555'` the character `ŭ` where bash passes `rm`, so that spelling ran while the view matched nothing. **A NUL TRUNCATES the body**, which is also measured and also was a bypass: bash cannot place a NUL in an argv and what it does instead is stop there, so `$'AA\0junk'`, `$'AA\400junk'`, `$'AA\x00junk'`, `$'AA\u0000j'` and `$'AA\c@junk'` all yield `AA` — leaving the escape encoded let `$'dd\0junk' if=/dev/zero of=/dev/sda` run while the view held `dd\0junk if=`. The other inert codes (out of range, lone surrogate) keep the escape rather than truncating, because bash does not produce them at all. An unrecognised escape keeps both characters as bash does. **`\cX` control escapes decode too, on a MEASURED mapping**: `ord(upper(X)) & 0x1F` with `?` special-cased to 0x7F — an XOR-0x40 derivation gets `\c0` wrong (bash yields 0x10, not `p`). `\cI` is a TAB, so `bash -c $'rm\cI-rf /'` hands the inner shell a tab-separated `rm -rf /` and it runs; every `\cX` result is a control character and so takes the same normalization to a space, except a NUL, which stays encoded. The mapping is restricted to a **single ASCII** target, because `str.upper()` is not length-preserving outside it (`"ß".upper()` is `"SS"`) and `ord` of that raised `TypeError` straight out of the permission gate — a crash where a security decision belongs; a non-ASCII target keeps both characters instead.
- **The quoting regex excludes the backslash from its negated classes, and that is a ReDoS fix.** With `[^']` a backslash could match either alternative — `\\.` (two characters) or the class (one) — the textbook ambiguous quoted-string pattern, so an unterminated `$'` followed by a run of backslashes forces the engine through ~1.618ⁿ tilings of that run. This regex runs inside the PreToolUse gate on the full, uncapped command, so it is a hang rather than a slowdown (measured: 9 ms at 24 backslashes, growing ~1.6x per character). Excluding the backslash makes the alternation unambiguous — a backslash is always consumed by `\\.` — while accepting exactly the same language. Pinned structurally *and* with a time budget by `test_the_quoting_regex_is_not_redos_prone`.
- **Audit metadata goes through `redact_and_truncate`, never a bare slice.** A credential straddling the 200-character boundary would be cut in half, and the fragment no longer matches the credential pattern, so SEL's own write-path redaction cannot catch it and the partial secret persists in a dashboard-readable log — the exact reason that helper exists ("Redaction runs over the full text BEFORE the `max_chars` slice"). `_emit_deny_event`'s `segment` and `raw_segment` take it, and so do the other two emitters that carry caller text into metadata: `_emit_push_allow_event`'s `command` and `audit_injection_dropped`'s `sample`.
  - **The ORDER only buys anything if the caller has not already folded the text.** The scrubber's AWS key-ID spelling is case-SENSITIVE on purpose — widening it would false-positive on ordinary prose (`asia` is a word) across every egress surface, and `credential_patterns.AWS_KEY_ID` also gates a request-blocking surface — so a key handed over already lowercased slips past the pre-slice pass, gets cut by the 200-char clip, and the surviving prefix is then too short for SEL's own any-case write-path net (`sel._AWS_KEY_ANYCASE_RE`, boundary-bounded at both ends) to match either. `is_denied` therefore audits the RAW `tool_name` on the allow path, not the `lower` view it matched with; the injection-dropped emitter's three `context.py` callers already pass raw text. An INTACT case-folded key is still caught, by that write-path net — the straddling window is the only one where both passes miss.
- **The whole-command payload walk is skipped when the split produced a single segment**, because the whole command then IS that segment and walking it twice doubles the payload scan for no additional view. That scan is quadratic in token count inside the pre-existing `_nested_shell_payloads`, which the self-protection floor already runs on every command (measured on a command padded with N interpreter tokens: the extractor alone is ~280 ms at N=1600, the floor's own `_self_token_frames` ~287 ms, and this pass went from ~2.07 s to ~1.02 s once the duplicate was removed). **Residual:** the remaining factor over the floor is the same quadratic, paid once more here; making `_nested_shell_payloads` linear is a change to shared floor machinery with its own review surface, not a normalization change. Pinned by `TestDenyMatchingIsQuoteNormalized::test_a_single_segment_command_is_not_walked_twice`.
- **Accepted residuals — two false positives, kept on purpose.** Both were raised as advisories and both are over-blocks rather than bypasses. (a) `$'…'` is inert inside DOUBLE quotes — bash's word for `"$'r\155 -rf /'"` is the literal text and `echo` prints it verbatim — but the decode does not track the outer quote context, so a view can hold the decoded form. (b) `$'r\155 -rf /'` is ONE word whose intra-word spaces become argv boundaries in the re-join; running it yields "No such file or directory", since no program has that name. Both are accepted on the asymmetry this module already documents for its data-consumer denylist: a false positive is "annoying, visible, and safe" whereas the inverse is a silent bypass, and `is_denied`'s own docstring states over-blocking is the safer direction for this pass. Both suggested remedies push toward *less* denial, and the second would have to mask intra-token whitespace — the mechanism that makes a re-spelled command's argv read as the command at all. Pinned by `TestDenyMatchingIsQuoteNormalized::test_two_accepted_over_blocks_are_pinned_not_implied`.
- **Residual — a rule's own text must still be contiguous, which no view can fix.** `$'rm\0junk' -rf --no-preserve-root /` normalizes to exactly the command bash runs, and is still allowed, because `rm -rf /.*` requires its text contiguous and does not tolerate an interposed flag. The PLAIN spelling `rm -rf --no-preserve-root /` is allowed for the same reason, which is what places this in the built-in rule's authoring rather than in normalization: no view can make a non-matching pattern match. Closing it means editing a shipped rule's regex, which changes matching for the whole catalog. Pinned by `TestDenyMatchingIsQuoteNormalized::test_flag_interposition_is_a_catalog_gap_not_a_view_gap`, whose first assertion flips when it is closed.
- **Residual — the synthesized-target tier is NOT covered, and not for the reason first claimed.** `is_denied_synthesized_target` matches raw text only. The PR that added this view justified that by calling the target gate-constructed rather than shell text; pinning the assumption disproved it. The `path` VALUE is model-authored, and `_normalize_search_path` resolves home variables and dot segments but **not quoting**, so `path='"$HOME"/notes'` synthesizes `file-search path="/home/alice"/notes max_depth=3` — quote characters intact — and a path-keyed operator rule can miss it exactly as the shell tiers used to. What IS true is that the tier has different semantics (a synthesized grammar with no chaining, no program position, and values whitespace-encoded by the synthesizer), so the per-segment shell view is the wrong instrument for it; the fix belongs in `_normalize_search_path`, alongside the normalization it already performs. Pinned as a documented gap by `TestDenyMatchingIsQuoteNormalized::test_the_synthesized_target_keeps_model_authored_quoting`, which is the assertion that must flip when it is closed.

**Structured-param synthesis (`_command_from_tool_params`, `types.py`)** — kiro-cli's `use_aws` tool is reported with `kind=execute` (making `is_shell=True`) but its params are the structured `{service_name, operation_name, parameters, region}` shape, not the `{command: "..."}` shape. Without synthesis, `AcpEvent.shell_command` returns None and the deny-by-default backstop fires on every `use_aws` call. The helper synthesizes `aws <service> <operation> [--region r] <serialized parameters> <positional args>` for the gate to evaluate:

- **Casing normalization**: `operation_name` is normalized PascalCase/camelCase → kebab-case via `_normalize_to_kebab()` before synthesis (e.g. `DeleteStack` → `delete-stack`). This prevents a casing mismatch from silently bypassing the kebab-case deny globs. `service_name` is NOT normalized because AWS CLI service names are already single lowercase tokens (`cloudformation`, `s3api`) and normalizing them would incorrectly hyphenate. The normalization is injective over the space of valid AWS API names so it cannot produce false collisions between a benign op and a denied one.
- **Whitespace fail-closed**: `service_name` or `operation_name` containing whitespace returns None (deny-by-default) rather than synthesizing a multi-token string that could confuse regex-based deny rules or produce shell-injection semantics in the synthesized string.
- **Best-effort caveat (serialized tail)**: the `parameters` dict is serialized via `json.dumps(sort_keys=True)` into the tail so the sensitive-path and exfiltration checks scan it (e.g. `cat ~/.aws/credentials` inside `ssm send-command` `commands`). However, JSON escaping (`\"`, `\\`) can render an embedded payload in a form the shell-text matchers were not authored for. This is acceptable for a single-user tool with operator consent (the human sees the tool call in the approval UI) but is NOT a complete smuggling defense. A future hardening pass could apply the shell-normalizer to the deserialized leaf values individually.
- **Half-formed shape fallback**: if either `service_name` or `operation_name` is missing/empty/non-string, synthesis returns None and deny-by-default remains armed.
- **Evidence note**: no captured event in the security event log contains the raw `operation_name` value (the SEL records tool names, not params). The kiro-cli binary strings contain PascalCase AWS SDK operation names (`GetId`, `CreateToken`, `DeleteStack`), and the tool spec states params "MUST conform to the AWS CLI specification" (kebab-case), but since the value is model-authored, BOTH casings can arrive. Normalization makes the assumption non-load-bearing.

**File-search argument synthesis (`_search_deny_target`, `hooks.py`)** — both deny tiers match text, and they are handed the display title plus, for a shell tool, the raw `command`. A file-search builtin (`glob`, `grep`, the `code` tool's search operations) has neither: its title is LLM-authored prose that need not name a path, and it carries no `command`. Its scope — the root it walks and whether that walk is depth-capped — lives only in its arguments, so a `glob` rooted at the home directory reads the same tree a `find ~` does while the `find` rules authored to refuse exactly that see nothing. The gate therefore synthesizes a fourth deny target, `file-search path=… max_depth=…`, from `raw_params` and evaluates it in a **tier of its own** (`is_denied_synthesized_target`), not alongside `normalized` / `tool_name` / `command`.

- **Only SCOPE is emitted — `pattern` and `include` deliberately are not.** They are model-authored free text, and emitting them verbatim broke the mechanism in both directions: a value could mint a field it is not (a pattern containing `max_depth=` silences a rule keyed on the absence of a cap — the absence of a key is the only way the tier can express "unbounded"), and a read-only search whose pattern is `DROP TABLE` matched the command-oriented `sql-drop-table` built-in, denying ordinary audit greps. `pattern` is read by the shape gate only. The consequence to know: a rule can constrain *where* a search runs, never *what* it looks for.
- **Every emitted value is percent-encoded** (`_encode_search_field`) for `=`, whitespace and `%`, so a value cannot forge a field boundary or a field name. Without this an attacker-controlled or merely unlucky `path` disarms the rule.
- **Shape-identified, not title-identified**: a non-empty `pattern`, or an `operation` in the enumerated `_RECURSIVE_SEARCH_OPERATIONS` set (walks that carry no pattern of their own), and no `command`. The arguments are what the tool actually runs with — the same ground-truth reasoning the sensitive-path keystone uses when it reads `raw_params['path']`. A `command` means a shell tool, already covered by the raw-command target.
- **An absent `max_depth` emits nothing**, which is what lets a rule express "an unbounded search", mirroring how the built-in `find` rules distinguish a capped traversal from a whole-tree walk. A `max_depth=0` IS emitted (an explicit cap, not a missing one); a boolean depth is not (`bool` is an `int` subclass and no rule could match it sensibly).
- **Both key spellings are read, one is emitted.** Our tool schemas declare snake_case but kiro-cli echoes some `rawInput` keys camelCased (the split `TOOL_PURPOSE_KEYS` handles for the purpose key), so each field is looked up under every accepted spelling — `max_depth`/`maxDepth`, `path`/`file_path`/`filePath` — and written under the canonical snake_case name, so a rule spells each field exactly once. A missed spelling would not merely drop a field, it would **invert** a rule: without `max_depth`, an absence-of-a-cap rule fires on a search that carries a cap.
- **The `file-search` prefix is a namespace, not a trust boundary.** The display title is a deny target in its own right, so a title quoting the prefix trips such a rule too. That is an over-block identical to the title tier for every other rule and it grants nothing.
- **The `path` field is normalized before encoding** (`_normalize_search_path`: home-variable substitution → `expanduser` → `normpath`, rewrite separators to `/`, then collapse a leading `//` to `/` on POSIX), so one spelling of a tree reaches a rule. `path="~"` walks the home tree exactly as its literal root does, and a rule anchored on the literal root matches only the latter — without normalization the protection reads as present while being absent. Three details are load-bearing rather than cosmetic. **Variable expansion is an allowlist** (`_SEARCH_HOME_VARS`: `HOME`, `USERPROFILE`), never `os.path.expandvars`: the value is model-authored and the synthesized target is **audited** — a denied target becomes the security event log's `operation` field — so dereferencing arbitrary variables let an agent pick a path prefix it knew a rule refuses, append `$AWS_SECRET_ACCESS_KEY`, and have the deny it deliberately triggered write the expanded secret to a readable log, making the deny matcher itself an exfiltration primitive. The home variables are safe because their value is the home path a home rule already matches on; every other variable stays literal, and an **unset** home variable also stays literal rather than substituting empty, which would turn `$HOME/x` into `/x` and claim a root-scope walk the tool never performs. The **separator rewrite** keeps the grammar OS-independent: `normpath` produces `\` on Windows, so a rule authored with `/` (the form documented here) would silently stop matching there, which fails **open**. The **`//` collapse** is POSIX-only: POSIX leaves a path beginning with exactly two slashes implementation-defined, so `//home/alice` would otherwise reach a `/home/`-anchored rule unmatched, while on Windows a leading `//` is a UNC or extended-length root that must survive intact. Normalization is **lexical only** — no `realpath`, so a symlink into a denied tree is not resolved, and the resolved sensitive-path keystone remains the layer that does not depend on spelling.
- **A relative root is NOT absolutized**, which is a deliberate divergence from `governance._norm_item`. `abspath` resolves against the *gateway process* cwd, not the cwd the tool runs in, and that misattribution cuts both ways: a rule denying the tree actually walked is bypassed, and a rule naming the gateway's own tree falsely denies an unrelated search. Governance absorbs that because it is a policy intersection where an ungoverned scope permits; a hard deny cannot. A relative root therefore stays relative, and no rule keyed on an absolute prefix matches it.
- **Normalization never raises.** `expanduser` raises `ValueError` on a `~name` form carrying an embedded NUL, and this runs inside the permission gate where an exception is a crash rather than a security decision. On any `OSError`/`ValueError` the raw value is returned for encoding, which cannot forge a field.
- **The emitted string is a public grammar.** Operators author rules against these field names, this order, and this encoding, so changing any of them silently breaks or inverts rules already installed. Treat additions as append-only.
- **Rule-anchoring caveat for authors.** Encoding stops a value from minting a field; it cannot make a regex match a path it was not written for. Two consequences: a rule that ends the `path` field with `(?:\s|$)` is evaded by any suffix — `/local/home/alice/x`, or an encoded `%20…` tail — so anchor on the prefix you mean to refuse (`path=/local/home/`) rather than the end of the field. And keep the `=` when you key on a field name: `(?!.*max_depth=)` is unforgeable, while `(?!.*max_depth)` is silenced by a directory literally named `max_depth`, since only `=` and whitespace are encoded.
- **Residual limits — this is defense in depth over the always-on sensitive-path keystone, not a complete sandbox.** The recursive-`operation` set is enumerated, so a tool that walks a tree under some other argument shape produces no target; only singular path spellings are read, so a call passing a `paths`/`files` sequence — or omitting the root entirely to walk the cwd — emits no `path` field for a path-keyed rule to see; and coverage depends on the caller threading `raw_params` at all, exactly as the arg-derived governance scopes do.
- **The synthesized target is evaluated in a tier of its own** (`is_denied_synthesized_target`, `security.py`). A synthesized target is not a command line, and running it through the whole shared rule set made the command-oriented built-ins match its argument text: `mkfs.*` denied a read-only search of a directory named `mkfs-tests`, and `.*python.*botocore.*credentials.*` denied a search inside a real virtualenv. Encoding whitespace defuses the multi-token rules (`rm -rf /`, `terraform destroy`, `DROP TABLE`), but nothing defuses a whitespace-free pattern against a path. The only per-rule remedy was disabling that rule by id, which also stopped it protecting real shell commands — so a false positive on a search cost a real control to clear.
  - **Which patterns participate: exactly the ones the caller passes.** The hooks gate passes the operator's own *enabled* `user_added` regexes and their `auto_deny_tools` globs. The shipped built-in catalogue is **not** passed and takes no part in a synthesized target: a built-in cannot express a scope rule for one, so its only possible hit here is the incidental collision above. The companion overlay is evaluated separately (below).
  - **Provenance is structural, not inferred.** An earlier revision passed the merged effective set and classified each pattern by testing its text against the shipped catalogue. Pattern text is not provenance: an operator who authors a pattern whose text coincides with a shipped one (`mkfs.*` is a natural thing to type) had their own rule read as shipped and dropped — a silent fail-open on an explicit deny, reachable with no knowledge of the catalogue and no rule disabled. Passing only what participates removes the classifier, so there is nothing left to misclassify.
  - **Ratchet: no shipped built-in is authored against the grammar.** `test_no_shipped_builtin_is_authored_against_the_grammar` asserts it behaviourally rather than by grepping for the literal (a regex can reference the namespace without containing it — `file.search`, `(?:file|dir)-search`, `\x66ile-search`): for every shipped rule that matches a synthesized target, the match must survive replacing the namespace bytes, i.e. it never depended on them. A future built-in written against this grammar fails the ratchet, which is the signal to give that rule an explicit way into the tier rather than a test to update.
  - **The ADD-only companion overlay keeps its command semantics.** `PolicyAuthority.is_denied_synthesized_target` evaluates the overlay first, through `security.is_denied` with an empty regex tier (empty, not `None` — `None` fails closed to every built-in and would evaluate the whole shipped catalogue against the synthesized target, reinstating the collision). An overlay pattern is opaque enterprise policy, and one restricting a filesystem **scope** is spelled as bare path text (`*forbidden-share*`), so any host-side narrowing of how it is matched could drop a denial its author meant. `assert_security_floor` covers this method in its runtime `@final`-override guard, so a subclass cannot always-allow file-search calls and still pass boot.
  - **`patterns=None` means the regex tier contributes nothing** — deliberately NOT `is_denied`'s fail-closed-to-every-built-in. Getting that backwards would evaluate the whole catalogue against a synthesized target, which is exactly the state this tier exists to leave.
  - **This tier does not run the argv-structural floors** (credential mint, self-kill, restart/update/cloud) **or the verb-anchored git-publish detector**, and does not do per-segment (pass 2) re-evaluation. Each interprets shell syntax a synthesized target does not have: its tokens are the namespace and `key=value` pairs, values are whitespace-encoded so one cannot split into two tokens, and no such target names a program — so a search of a tree cannot mint a credential or kill a process, and splitting only manufactures pseudo-commands out of path substrings. A real command still reaches all of them through its own `command` target.

- `is_denied(tool_name, extra_patterns, *, denied_regexes, reason_notes)` evaluates the *effective* denied-command set plus a dedicated verb-anchored git-publish detector. The **regex tier** (`denied_regexes`, matched via `re.search`, case-insensitive) is the enabled subset of `BUILTIN_DENIED_RULES` plus the user's `user_added` patterns from the keystone `denied_commands.json` opt-out state, which the hooks layer resolves via `compute_effective_denied(...)` and passes in; the **glob tier** (`extra_patterns`, fnmatch) carries legacy `auto_deny_tools` + the companion overlay. `reason_notes` is an optional `{pattern: operator note}` map (from `hooks.resolve_denied_notes`, forwarded opaquely by `PolicyAuthority.is_denied`) that decorates the refusal text only — it cannot add, remove, or alter a match. "Agent-configured patterns" no longer means a kiro agent JSON `deniedCommands` array — that injection path is retired. When `denied_regexes` is `None` the check fails closed to all built-ins enabled. The git-publish detector runs before either tier and is always-on; the protected-branch **gate** it feeds is default-on but **per-rule disableable** (see the opt-out note in the Protected-branch gate bullet below), except for the anti-obfuscation branches, which no opt-out can reach:

  - **Refusal string (a parsed micro-format, not free text):** the first line is always exactly `f"{DENY_REASON_PREFIX}{matched}"` — `DENY_REASON_PREFIX` is exported from `security.py` precisely so guards cannot drift from the producer. It is byte-stable on purpose, because three consumers parse it: `website/src/pages/chat/RecoveryCard.tsx` extracts the pattern with `/Blocked by security policy:\s*(.+?)\s*$/gm`, the test helper `_denied_by` partitions on the exact `"Blocked by security policy: "` separator, and `chat_runner` reads it for display (after redaction). When the matched pattern has an operator note, the note is appended as a **second line** — never on the first, which would be captured as part of the pattern. Because `RecoveryCard`'s regex is GLOBAL and per-line, a note containing the prefix would be parsed as a second, fabricated pattern; that is why notes carrying it are rejected at the endpoint and dropped in `resolve_denied_notes`. Both guards test `DENY_REASON_MATCH_PREFIX` (the colon-terminated form derived from the emitted prefix), NOT the emitted prefix itself: the regex makes the space after the colon optional, so `"Blocked by security policy:forged"` parses as a refusal line without containing the emitted string. Anything added to this format must keep line one intact.
  - **Git publish (verb-anchored regex):** `git push` is detected by `_is_git_publish()` (`_GIT_PUBLISH_RE` + `_GIT_PUBLISH_GLUE_RE`), **not** a substring glob. `push` must be the git *subcommand* (first non-flag token after `git`, allowing intervening `-x` / `-C path` / `-c k=v` options), so a commit message, branch name, grep pattern, or ssh remote payload that merely contains the word "push" is **not** blocked (e.g. `git commit -m '...push...'`, `git log --grep push`, `git switch -c fix/git-push`). Checked on the whole string first to catch command-substitution glue-evasion (`git$(echo ' ')push`, `git\`echo\`push`, `git_push`) and on segment-spanning chains (`git stash push && git push origin main`). Replaces the former broad `*git*push*` glob + ` stash push` exception, which over-blocked benign commands and surfaced as a silent `Tool use aborted` on the removed standalone provider.
  - **Protected-branch gate:** `_is_git_publish()` is a **pure, side-effect-free detector** — it only answers "is this a git push?". Whether the push is *allowed* (feature branch) or *denied* (protected/bare) is decided by `_git_publish_floor_tags()`, which returns the set of **rule-id tags** the command trips, at the single enforcement point in `is_denied` (via a deferred `push_allow_pending` flag), which is also where **both** SEL audits fire: `_emit_deny_event` on deny and `_schedule_push_allow_audit` (SEL `push_allowed`, operation `git_push`) on allow. `_is_push_to_protected_branch()` is retained only as a thin boolean view over the tag set for callers that need the yes/no answer. The `push_allowed` audit is deferred to the *final* allow exit, so a compound `<feature push> && <denied command>` chain that later trips a deny pass logs a **deny**, not an allow. The allow audit is handed the **raw** `tool_name`, not the lowercased matching view: nothing matched on an allow, so the fold buys the record nothing and costs it a case-sensitive branch name (`Feature-ABC` recorded as `feature-abc`) plus the pre-slice credential pass (see "Audit metadata goes through `redact_and_truncate`" above). Pinned by `test_push_branch_gate.py::TestGitPushEnforcement::test_allow_audit_records_the_raw_command_not_the_matching_view`.
    - **Opt-out, and the part of it that is NOT optional.** Each tag names a real git-publish catalog rule, and a tag fires only while its rule is in the enabled set — so an operator CAN disable protected-branch push blocking per rule (or wholesale with `disable_all`) through the keystone `denied_commands.json`. Three branches deliberately bypass that check and deny unconditionally, emitting the sentinel `_GIT_PUBLISH_UNGATED` instead of a rule id: an ambiguous refspec (`_AMBIGUOUS_REFSPEC_RE`), a brace-expansion refspec (`_AMBIGUOUS_EXPANSION_RE`), and the two "cannot parse" fallbacks (unparseable argv, or a push detected upstream with no clean segment). Those are anti-obfuscation, not policy: an operator opting out of a rule is choosing to allow a command shape they can *read*, which is not a licence to allow one nobody can. `git-publish-push-brace-expansion-refspec` is therefore the one git-publish rule that stays **locked** in the Settings panel (`_FLOOR_ENFORCED_RULE_IDS`), because its coverage is an ungated branch and a toggle for it would be a lie. A denial now reports the matched rule's own `pattern`, so it resolves to a `rule_id` in SEL rather than the opaque `git push` label.
    - `_PROTECTED_BRANCHES` covers `main`/`mainline` plus the legacy Git default-branch name (see `_PROTECTED_BRANCHES` in `security.py`), plus ambiguous runtime-resolved refs `_AMBIGUOUS_REFS` = {`head`, `@`, `fetch_head`}. A push to any of these (or a **bare** `git push` / `git push <remote>` with no explicit branch, since the current branch might be protected) is denied.
    - `_PUSH_ALL_BRANCHES_FLAGS` = {`--mirror`, `--all`} are denied **outright** (they push every local branch, so a per-branch target check cannot vouch for them), kept in lockstep with the `--(mirror|all)` regex in `config/defaults.json`.
    - `_is_push_to_protected_branch()` splits the command with `_split_segments()` and validates **every** `push` segment / refspec (closing the `push origin feat && push origin main` bypass), normalizing `refs/heads/…` paths and `local:remote` refspecs; refspecs with shell/revision syntax (`$`, `` ` ``, `@{…}` — `_AMBIGUOUS_REFSPEC_RE`) are treated as ambiguous and denied. If a push was detected upstream but **no** clean segment parses, it denies to be safe.
    - **Force push:** a force flag (`--force` / `-f` / `--force-with-lease`) does not by itself make a feature-branch push protected (force-push to a feature branch is normal PR/rebase workflow), but force-push to a *protected* branch is still blocked because the target check fires regardless of flags.
  - **Self-protection (argv-structural floor, UNION with the regex tier):** rules that stop the agent disabling its own controls — credential minting, process termination, and the `restart` / `update` / `cloud` / `gateway restart` lifecycle commands — are enforced by **both** their `pattern` in the regex tier **and** dedicated argv predicates. Neither half is sufficient alone, and the floor is deliberately **additive**, never a replacement:
    - **Why the floor exists.** These rules must distinguish a dangerous *invocation* from an incidental *mention*, and raw-text matching cannot: the gap between the product name and the dangerous verb has to tolerate ordinary shell noise (a quoted verb `kirocrew "token"`, global flags, a redirection — bash accepts one anywhere in a simple command, so `kirocrew >/tmp/out token` is a mint), but any character class wide enough for that also spans a filesystem path, and a product-named worktree path (`…/kirocrew-wt-x/test_token_auth.py`) is the false positive the rules must not produce. Quoting cuts the other way too: `pkill -f '[;]*kirocrew'` is a working by-name kill whose quoted `;` textual splitting misreads as a command separator. The floor therefore tokenizes with `normalize_shell_command()` (resolving quoting, empty-string concatenation, `$HOME`/tilde) and matches on **argv**: lifecycle rules recognize both the console script and the documented `python -m kiro_crew` entrypoint, begin after the module operand, and compare the first non-flag CLI words exactly; a Python script, another module, or an ordinary command that merely contains `kiro_crew` remains a mention rather than an invocation. For the mint, a token whose whole *program name* is `kiro[-.]?crew` followed by a later token in the same argv that is exactly `token`; for the kill, `pkill`/`killall` with the name in any argument (unbounded — the target is a pattern, not a path), or a bare `kill` whose PID comes from a command-substitution **body** naming it (walked with paren nesting, so `$(pgrep …)`, `$(pidof …)`, a pidfile read and backticks are all covered without allowlisting a resolver binary).
    - **Why the regex stays.** A shell's `-c` argument is a *command*, so `bash -c "kirocrew token"` hands the tokenizer one opaque token. The floor closes that class by re-tokenizing literal `sh`/`bash`/`zsh` `-c` payloads and `eval` arguments and checking those argvs too (`_self_token_frames`, depth-capped), but a payload that is not literal (`eval "$CMD"`) has no visible script, and the tokenizer itself can fail (unbalanced quotes). Keeping both `pattern`s in `regex_patterns` means a tokenizer failure or an unseen payload **fails closed** on raw text rather than open. `test_denied_commands_security.py::TestSelfProtectionFloorIsAdditive` pins this: the patterns must stay in the effective set, each pattern must be a true **subset** of its predicate (so the posture-UI text cannot drift from enforcement), and a simulated tokenizer failure must still deny.
    - **Platform note.** `normalize_shell_command()` expands `$HOME` via `re.sub` with a **callable** replacement, not a string. A str replacement is parsed as a template, and on Windows the home path (`C:\Users\…`) contains `\U` — an invalid escape — so a string replacement raised `re.error` for *every* input on that platform, silently emptying the token list and disabling both this floor and the git-publish normalizer second-pass.
  - **Interpreter argv literal (`credential-exfil-kirocrew-token-argv`):** a separate, narrow rule for the one shape neither half above can reach — an interpreter payload that spawns the CLI through a **library call** rather than as a shell word (`python -c "subprocess.run(['kirocrew','token'])"`, `node -e 'execFileSync("kirocrew",["token"])'`, `perl -e 'system("kirocrew","token")'`). The floor cannot help: the payload is one opaque token to the shell tokenizer and its contents are Python/JS, not shell. Scoped to the two words as **adjacent quoted arguments**, with a separator class admitting only what appears *between* argv elements (quote, comma, whitespace, opening bracket/paren) and deliberately excluding `.`, `*`, `/` and `>` — that exclusion is what keeps a regex **literal** quoting this very rule (`re.search(r'.*kirocrew.*token', cmd)`) and prose naming both words from matching, both recorded false positives. It carries a second alternative for the **single-string** spelling (`os.system("kirocrew token")`), which is **sink-qualified**: the two words inside one quoted string match only when that string is the argument of a call that EXECUTES it (`os.system`, `os.popen`, `subprocess.run`, `shell_exec`, `execSync`, `system`, `popen`, …). The sink prefix is what makes this safe — it is precisely what a regex literal, a commit message and `console.log(...)` lack, so those stay allowed while the executing form does not. A sibling rule `self-protection-kill-interpreter` does the same for a kill command (`os.system("pkill -f kirocrew")`). **Residual gap:** an interpreter that ASSEMBLES the name at runtime (string concatenation, a base64 blob, an HTTP call to the gateway) never contains it for any pattern to find. The un-disableable guarantee for this credential remains the sensitive-path floor over `token_signing.key`, which these rules do not replace. The sink set includes the asyncio spawners (`asyncio.create_subprocess_shell` / `_exec`, with the module prefix optional since the bare name is importable), which execute their argument the same way.
  - **Pass 1 (whole-string glob):** every deny glob is matched against the full input. If a pattern matches and no exception pattern also matches the full input, the command is denied immediately. This closes evasion vectors where the deny string spans a shell separator boundary.
  - **Pass 2 (per-segment glob):** only runs if pass 1 found a glob match AND the full input also matched at least one exception. The input is split on shell separators (`;`, `&&`, `||`, `|`, `&`, `$()`, backticks, newlines) into independent segments, and each segment is re-evaluated. `_DENY_EXCEPTIONS` is currently empty (the former git-stash carve-out is obsolete under the verb-anchored detector); the machinery is retained for any future scoped exception.
  - SEL audit events emitted on every denial (`deny_event`, recorded under the `git push` label for git-publish) and every exception grant (`deny_exception`).
> **Removed with the standalone provider.** A former check
> (`cc_agent.find_overbroad_cc_deny_rules`, the `seed_isolated_cc_config`
> isolation seed, and the `kirocrew doctor` surfacing of over-broad
> `permissions.deny` rules) guarded against a user's `~/.claude/settings.json`
> `Bash(*)` rule aborting commands upstream of KiroCrew's gate. It was specific
> to the `claude-agent-acp` backend and was **deleted** when KiroCrew became
> KiroACP / `kiro-cli`-only (`agent.provider` fixed to `acp`). kiro-cli's
> permission model routes every tool decision back through KiroCrew's
> `HookManager.on_tool_call` gate, so there is no equivalent upstream-deny gap.

**kiro-cli `autoAllowReadonly` removed.** The `toolsSettings.execute_bash.autoAllowReadonly: true` flag in `config/defaults.json` is gone — kiro-cli no longer self-approves read-only bash upstream of the gate (which would let those calls skip `hooks.py` entirely). KiroCrew now performs read-only auto-approve itself inside `hooks.on_tool_call`, placed **AFTER** the sensitive-path, deny-floor, and governance checks, so a deny always wins over the read-only fast-path (see "Read-only auto-approve" below).

**Agent-config injection retired.** KiroCrew no longer injects `deniedCommands` into `~/.kiro/agents/*.json`. `agent._enforce_denied_commands()`, the ~60s `CleanupHook('denied_commands', …)` re-enforce loop (`session.py`), and the `agent.enforce_denied_commands` config scope (`all`/`kirocrew`) are all removed. Enforcement is hooks-gate-only, so a kiro agent config that edits or omits `deniedCommands` cannot weaken KiroCrew's ceiling — the gate is authoritative (cross-ref `governance.md` Plane A/B).

### Denied-command rules, opt-out state, and read-only auto-approve

**`DeniedCommandRule` model** — a frozen dataclass in `security.py` with fields
`id: str` (a stable slug, e.g. `credential-exfil-s3-cp`; the opt-out key AND the
SEL audit key), `pattern: str` (a Python regex matched via `re.search`,
case-insensitive), `category: str`, and `description: str` (one human sentence
for the UI). `BUILTIN_DENIED_RULES: list[DeniedCommandRule]` is the canonical
default-ON catalog spanning the categories `aws-destructive`,
`credential-exfil`, `iac-teardown`, `local-destructive`, `pipe-to-shell`, `sql`,
`self-protection`, `git-publish`, `reverse-shell`, and `sensitive-file-read`.
`BUILTIN_DENY_PATTERNS` is retained as a derived alias
(`[r.pattern for r in BUILTIN_DENIED_RULES]`).

**Effective-set resolver** — `compute_effective_denied(rules, disabled_ids,
disable_all, user_added, governance_pins)` is a pure, order-preserving, deduped
function returning the regex-tier list: include a rule's pattern if
`(not disable_all and id not in disabled_ids) OR id in governance_pins`, then
append `user_added` verbatim. Governance pins win — a pinned rule is re-added
even if the user disabled it or set disable-all (tightest-wins). The hooks gate
computes this once per tool call via `HookManager._effective_denied(ctx)` and
passes it as `denied_regexes` into `is_denied`.

**Edition-contributed rules — the `denied_rules` seam.** A composed edition can
contribute additional `DeniedCommandRule` records through the
`DeniedRuleProvider` platform adapter (`current_context().denied_rules`).
`security.edition_denied_rules()` reads and validates them and
`hooks.resolve_effective_denied_regexes` unions them into the `rules` argument of
`compute_effective_denied`, so a contributed rule is **default-ON and resolved by
exactly the same opt-out arithmetic as a built-in**: an operator can disable it by
id or clear it with `disable_all` through the existing keystone file and the
existing `/api/security/denied-commands` endpoints, and Settings → Security lists
it (tagged `source="edition"`) alongside the built-ins.

This is deliberately the opposite half of `SecurityOverlay.extra_deny_patterns`,
which remains the un-weakenable floor: overlay patterns travel the GLOB tier via
`extra_patterns` and no opt-out can reach them. An edition picks per pattern —
floor, or default-on-but-overridable. Consequences of that split, all pinned by
`test/test_denied_rule_seam.py`:

- **Regex, not glob.** A contributed `pattern` is a Python regex on the regex
  tier. Moving a pattern over from the overlay requires rewriting it; a glob's
  `*` are quantifiers as a regex.
- **Namespaced ids.** `disabled_ids` is one flat set, so an id colliding with a
  built-in id is skipped (the built-in wins) rather than letting one rule's
  toggle move another's.
- **Not pinnable (v1).** Governance `commands`-scope pins resolve a pattern to a
  rule id against the static catalog, so a pin cannot name a contributed rule. An
  edition needing an un-opt-out-able pattern keeps using the overlay.
- **Fail-soft.** A raising or absent provider yields no contributed rules; the
  built-in catalog and the overlay floor are unaffected.
- **Full-input matching, or not published.** The matcher has two engines: a
  forward-only *fragment* matcher that splits a pattern on its top-level `.*` and
  scans the WHOLE input, and an exact whole-regex `re.search` over a
  length-capped window (`_DENY_FALLBACK_SCAN_MAX_CHARS`, 2000). The capped engine
  exists because Python's backtracking `re` cannot give exact semantics AND
  full-input AND ReDoS-safety at once, and it is required only for a pattern the
  fragment matcher would UNDER-match: one with a top-level alternation, or whose
  non-final fragments can over-consume across a `.*` gap. A pattern that splits
  into **one** fragment (no top-level `.*`) needs neither trade-off — there is no
  gap to backtrack across, so its single `re.search` is already exact AND
  full-input — and it therefore takes the unbounded path whoever authored it:
  built-in, edition-contributed, or user-added. This matters because a rule
  enforced over only a 2000-char prefix is bypassed by padding the command
  (`env PAD=<2001 chars> <denied cmd>`), which is not a guarantee the Settings
  panel should show as enforcing. So a contributed pattern that WOULD land on the
  capped engine is **skipped, not published** (`_matches_full_input`, the same
  principle as the `is_safe_user_regex` screen above): an edition rewrites the gap
  as a bounded class such as `[^;&|\n]*`, which keeps the pattern one fragment, or
  uses the un-weakenable overlay if it truly needs the loose form. Note the gate
  evaluates shell segments separately, so a pad behind a `;` was never the
  exposure — only a pad inside the SAME segment as the needle.
- **Discoverable in the panel.** A contributed row carries `source: "edition"` in
  the snapshot and renders an `edition` badge with a tooltip in
  Settings → Security, so an operator can tell a contributed rule from a shipped
  one. A seam whose rules are indistinguishable from built-ins would still leave
  the refusal unattributable, which is the gap the seam exists to close.

**How the always-on gates report and fail.** Three details of the gates that now
honour per-rule toggles:

- **The git-publish gate fails CLOSED on an unresolvable tag.** A floor tag naming
  no catalog row is a maintenance error, not a policy choice, and the two must not
  share a code path: skipping an unknown tag would turn a renamed rule id into a
  silent *allow* of a protected-branch push. An unresolvable tag therefore denies,
  logs at ERROR, and reports under the ungated row. The structural test in
  `test_push_branch_gate.py` still catches the drift at build time; this is the
  behaviour if that guard is ever removed.
- **The refusal names the rule ID; SEL keeps the pattern.** A gated git-publish
  denial leads with the rule id and carries the regex on its note line. The
  dashboard's RecoveryCard fills its chip verbatim from the first line, so leading
  with a ~70-character regex would make the most frequent denial an agent user
  hits unreadable, while the id is both short and the identity of the toggle that
  turns it off. The SEL event still records the pattern, which is what maps an
  event to a catalog row.
- **A regex spanning two rows attributes each match to its own row.** The nc/ncat
  reverse-shell regex covers two rules; denying while *either* was enabled meant
  switching `reverse-shell-nc` off left plain `nc` blocked by its sibling. Each
  match is now attributed to the row it belongs to (longest discriminator first,
  so `ncat` is never read as `nc`), and every match is examined, so a leading
  disabled spelling cannot shadow a trailing enforced one.
- **One shape earns one tag.** An all-branches flag suppresses the bare /
  single-argument fallback: with `--all` the absence of a refspec is not the
  "which branch is this?" shape, since the flag already names the target set
  exhaustively. Tagging both meant `push --all origin` also carried the
  single-argument tag, so disabling mirror-all left the command blocked by its
  sibling — enabled-and-off with enforcement unchanged.
- **The unbounded path is gated on backtracking cost, not just correctness.** A
  single-fragment pattern gets full-input matching (no 2000-char cap) *only* if
  `_polynomial_backtracking_prone` clears it. `_redos_prone` screens the
  EXPONENTIAL family at publication; it deliberately passes the POLYNOMIAL one
  (`a+a+$`, `\w+\d+$`, `.*.*!`), which is harmless on a capped window and not
  harmless off it — measured, `a+a+$` against 2,000 characters takes ~3.5s, 4,000
  ~27s, 8,000 ~228s, inside the synchronous PreToolUse gate. A flagged pattern is
  still enforced, on the bounded engine it already had; the predicate is NOT
  folded into `is_safe_user_regex` because refusing such patterns outright would
  drop rules that work today, and a rule silently not published is the defect
  this module fights rather than a fix for it.
- **One context snapshot per tool call.** The gate reads `current_context()` once
  and reuses it for both the always-on structural checks and the rule-catalog
  checks. Two reads let a live ceiling refresh land between them and judge one
  call half under each policy state. The direction that makes it matter: the
  structural IMDS/exfil checks are the only ones that catch an ENCODED address
  (`credential-exfil-imds-any` exists because the curl/wget patterns match a
  literal dotted quad), so a governance pin arriving after that point could never
  reach the encoded form — and honouring a pin late is not honouring it.

**Push-option spellings the target parser must not misread.** `git push` accepts
the repository as a flag *value* (`--repo=<x>` / `--repo <x>`), and both spellings
begin with `-`. A naive "strip the flags, the first positional is the remote" read
therefore treats the only remaining token as the remote, so `git push --repo=origin
main` classifies as the single-arg shape rather than the protected-branch one.
`--branches` is likewise git's own modern alias for `--all` (2.44+). Both were
harmless while the whole floor was unconditional — the misclassified shape was
denied anyway — and became bypasses the moment the rules were individually
disableable, since switching off the rule a shape is misattributed to publishes it.

**Abbreviations are resolved the way git resolves them.** Git accepts any
unambiguous PREFIX of a long option, so `--mirr` is `--mirror` and `--rep=origin`
is `--repo=origin`. Matching flag literals exactly therefore missed every
abbreviation, with the same misclassification consequence. The classifier now tests
whether a token is `--` plus a prefix of an option it cares about
(`_push_option_matches`), rather than comparing against a list of spellings — a
spelling list can only ever trail the next abbreviation. It deliberately does NOT
carry git's full option table: testing the prefix against only the dangerous
options is equivalent to resolving against every option and then intersecting,
because a non-dangerous option can only add a candidate and never remove a
dangerous one. That equivalence is asserted over every prefix of every `git push`
long option, so it is a checked property rather than a comment. A consequence worth
stating: an ambiguous abbreviation reads as dangerous (`--a` matches `all`), which
is free, since git refuses an ambiguous abbreviation itself and the command never
runs; a fully-spelled unrelated flag such as `--atomic` is unaffected, being a
prefix of nothing dangerous.

**Option arity is modelled, and a mis-parse can only over-protect.** Only `--repo`
had its separated value consumed, so every other value-taking push option
(`-o`/`--push-option`, `--receive-pack`, `--exec`) leaked its value into the
positional list, where it was read as a remote or refspec. The consequence was not
a misclassification but an ERASURE: for `--repo=origin --push-option ci.skip` the
leaked `ci.skip` became the sole "refspec", it normalizes to a non-protected name,
and the tag set came back empty — which is an ALLOW, since the tag set drives the
protected-branch decision. The scan now carries an explicit arity table
(`_PUSH_VALUE_OPTS`, resolved through `_push_option_matches` so abbreviations keep
working) whose separated values are consumed, and a no-value table
(`_PUSH_NO_VALUE_OPTS` plus the structural `--no-*` rule and the short-option
bundles) vouching that a neighbour token is positional. Anything else — a future
git option, an unmodelled arity such as `--recurse-submodules`'s — hits the
**fail-protective fallback**: the positional split is not trusted, BOTH
no-refspec rows are emitted — the bare tag, plus the single-arg tag whenever
positionals are visible, since an untrusted split cannot distinguish option
values from a remote and disabling whichever single row the fallback happened
to pick was demonstrated as a bypass three times in review (suppressed only
when an all-branches flag already covers a superset) — and EVERY positional is
scanned as a refspec
candidate so an actual protected name still reports its precise catalog row. A
token with an attached `=` value never disturbs the split, whatever the option, so
it is skipped as before; a bare `--` ends option parsing exactly as git reads it.
The invariant this buys: an unrecognised option can change the answer only toward
MORE protection, so the erasure class cannot silently reopen when git grows a new
value-taking option. The same fallback covers word FRAGMENTS: the scan tokenizes
on whitespace while the shell fuses a quoted or escape-continued span into one
word, so a value like `--push-option='ci skip'` arrives as fragments whose tail
would read as a refspec. `_push_token_shell_read` (one shared walk serving both
the fragment and operator-piece signals) walks the shell's own
quote/escape state over each raw token — backslash escapes outside quotes, inside
double quotes, and inside `$'...'` ANSI-C strings, but is literal inside plain
single quotes — and any token whose state does not return to normal (an open
quote, or a trailing escape that consumed the separator) poisons the positional
split the same protective way. An ESCAPED quote is data, not a delimiter: a
character-count/parity test was bypassed by `\"` in review, which is why the walk
tracks state rather than counting. Complete words keep their precise reading in
both directions — `"feat\"x"` is not flagged, and the quote-splice `'ma'\''in'`
still reads as exactly the protected-branch row. The `$`-lookback for ANSI-C can
misread `$$'` (PID expansion) as ANSI-C, which only ever OVER-flags, never the
reverse. Word-PRODUCING syntax is handled before the split assigns slots at all:
a `$` anywhere in any token — or a token-LEADING `~`, which is env-driven text
rather than path syntax (bare `~` IS `$HOME`, and `HOME=refs/heads` turns
`~/main` into a protected refspec; a mid-word `~` stays literal data) — lands
the segment on the ungated branch (parameter
expansion word-splits AFTER this scan — `V='ci.skip main'; git push --repo=origin
--push-option $V` hands git a `main` refspec the split never saw — and this slot
must not be weaker than the refspec slot's existing `$` posture), while glob
characters (`*` `?` `[`) and extglob patterns (`@(` `+(` `!(`) in any token keep
the wildcard-refspec identity, since
pathname expansion can produce words and none of those characters is legal in a
refname. Shell OPERATORS are consumed by the shell, never by git, so they are
handled before any argv-level reading (and before `--`, which is git's
end-of-options, not the shell's): a `#`-opened comment truncates the segment's
remaining tokens; a redirection token is excluded with the shell's own arity (an
attached target — `2>err`, `2>&1` — is self-contained, a bare operator consumes
the next word), which keeps `git push origin feature-x 2>&1` allowed while
`git push origin </dev/null` reads as the precise remote-only shape instead of
scanning a phantom refspec; a bare `&` (a single ampersand is not a segment
separator upstream, only `&&` is) or operator glue mid-word marks the split
untrusted AND scans the operator-delimited pieces, so a protected name cannot
hide behind glue — except that a word glued to a WELL-FORMED redirection
(`origin>/dev/null`, `main>log`) decomposes precisely instead: the pre-operator
word stays positional and the redirection is consumed, because bash reads it
exactly that way and the fallback's bare tag was the WRONG catalog identity for
a remote-only push (an identity miss is itself a hazard under per-rule
opt-out). Redirection arity also recognises bash NAMED descriptors
(`{name}>...` is all redirection — read as a word, the `{name}` became a
phantom refspec erasing every tag) and quoted TARGETS (`>'log'` — the operator
grammar admits no quotes, so a quote can only sit in the target group;
refusing the whole token for it had mislabelled the shape), while fragment
tokens (open quote state) still poison the split protectively. Quoted operator characters are data and none of this fires. A segment
whose CUMULATIVE quote/escape state is still open at its end continues into the
next line — bash line continuation (`\<newline>` vanishes) and quoted newlines
splice words across the newline segment boundary, so `origin ma\` + newline +
`in` pushes `main` while no scanned token spells it — and lands on the ungated
sentinel (the `ma$in` posture); a mid-segment open whose quote closes before
segment end stays on the disableable fallback, because in-segment joining can
only fuse whitespace into a word (never a valid refname) and the pieces stay
visible to the superset scan. The invariant
has two different strengths, stated honestly: the OPTION-ARITY axis fails
protective by construction (an unrecognised option lands in the fallback), while
the SHELL-SYNTAX axis rests on the metacharacter inventory being complete — a
construct the scan does not know parses as an ordinary word — so that inventory
is pinned as a checked property
(`test_every_bash_metacharacter_is_accounted_for` maps every bash metacharacter
to the layer that accounts for it: upstream separators, upstream
substitution/expansion ungating, the `$`/glob pre-checks, redirection/comment
consumption, the fragment walk, or a documented benign rationale). (Note the
earlier retraction of a 35-entry option table on
the dangerous-PREFIX axis is not precedent against these tables: prefix matching
is a set intersection where extra names are inert, while arity decides which
tokens are refspecs at all, so a table here does change outcomes.)

**Opt-out state — keystone `denied_commands.json`.** The opt-out state is a
security ceiling, so it lives in its OWN keystone file
`~/.kiro/crew/denied_commands.json` (respecting `KIROCREW_HOME`) — NOT in the
agent-readable `config.json`. The file's root IS the opt-out object:

```json
{
  "disable_all": false,
  "disabled_ids": ["<builtin-rule-id>", ...],
  "user_added": [{"id": "user-xxxxxxxx", "pattern": "rm -rf /tmp/mine", "enabled": true,
                  "note": "use a scoped path instead"}]
}
```

`note` is optional operator prose, surfaced in the refusal when that rule fires so
the caller reads remediation instead of a raw regex. It is metadata: it never
participates in matching. Create-only, mirroring `pattern` — neither has an edit
endpoint. Two constraints follow from the refusal being a parsed micro-format
(see "Refusal string" below): the add endpoint collapses whitespace and rejects a
note containing `DENY_REASON_PREFIX` (`note_forges_reason`), and
`hooks.resolve_denied_notes` drops any note that still carries the prefix, which
is the guard that holds for a keystone file edited by hand.

The file is on `_SENSITIVE_HOME_DIRS` (read+write block) AND the governance
boot-integrity `required` tuple, so the agent can neither READ nor WRITE its own
deny ceiling via any shell form — it inherits the mature `is_sensitive_path`
gate (variable-indirection, symlinks, `KIROCREW_HOME`, interpreters, casefold,
realpath), at the same protection level as `security_policy.json`. This is why
NO bespoke bash write-matcher is needed (an earlier config.json-based approach
plus such a matcher was removed: a matcher over shell text can never be provably
complete, and the keystone floor closes the whole class).

- **Read (boot / cron):** `hooks_config_from_config_dict` parses the flat hook
  keys from config.json's `hooks` section, then OVERLAYS the opt-out state from
  `load_denied_commands_state()` (the keystone file). config.json's
  `hooks.denied_commands`, if any, is IGNORED — the keystone file is the sole
  source. Parsed into `HooksConfig` as `denied_commands_disable_all` /
  `denied_commands_disabled_ids` / `denied_commands_user_added` (list of
  `UserDeniedPattern`). Missing file / keys → the safe "nothing disabled" state
  (fail-safe for a deny gate).
- **Write (dashboard):** the 6 `/api/security/…` mutations run
  `_write_denied_state` — an atomic read-modify-write of `denied_commands.json`
  under the shared config lock, routed through
  `atomic_write(restrict_to_owner=True)`. The lockdown lands on the temp file
  before any content reaches it, so the keystone never exists in a
  world-readable file: 0o600 on POSIX, owner-only DACL on Windows. On a
  lockdown failure `atomic_write` raises by default (the same fail-loud
  contract the other keystone writers in `apps/builtins/*` use for their
  `policy_store.py` and `secrets.py`), so a transient icacls failure cannot
  leave the ceiling under the inherited parent DACL.
  `_reload_live_hooks` splices the new opt-out fields onto the live
  `HookManager` (preserving its flat hook keys) so the change enforces without
  a restart. These operator endpoints open the file directly and do NOT route
  through the agent tool gate.

`HooksConfig.from_dict` remains **fully defensive** against malformed values
(type-checks every field; booleans — `disable_all`, the auto-approve flags, a
user rule's `enabled` — go through `_coerce_bool`, since `bool("false")` is
truthy in Python; unknown junk fails safe: `disable_all` → `False`, `enabled` →
`True`). The snapshot/handler read helpers apply the same normalization
(`disabled_ids` filtered to non-empty strings so a malformed `[{}]` can't raise
`TypeError: unhashable type`).

(config.json itself keeps its pre-existing write-only protection
`_WRITE_PROTECTED_HOME_PATHS` for its *resource-ceiling* fields, unrelated to the
opt-out state which no longer lives there.)

**Settings → Security UI** — the panel edits this state: a "disable all
built-in denies" toggle, per-rule toggles grouped by category (each category is
a collapsible accordion with a count, revealing the monospace pattern per rule),
and an add-your-own field for custom patterns. Built-ins are never deletable —
only disableable. Disabling a built-in (or the disable-all toggle) requires an
explicit acknowledgment in a confirm modal and writes a SEL audit entry
recording the weakened state. A governance-pinned rule renders locked (forced-on)
and cannot be toggled off. The seven `git-publish` category rules render the same
lock treatment for a different reason: their enforcement is the always-on
verb-anchored floor (`_is_git_publish` / `_is_push_to_protected_branch`), which
consults no opt-out state, so the snapshot forces them `enabled` and marks them
with `lock_reason: "floor"` (governance pins carry `lock_reason: "policy"`;
`pinned` keeps its governance-only meaning). `floor_enforced_builtin_command_ids()`
derives the set from the rule category — never a hand-maintained id list — and is
display/API-only: nothing in the enforcement path reads it. A `PATCH` disable for
a floor-enforced id is rejected with the same 409 shape as a governance pin
(`code: "floor_enforced"`, SEL-audited as denied with `=floor_enforced`) and
never persists into `disabled_ids`; re-enabling stays a no-op success. When *any*
rule is governance-pinned
(`governance_locked`), the **disable-all** toggle stays available and functional
(it shows the pinned-policy tooltip alongside the still-live control): the
backend keeps pinned rules enforced under `disable_all` via
`compute_effective_denied` (`(not disable_all and id not in disabled) OR id in
pins`), so a pin on one rule must not block opting every *other* (unpinned) rule
out.

**Live reload (no restart)** — a mutation hot-reloads the running
`HookManager` via `_reload_live_hooks` so the PreToolUse gate reflects the new
opt-out state immediately. The **heartbeat**-scoped manager
(`slack.gateway._build_heartbeat_hooks`, which drops the user's
`auto_approve_tools` so `HEARTBEAT_SAFE_TOOLS` is the sole approval authority) is
rebuilt **per heartbeat run** from the current primary manager — not snapshotted
once at init — so a just-disabled built-in or just-added user deny reaches
unattended heartbeat sessions without a gateway restart (cross-surface
consistency).

**Defense-in-depth nuance** — roughly a third of the rules overlap an independent
keystone control, and it matters which of those controls the rule's own toggle
now reaches:

- **Still always-on, opt-out cannot touch it:** the sensitive-file read floor.
  Disabling `sensitive-file-read` leaves `~/.aws/credentials`, the SEL log, the
  HMAC key and the rest of the fenced set blocked by the path floor, so such a
  command stays refused by defense-in-depth.
- **Now gated by the rule's own toggle:** the IMDS gate (`_check_imds_access`,
  keyed to `credential-exfil-imds-any`) and the bash exfiltration branches
  (`audit_bash_exfiltration`, each branch keyed to the catalog rule(s) it
  implements). These used to fire regardless of opt-out state. They are still
  default-ON — the toggle is opt-*out* — but an operator who disables the rule
  now disables the branch with it, which is the point: a rule advertised as
  disableable that stayed enforced anyway was a lie the Settings panel told.
- **Split:** the `git-publish` rules. The floor is still their *only* enforcement
  (their ReDoS-prone patterns never reach the regex tier), but the floor now
  consults the enabled set, so disabling one allows exactly the command shape it
  covers. The exception is `git-publish-push-brace-expansion-refspec`, whose
  coverage is an ungated anti-obfuscation branch; it is the one git-publish rule
  the Settings surface still locks (see the Protected-branch gate bullet above).

The ~85 purely-opinionated destructive rules (AWS delete/mutate, `cdk`/`terraform`/
`pulumi destroy`, `rm -rf`, `DROP DATABASE`, kill-kirocrew, reverse shells) have
no keystone backup, so disabling those fully unblocks them (the actual user ask).

**Governance enterprise force-pin** — the Level-1 `security_policy.json`
`commands`-scope deny patterns are the enterprise force-deny. `hooks.py` reads
them via `_governance_pinned_command_ids(ctx)` (backed by
`governance.resolve_pinned_commands`) and unions them into the effective set, so
a pin overrides user opt-out via tightest-wins. Because `security_policy.json`
is on the `_SENSITIVE_HOME_DIRS` keystone (the agent cannot write it), a pin is
un-opt-out-able by construction. See `governance.md`.

A **Level-2 profile** can also pin a `commands`-scope rule. Two accessors keep
enforcement and display correctly scoped:

- `pinned_builtin_command_ids()` (ENFORCEMENT) — the **active ceiling only**.
  The hooks gate force-re-adds these ids (tightest-wins) so a user opt-out can't
  weaken a *ceiling* pin. It deliberately does NOT union other profiles' pins: a
  rule pinned only for profile A must not be force-enforced for profile B or a
  no-profile session. Per-*profile* command enforcement is handled separately by
  the gate's `_governance_denial` commands-scope deny plane, which resolves the
  *bound* profile.
- `pinned_builtin_command_ids_for_snapshot()` (DISPLAY) — the ceiling pins
  **unioned with the pins from all loaded profiles**
  (`governance_profiles.all_profile_pinned_commands()`). Used by the
  surface-agnostic Settings > Security snapshot (and the builtin-toggle 409
  check) so a rule pinned by *any* profile renders locked and rejects a disable,
  never surfacing as a no-op opt-out (UI success while the bound-profile gate
  still denies). This is display-only and does not widen enforcement.

**Read-only auto-approve** — now that kiro-cli's `autoAllowReadonly` is retired,
`hooks.on_tool_call` auto-approves read-only tool calls itself, as the **last**
branch before `allow()` — after every early-return deny (deny-by-default shell,
sensitive-path, sensitive-bash, exfil, write-protected-config, effective deny
set, governance). Position guarantees a read-only classification can never
re-admit anything the deny/governance gates blocked. For a shell tool it
auto-approves only when `command` is present and
`dashboard.state.is_read_only_bash(command)` is True (deny-by-default: rejects
output redirects, substitution, and backgrounding; input redirects/comments are
refused for the position- or mode-sensitive verbs where a shell-elided word can
change the verdict). Help/version syntax does not create read-only authority:
the command must already match the explicit read-only command table, otherwise it
falls through to human approval. A non-shell tool is auto-approved when
`tool_kind in {"read", "fetch"}` or `slack.gateway._is_read_only_tool(tool_name)`
is True. Both classifiers are imported function-locally to avoid an import cycle.

Computer-use observation tools get their own **explicit** pair in that same
branch (`_cu_read_only_auto_approve`), keyed on the code-owned
`governance.computer_use_action_classes()` table rather than the
`_is_read_only_tool` title heuristic — that heuristic keys on a leading verb, and
an agent-supplied title must never decide whether a keystroke is synthesized into
somebody's window. It is additionally gated on the keystone primary enable, so no
auto-approval can exist while the feature is off. There is **no**
`computer_use.approval` ordinal and no approval-floor clamp helper: that row was
removed with the rest of the computer-use governance model, so nothing makes the
feature observation-only; see [governance.md](governance.md).

**The semantic `tool_kind` is an ALLOW-list, evaluated before ANY title-keyed
fast-path.** Only `tool_kind in _READ_ONLY_TOOL_KINDS` (`read`/`fetch`) auto-approves
outright; **every other non-empty kind returns `allow`** — i.e. falls through to
interactive approval — before the computer-use check or `_is_read_only_tool` is
consulted. A title-keyed branch is reachable only when the kind is **absent**.

**The computer-use auto-approve additionally requires an EXPLICIT read-only kind** —
it is reached only *under* that allow-list branch, never on an absent kind. Two
agent-controlled inputs meet there and neither may decide alone: the title, and the
absence of a kind (indistinguishable from an honest omission). They must agree.

Three findings shaped this, and all are worth keeping in view:

1. `tool_name` is the display title, and `select_tool_title` (`acp/_dispatch.py`)
   prefers the LLM-authored `description`, so it is **agent-controlled** — as
   `on_tool_call`'s own docstring states. The computer-use branch originally sat
   *above* any kind test, so once the operator enabled computer use, a mutating call
   titled `mcp__kirocrew-computer__computer_get_state` skipped the prompt entirely
   (verified for all six mutating kinds).
2. The first fix was a **denylist** (`kind in _WRITE_TOOL_KINDS` → `allow`), and it
   was still fail-open: `tool_kind` is passed through verbatim from the ACP `kind`
   field, so it is an arbitrary agent-influenced string and no enumeration of
   mutating kinds can be complete. `kind="other"` is a real ACP value and sailed
   past it. Hence the inversion — deny-by-default on the kind.
3. The allow-list still let an **omitted** kind through to the computer-use branch,
   so a `computer_click` could forge an observation title, send no kind, and
   auto-approve. Fixed by demanding the explicit kind. Deliberately *not* fixed by
   blocking absent kinds outright: the generic `_is_read_only_tool` fallback rejects
   every `mcp__kirocrew-computer__*` title anyway (asserted by a test, so it cannot
   quietly start matching), so a blanket block would have regressed every ordinary
   tool's "reads don't nag" behaviour for no security gain.

`_WRITE_TOOL_KINDS` survives as documentation of which kinds have been observed to
mutate; **the gate must not branch on it again**. Over-blocking here costs one
approval prompt, under-blocking costs the prompt that is the last thing between an
injected agent and a click. Pinned by
`test_hooks.py::TestMutatingKindBeatsTheTitle`, which asserts the unknown-kind cases
behaviourally AND asserts over the AST that `on_tool_call` references no
mutating-kind denylist.

### Computer use: a pixel/AX surface the path matchers cannot see

Native desktop GUI automation ([computer-use.md](computer-use.md)) is a security
surface unlike every other one in this module, and the difference is worth stating
plainly: **`is_sensitive_path` cannot see it.** A click has no path, a keystroke
has no command body, and a window's pixels have no filename. So none of the
mature matchers in this document apply — not the sensitive-dir regex, not the
relative-traversal block, not the symlink resolution, not the write-protected
tier. A Terminal window rendering `~/.aws/credentials` is, to the path gate, no
different from a text editor.

Three controls carry the weight instead:

1. **The app denylist is a floor in code** (`computer_use/policy.py::_DENIED_BUNDLE_PREFIXES`),
   matching by bundle-id PREFIX (so a helper process under a blocked bundle is
   covered) or case-insensitive process-name SUBSTRING (the Windows/Linux drivers
   may only ever learn a process name). Five categories: `terminal`,
   `credential_manager`, `system_settings`, `auth_prompt`, `kirocrew_self`.
   **`terminal` is refused for ALL verbs, reads included** — for two independent
   and individually sufficient reasons: a terminal window renders whatever the
   operator last `cat`ted as both AX text and pixels (and redaction only catches
   shapes it knows — it will not recognise an SSH private-key body or an opaque
   bearer token), and a keystroke into a shell prompt IS arbitrary command
   execution that never passes `hooks.on_tool_call`, bypassing the entire
   denied-command ceiling, the sensitive-path gate and the SEL audit of bash
   calls. `kirocrew_self` is there because KiroCrew's own dashboard can flip this
   feature's primary enable and the denied-command opt-out — controls that are
   out-of-band precisely so the agent cannot reach them. The list is
   operator-EXTENSIBLE (`extra_denied_apps` can only ADD) and never
   operator-shrinkable. There is no enterprise force-pin on top: the
   `computer_use.apps` ruleset was removed with the rest of that model, so
   `PolicyConfig.from_state` reads only `allowed_apps` / `extra_denied_apps` and the
   shipped entry plus the operator's own additions are the whole list.
2. **The secure-SUBROLE check**, and it must be the subrole. A real macOS password
   box reports `AXRole = "AXTextField"` (innocuous) with
   `AXSubrole = "AXSecureTextField"` and a **readable** `AXValue` — live-verified.
   So the intuitive `AXRole == "AXSecureTextField"` check **misses every password
   field**. The driver sets `secure = (role == SECURE_SUBROLE or subrole ==
   SECURE_SUBROLE)` and three protections key off that one flag: the renderer
   emits `<secure>` for the value (never the bytes, not truncated, not
   masked-with-a-hint), `policy.check_input_target` refuses
   `set_value`/`type_text`/`press_key` at a secure target, and a window containing
   ANY secure node gets **no screenshot at all** (whole-window suppression — there
   is no reliable way to blank a sub-rectangle of an already-encoded JPEG, and a
   partial redaction that missed would be worse than none). This floor has **no
   policy key and none will be added**: `resolve(None, None, …)` permits
   everything on an ungoverned host, so anything expressed only as a governance
   scope leaks by default for every single-user install. It belongs with
   `_SENSITIVE_HOME_DIRS` and the AKIA redaction, not with governance.
3. **The input-text scan as an explicit SECOND layer**, not the primary control.
   Text bound for another app's window is run through `is_sensitive_bash_command` →
   `audit_bash_exfiltration` → `is_denied` (called with `denied_regexes=None`, so
   it fails closed to the full built-in rule set — a user's opt-out from a bash
   deny rule is a decision about commands the AGENT runs under the tool gate, not
   a licence to type the same command into somebody else's window). This module
   already records the maintainers' position that chasing shell-parser
   completeness in a text matcher is a losing game, which is exactly why "refuse
   the app wholesale" comes first.

**Accepted residual — the screenshot directory stays agent-readable.** Persisted
JPEGs live in `<tmp>/kirocrew-computer-shots`, created `mode=0o700` with each file
passed through `platform_compat.restrict_to_owner` and ring-trimmed to 200 — but
the agent can still reach them with `fs_read`. This is the same posture browse
already ships; computer use widens WHAT can be in the frame (any window, not one
browser tab), which is bounded by per-window capture only (never full-screen) and
the whole-window suppression above. The design does not widen the posture and
does not claim to close it. A reviewer will find this independently, so it is
recorded here rather than left implicit.

Two further boundaries this module does **not** cover, stated so nobody assumes
otherwise: shell GUI automation (`osascript`, `cliclick`, `xdotool`,
`screencapture`, …) is a `commands`-scope item governed by the deny floor, never
re-parsed into GUI sub-effects; and the **web terminal PTY**
(`dashboard/handlers/terminal.py`) contains no `is_denied` /
`is_sensitive_bash_command` / governance call at all, so it is an operator-only,
ungoverned plane today.

### A variable LEAF under the keystone (`security.py`, `win_crew_var_leaf_path`)

`~/.kiro/crew` is not fenced as a directory — only its leaves are — so a read whose *filename* is unresolvable (`cat "$HOME/.kiro/crew/$F"`) can only be refused by asking whether the DIRECTORY holds a protected leaf. The token-level rule does that, but it needs a token: POSIX `shlex` destroys an unquoted Windows-native path before any token rule runs, so a raw-text branch matches the same shape directly on the command text, anchored to the crew home and its leaf-bearing subdirectories.

**The bracketing expansion forms match their OPENER and do not describe a body.** This gate only ever asks "does an unresolved expansion start here", and any answer that models the contents can be out-nested. Both failures were real: a body permitting one level of nesting (`(?:[^()]|\([^()]*\))*`) missed `$(a $(b $(c)))`, and `\$\{[^}\s]+\}` missed `${My Var}` because a PowerShell variable name may legally contain a space. `$(`, `@(` and `${` are therefore matched bare — that cannot be out-nested, and it can only deny more, which costs nothing here because a *resolvable* leaf under the keystone is already fenced by name.

The delimited forms keep their closers on purpose: an unterminated `%`, `!` or backtick is a **literal** to cmd, PowerShell and `sh` respectively, so it names no expansion and matching it would refuse ordinary filenames. Anchoring is what bounds the false-positive cost of the opener-only forms — `echo $(date)` and `type %APPDATA%\$(x)\config.ini` are unaffected, because neither names the keystone directory.

`test_security.py::TestKeystoneVariableLeafNativeSpellings` parametrises every anchor, both separators, both crew-home spellings and both variable and computed leaves; restoring either depth-limited body fails 48 of its cases.

### Suspicious Bash Patterns (`security.py`)

55 patterns in `SUSPICIOUS_BASH_PATTERNS` checked by `audit_bash_command()` at tool invocation time. Patterns with `*` use `fnmatch` glob matching; others use substring matching.

**Deletion patterns**: `find * -delete`, `find * -exec rm`, `find * -exec shred`, `xargs rm`, `git clean -f`, `shred `, `truncate `, `rm -rf /`, `rm -rf ~`

**Exfiltration patterns**: `curl * -d @`, `curl -d @`, `curl * --data @`, `curl --data @`, `curl * -F file=@`, `curl -F file=@`, `wget --post-file`, `nc * < `

**Pipe execution**: `| bash`, `| sh`, `| python`, `| perl`

### SEL Forward Callback (`sel.py`)

`set_forward_callback()` enables centralized log integration (basin/ktap). Events are redacted via `redact()` before forwarding to strip credentials and exfiltration URLs from string fields. Callback failures are logged at debug level (never silently swallowed).

### Credential File Permissions

`load_credentials()` in `loader.py` enforces `chmod 600` on `~/.kiro/crew/.env` at load time. If permissions are too open (group/other readable), they are tightened automatically. If `chmod` fails (e.g., file owned by another user), a warning is logged.

### Observe Mode Context Isolation

`channel_history.push` in observe-mode channels is gated on `_user_authorized`. Only messages from the owner or allowlisted users are recorded in the history buffer. This prevents non-owner messages from influencing LLM context via prompt injection through shared channel traffic.

### Slack Thread-Context XPIA Screening (commit 1fde6107)

When a new session starts inside an existing Slack thread, the handler fetches the thread-root message (`thread_parent_text`) and/or thread metadata (`thread_meta`) via `conversations.history` / `conversations.replies`. This content can be authored by **any** user — anyone who can post in a thread the bot participates in, not just the owner — so it is untrusted (XPIA) input. Beyond the existing `redact()` pass (credential/exfil stripping), `context.py:build_message` now:

- Screens both `thread_parent_text` and `thread_meta` with `security.contains_injection()` (a public wrapper over the shared `_INJECTION_PATTERNS` set, which lives in the dependency-free `vector_memory_constants` module and is re-exported by `vector_memory`) and **drops** the content on match; the parent branch then degrades to the bare thread-metadata block so the LLM still knows it is in a thread. The wrapper imports the pattern set at module top level and does **not** fail open — a screen that cannot run must not silently pass untrusted content through.
- Frames surviving parent text as **`[SLACK THREAD CONTEXT — UNTRUSTED DATA]`** wrapped in `<<<UNTRUSTED_THREAD_PARENT … >>>END_UNTRUSTED_THREAD_PARENT` delimiters, explicitly instructing the model to treat it as content to read and never as instructions to follow — instead of the prior "started by a prior session … here is what was posted" framing that presented it as trusted output.
- Emits a `prompt_injection_dropped` SEL audit event (`security.audit_injection_dropped()`, best-effort) whenever screened thread-parent or thread-metadata content is dropped, so attempted injection via shared thread surfaces stays visible in the audit trail.

### Mermaid Diagram Sandboxing

Mermaid `securityLevel` is set to `'strict'` in `MarkdownRenderer.tsx`, rendering diagrams inside an iframe sandbox. This prevents JavaScript execution from prompt-injected Mermaid diagram payloads.

### MCP Input/Output Validation (`validation.py`)

Centralized validation for all 12 MCP tool handlers (SDO-183):

- **Type-safe schemas**: `FieldSpec` + `ToolSchema` declarative validation
- **Unicode normalization**: NFC normalization + hidden character stripping (control chars, format chars, private use, surrogates — preserves `\n`, `\r`, `\t`)
- **Allow-lists**: enum enforcement for lesson categories, cron schedule kinds
- **Regex patterns**: agent name, job ID format validation
- **Range checks**: positive numbers for timeouts/intervals, valid timestamps
- **Length limits**: tool names (64), short strings (500), medium (5K), long (50K)
- **Unknown field rejection**: rejects unexpected fields in tool inputs
- **Response truncation**: 100K char limit prevents DoS from unbounded tool output
- **JSON-RPC 2.0 envelope validation**: request + response structure

### Foreign-Agent Import Boundary

Foreign-agent import treats every discovered file and database as untrusted
local input. Source ids are validated against the engine's registry (the shipped
foreign agents plus any an edition registers); Quick and unknown source ids are
not accepted. The
category catalog is likewise fixed to sessions, memories/preferences,
workspaces, MCP servers, user-authored skills, compatible schedules, and the
strict settings allowlist.

OpenClaw current discovery is restricted to `~/.openclaw` or normalized
`~/.openclaw-<OPENCLAW_PROFILE>` state, its JSON5 `openclaw.json`, and the
explicit `OPENCLAW_STATE_DIR`/`OPENCLAW_HOME`/`OPENCLAW_CONFIG_PATH`/
`OPENCLAW_WORKSPACE_DIR` overrides. The `"default"` profile means unprofiled
state. Only the documented `.clawdbot` legacy root with `clawdbot.json` or
`openclaw.json` is retained. `.moltbot`, implicit `openclaw.json5`,
`config.json`, root `mcp.json`, top-level sessions, and guessed root databases
are not scanned.

`GET /api/onboarding/import/scan`, `POST /api/onboarding/import/apply`, and
`PUT /api/onboarding/import/state` all require normal dashboard
authentication. Apply revalidates source/category selection and current
filesystem state instead of trusting scan output or client-supplied paths.

Security invariants:

- **No secret movement:** credentials, tokens, cookies, literal MCP environment
  values/headers, security policy, governance profiles, admission/deny state,
  and other secret-bearing records are reported by category/reason only and are
  never returned as values or copied.
- **No executable authority:** hooks, native agents/personas, raw
  instructions/system prompts, tool transcripts, approval state, provider
  sessions, and runtime/security state are never imported.
- **Constrained projections:** sessions keep visible user/assistant text only;
  memory goes through native writers/limits; workspaces must resolve to valid
  existing non-sensitive directories; MCP requires exactly one secret-free
  stdio/HTTP transport and cannot replace managed servers; skills are
  user-authored, source-namespaced, traversal-safe, and symlink-safe; schedules
  are rejected whole when foreign execution, routing, repetition, provider, or
  security semantics cannot be preserved, semantically deduplicated, and
  created disabled; settings use a strict non-security allowlist and preserve
  existing values.
- **Bounded databases:** before a supported foreign SQLite store is opened, its
  main file and present `-wal`/`-shm` sidecars must be regular non-symlink files
  whose aggregate size is at most 64 MiB. Unsupported durable stores, including
  Hermes `memory_store.db`, are diagnosed without opening them. A lineage store's
  active memory rows are capped across both supported tables before either
  contributes import candidates.
- **Merge-only and idempotent:** existing KiroCrew data wins. A provenance
  ledger prevents replayed source items from creating duplicates and carries no
  grant of trust or permission.
- **Read-only source:** scan/apply never rewrite, move, delete, chmod, or
  otherwise mutate a foreign source tree. Unsupported, malformed, secret, or
  over-limit entries are skipped and reported rather than coerced. Malformed
  JSONL invalidates the complete file and any workspace provenance collected
  from its prefix. Symlinks and Windows reparse points/junctions are rejected at
  source traversal and destination skill ancestry boundaries.

Import is not a governance bypass. Every imported artifact is still subject to
the destination's ordinary security checks and to the effective
`POLICY ∩ PROFILE` ceiling; imported data cannot weaken either level.

### Dashboard Authentication & Authorization

**Dashboard URL config** — single `dashboard.url` field in `config.json` (e.g. `http://my-host.example.com:8080`). Hostname, port, local-only mode, and allowed origins are all derived from this URL. When not set, defaults to `localhost:5476`. `KIROCREW_PORT` env var overrides the port (dev mode).

**SSH tunnel instructions** — All SSH tunnel commands printed by `kirocrew gateway` and `kirocrew doctor` now use the `-N` flag (`ssh -NL ...`) to suppress remote shell allocation. The tunnel purely forwards the port without opening an interactive session on the remote host.

**Local-only resolution** (`origin.py:is_local_only()`):
- No Slack → always local-only (no auth layer available)
- Loopback host in URL (localhost, 127.0.0.1, kirocrew.localhost) → local-only (`127.0.0.1`)
- Non-loopback host or auto-detect on remote machine → all interfaces (`0.0.0.0`)

**Token authentication** (`token_auth.py`):
- HMAC-SHA256 signed tokens with dual expiry: 5-minute link click window (`exp`) + session TTL up to 20 hours (`session_exp`)
- `!dashboard` and `/kirocrew dashboard` available to owner and allowed users; link always sent via DM (never in channel)
- First use: validates `exp` (5-min window), binds IP, marks consumed, sets `mc_token_{port}` cookie with `max_age` from `session_exp`
- Subsequent requests: validates `session_exp` via cookie
- `parse_duration()` caps at 20 hours max (MAX_SESSION_TTL_SECS = 72000)
- Loopback access trusted only in local-only mode (SSH tunnel); on all-interfaces mode, all requests require a token
- `token_auth_middleware(local_only)` — single boolean controls all auth behavior
- **Secure cookie flag via `origin.is_https_request()`**: the `mc_token_<port>` cookie (and the refresh cookie) set `Secure` only when the request is HTTPS — `is_https_request(request)` returns True for a direct HTTPS request, or when `X-Forwarded-Proto: https` is present **and the immediate peer is loopback** (a TLS-terminating tunnel/proxy forwarding into the loopback-bound gateway). Plain-HTTP localhost must NOT set `Secure` or the browser refuses to send the cookie back

**Per-session logout (CWE-613)** (`token_auth.py`): the access cookie is a self-contained HMAC-signed token, so clearing it client-side (`Set-Cookie max_age=0`) does not stop a saved copy replaying until its `session_exp` (up to 20h). `RevokedNonceStore` is a persisted denylist of explicitly-revoked access-cookie nonces (`token_revoked_nonces.json`, mode `0600`, survives gateway restart; each entry stores the token's own `session_exp` as an eviction floor so the file cannot grow unbounded). `POST /api/auth/logout` → `revoke_access_cookie()` validates the token, then records its nonce; `validate_token` (cookie path) is **deny-by-default** — a token whose nonce is revoked, or that carries no nonce at all, is rejected. Link-click token exchange also mints a SEPARATE session cookie (fresh nonce, `register_nonce=False`) rather than reusing the one-time URL/link token as the long-lived cookie, and denylists the consumed link nonce so a captured link copy cannot be replayed as `mc_token_<port>` (the query-param LINK path does not consult the denylist, so legitimate re-navigation of the same link URL within the 5-minute window still re-exchanges for a fresh session cookie).

**Structured monitor API authorization** (`dashboard/handlers/autonudge.py`):
browser `GET/POST/PATCH` monitor routes expose durable provider observations and
can replace, restart, or stop the one record bound to a session. They therefore
require `is_owner_dashboard_request` before resolving a caller-selected slot or
monitor id or parsing a body. This closes the gap where an allowed Slack user can
receive a valid dashboard cookie but is not the operator who owns every local
session. Stale bootstrap subjects receive the shared re-authentication response;
other denials carry `dashboard_owner_required`. The agent-facing
`GET /api/autonudge/session-monitor` is instead a strict-internal route: both the
authentication middleware and the handler require the internal-secret trust
marker before the supplied `X-Session-Key` is resolved, with no browser-cookie
fallback. Allow and deny decisions are best-effort SEL audited with operation and
coarse reason only.
Legacy AutoNudge reads exclude structured records, and structured WebSocket
state is sent only to the owner-authorized client set. If a structured id is
presented to the legacy DELETE route, that route applies the same owner gate
before delegating to the monitor stop authorizer.

**Pull-request provider authorization and audit** (`dashboard/handlers/source_providers.py`): every full-source read, checks read, review-thread mutation, and background sidebar refresh may inherit host `gh`/`glab` credentials. Which instance those credentials may reach is not browser-controlled: `github.com` and `gitlab.com` are always accepted, and a self-managed GitLab host is accepted only when its exact `host[:port]` appears in the operator's deny-by-default `dashboard.gitlab_hosts` allowlist. Adding an entry is an explicit operator decision to let the local `glab` CLI reach that host, including one only resolvable on the internal network; the allowlist is matched exactly (no suffixes, wildcards, or `www.` stripping), malformed entries are dropped at config load rather than sanitized, and `_run_json` re-checks the host before spawn so a code path that skipped URL validation is denied instead of reaching an unauthorized instance. A self-managed target additionally loses `GITLAB_TOKEN` from the provider child environment: the variable is a single ambient credential with no host binding, so forwarding it alongside a redirected `GITLAB_HOST` would disclose a gitlab.com PAT, and every permission it carries, to the self-managed server. Those hosts authenticate from their own per-host entry in glab's config. Direct source APIs require the explicit empty `request["app"]` dashboard claim. With a configured `DashboardState.owner_id`, reads and mutations require exact equality with `request["user"]`. With no configured owner, only full-source and checks reads accept the signed machine-local bootstrap subjects `local-app` and `local-startup`; review-thread mutation remains denied. Machine-local startup and local-secret token issuance use the configured owner id as their subject when one exists, so the auto-opened dashboard and `kirocrew token` satisfy the same exact owner check. Missing claims, non-owners, app tokens, unrelated local subjects, and every unconfigured-owner mutation fail closed with 403. Every direct API attempt makes a best-effort SEL access record with only the caller, operation, and coarse reason. URL, thread id, provider text, and credentials are omitted. SEL write failure cannot weaken an authorization denial or replace the request's response or exception. Cancellation during request-body parsing or provider work is recorded as `failed/request_cancelled` when SEL is available, then the original cancellation is re-raised.

`_run_json()` emits credential-free SEL tool-invocation lifecycle events around every provider CLI attempt. Unsupported providers, invalid bounds, Windows sandbox absence, untrusted executables, and sandbox rejection record `denied`. An allowlisted command awaits its synchronous critical `invoked` append on a worker thread immediately before spawn, so an audit filesystem failure denies execution rather than launching a credential-bearing process unaudited, without blocking the gateway event loop. Cancellation while that worker is active remains fail-closed and waits for it to settle; if `invoked` landed, cleanup records `failed/request_cancelled` before re-raising and never spawns the provider. Provider launchers run in a dedicated process group, and timeout, output-overflow, and cancellation cleanup kills and reaps the complete launcher/provider tree so a sandbox wrapper cannot leave `gh` or `glab` orphaned on an unread pipe. Successful JSON decoding records `completed`; spawn, output, timeout, nonzero exit, decode, cancellation, and internal errors record `failed` with only a coarse reason. Audit records contain the logical provider (`gh`/`glab`), not argv, URL, repo path, output, environment, token, thread id, or exception text. Terminal audit failures are best effort and never alter an already-completed provider result.

**Structured GitHub monitor provider boundary**
(`monitoring/github_pull_request.py`): background pull-request shadow probes are a
separate monitor-owned consumer of the shared synchronous `github_runner`, not of the
dashboard handler. The target gate accepts only exact public `github.com` HTTPS
pull-request identities and normalizes `www.github.com`; it refuses arbitrary and
enterprise hosts, credentials/ports, repository-only paths, suffixes, queries,
fragments, raw control characters, URL parameters, non-canonical numeric aliases,
oversized pull-request numbers, and invalid owner/repository segments before resolving `gh`. Every
provider call uses the runner's validated absolute executable, minimal GitHub-only
environment, audit-or-deny invocation record, strict UTF-8 decoding, and
`pin_host="github.com"`; no monitor-specific token source or credential storage
exists.

Raw stdout, stderr, response envelopes, URLs, timestamps, cursor/request ids, bodies,
comments, and logs never cross the adapter boundary into monitor state, exceptions,
or logging. Canonical state is an explicit small allowlist; check labels are stripped
of controls and URLs, passed through `security.redact()`, and bounded in length and
count before persistence. Provider failures
are reduced in memory to fixed error kinds and reason codes, including a non-retryable
setup kind for missing, untrusted, or unexecutable `gh`; raw diagnostic text is then
discarded. The load-bearing primary read excludes `statusCheckRollup`; checks are read
separately with the head revision, so a missing Checks permission or a push between
requests produces typed incomplete supplemental evidence without erasing authorized
primary facts. Open-PR review-thread pagination is bounded to ten 100-node pages,
ignores outdated threads, and preserves usable nodes from partial GraphQL errors;
incomplete or capped evidence fails closed as pending. A terminal merged/closed
primary state does not issue either supplemental request. Shadow execution has no dispatcher
dependency and refuses an enabled wake request before either provider or persistence
work, so it cannot turn ambient GitHub authority into a model wake in this slice.
Locally imposed check and review-thread caps remain durable incomplete evidence and do
not consume the provider-error budget; transport failures and malformed provider
pagination remain typed supplemental errors.

The provider adapter's redaction is classified as inbound canonicalization rather
than an egress surface. The structured monitor controller is the corresponding
registered redaction sink: it passes the complete bounded wake envelope through the
exfiltration-URL and credential scanners before injecting it into an agent session.

Sidebar status follows the same read-only boundary. `GET /api/chat/slots` and the WebSocket handshake schedule provider refreshes and opt into cached `ci`/`state` fields only for an exact configured-owner request, or for signed `local-app`/`local-startup` dashboard subjects when no owner is configured. Generic slot serialization omits those fields. `DashboardState` tracks owner-authorized WebSockets separately, sends generic slot updates to all authenticated clients, then overlays credential-backed status only to the owner subset. This prevents a cache populated by an owner request from being replayed to a non-owner or app-token caller. Review-thread cache removal, generation advancement, and stale in-flight detachment still complete after thread ownership validation and before mutation dispatch, so cancellation cannot preserve or repopulate pre-mutation data.

**Stale pre-owner sessions must re-authenticate (`stale_session_reauth`)**: a dashboard token's subject is fixed at mint time as `owner_id or <bootstrap subject>`, and both `POST /api/auth/refresh` and the one-time-link exchange re-mint from the INCOMING subject, so a session signed in before `KIROCREW_OWNER_ID` was configured carries `local-app`/`local-startup` for its whole life. Setting or changing `KIROCREW_OWNER_ID` therefore requires every pre-existing dashboard session to re-authenticate: once an owner exists, the owner gate denies the bootstrap subjects, and that denial is the control working — re-accepting them would readmit every machine-local token to an owner-locked dashboard. The operator surprise comes from `owner_id` being overloaded: it is collected as the Slack Member ID for owner DM routing, but it is also the dashboard authorization principal and the token subject, so setting it for Slack DMs also rotates the dashboard's identity anchor. To make the remedy discoverable, every owner-gate deny site that fronts the shared owner predicate (`stale_owner_session_response` in `source_providers.py`, consulted by the chat mode/approve/worktree/followup gate, the source-provider routes, cloud provisioning, MCP-app calls, `ask_question`, the browser mutations, agent-config mutations, the AWS consent gate, and the instances federated search) labels exactly this case `401 {"code": "stale_session_reauth"}` instead of the generic `403 forbidden`, and the dashboard turns that signal into a sign-in-again banner that deliberately skips the silent-refresh path (refresh preserves the stale subject, so it can never recover this denial); direct-fetch surfaces that bypass the blessed transport (the app-sdk scoped API, the MCP-app tool relay, Mochi's approval bridge) raise the same prompt through the shared `staleOwnerSignal` detector. CHANGING an already-set owner also invalidates the previous owner's sessions, but those carry the old owner's subject — an ordinary non-owner now — so they keep the generic denial: the distinct label is only derivable for the bootstrap subjects, whose staleness is provable from the subject alone. The label is chosen strictly AFTER the deny decision — access is never granted, widened, or re-ordered — and only for an ALREADY-AUTHENTICATED dashboard-user caller whose signed subject is a bootstrap subject while an owner is configured; unsigned, invalid, app-token, and ordinary non-owner callers keep the generic denial, so the discriminator discloses nothing to an unauthenticated party.

**App-token least-privilege scope (CWE-269)** (`token_auth.py`): an app token is confined to its own app namespace + the API path prefixes the app declares in its manifest `permissions.api` allowlist; everything else is denied. `_enforce_app_scope()` is **deny-by-default** — `_app_api_allowlist()` returns an empty tuple on any failure (app not installed, manifest unreadable), confining the app to its own namespace only. Enforced at all grant points (the normal cookie/query-param flow and the cross-app `/apps/<other>/api` reverse-proxy path re-check); dashboard-user tokens (empty `app` claim) bypass the gate entirely. Denials emit a `log_api_access` SEL event (`operation="app_scope_check"`, `outcome="denied"`).

**Kiro prerequisite setup boundary (`kiro_prerequisite.py`)**: the dashboard's
status/install/login endpoints require the exact configured owner. Before an
owner exists, only the signed `local-app` and `local-startup` dashboard subjects
may use them; generic dashboard-user and app-token callers are denied and
audited. The two mutations also pass the shared Origin/Referer CSRF check. They
expose exactly three fixed verbs and accept no request-selected executable,
argv, installer URL, redirect downgrade, output path, or shell fragment.
macOS/Linux download only `https://cli.kiro.dev/install`; Windows downloads only
`https://cli.kiro.dev/install.ps1`. Every redirect and the final URL must remain
on the exact `cli.kiro.dev:443` host and expected path, with no credentials,
query, or fragment. Automatic redirect following is disabled: each `Location`
is resolved and validated before its destination request, with a three-redirect
limit. The downloader rejects oversized bodies, supports explicit HTTP(S)
proxies while bypassing `.netrc`, then requires both a release-pinned SHA-256
digest and the platform-specific official marker. An upstream installer change
therefore fails closed until KiroCrew updates the pin. The same validated bytes
remain in memory and execute through the fixed system interpreter's standard
input, closing the validation/execution replacement window. The unsandboxed
official installer receives a system-only `PATH`. Explicit login
inherits only the allowlisted user-path, UI/device-flow, TLS, and proxy values;
passive probes receive a narrower environment that carries TLS trust and, on
hosts that need it, desktop-session IPC, while excluding ambient cloud, Slack,
SSH-agent, and application credentials. Proxy configuration (both case
spellings, since matching is exact on POSIX and HTTP stacks disagree on which
case they honour) joins only the `whoami` identity stage: a proxy-only host is
exactly where `whoami` must still reach the IdP, but a proxy URL can embed
credentials, and `--version` is the first execution of an unvalidated
candidate that needs no network — so the version stage stays proxy-free and
the exposure delta is confined to a candidate that already passed the version
gate, reaching the same resolved binary an ACP session already runs with the
full inherited environment. The one deliberate *credential* exception is Kiro CLI's
OWN model credential (`KIRO_API_KEY`, `_IDENTITY_PROBE_ENV_KEYS`), forwarded to the
`whoami` identity probe only: the CLI reports an API-key session as signed in only
when it can see that variable, so filtering it out reports a host that ACP
authenticates on as signed out. In a post-scrub Docker container the variable
lives only in the data home's `.env` (the entrypoint scrubs every
`_CREDENTIAL_KEYS` entry — this one included — out of the gateway's
`/proc/<pid>/environ`), so the identity probe and the kiro-cli spawn paths read
it back from that file for exactly the one child that owns it; every other
scrubbed credential stays in-process. The exposure delta is that one probe's argv — the
credential reaches the same resolved binary the same probe already executes, in the
same standard sandbox posture, against the same real home. The `--version` probe,
which is the first execution of a candidate that has not yet answered anything,
stays credential-free. `whoami` decides identity from the CLI's exit status alone,
and that status reports which credential kind is configured rather than whether the
credential is accepted, so a stale or mistyped key reads as signed in.

Output and client-visible errors are bounded and credential/exfiltration-
redacted. Only HTTPS URLs on the exact official `app.kiro.dev` host or the
`/start` device path on `view.awsapps.com` are linkable. User-triggered
install/login records a critical `invoked` SEL event before spawn (audit failure
denies execution), followed by a best-effort terminal event. Passive
`--version`/`whoami` probes use the same paired audit lifecycle; probe events
contain only the probe kind and coarse outcome, never argv, candidate path,
output, or environment. One operation may run at a time. Filesystem candidate
and interpreter discovery runs off the asyncio event loop. Timeout,
cancellation, and gateway shutdown terminate and reap the full child tree using
`platform_compat`. A private POSIX supervisor remains the process-group leader
until all group members exit, so a pipe-holding descendant cannot outlive an
exited command leader or turn a retained PGID into a reuse hazard. The gateway
captures the supervisor source before agent sessions begin and
invokes it from memory with isolated Python; the supervisor wraps the completed
sandbox launcher as the outermost process, resolving a sandbox or cgroup
wrapper's executable to an absolute path before the supervisor's `execve`.
An agent cannot replace a mutable
supervisor file immediately before an owner-triggered operation, and the Linux
namespace launcher and supervisor never wait on each other.
Windows synchronously retains an identity-stable handle for the primary process
after spawn, and successful process completion awaits the descendant tracker
until every retained child is inactive and terminally scanned. An immediate-exit
launcher therefore cannot disappear before its helpers are anchored or report
success while a detached installer remains live. Discovery continues from every
live child, so late helpers are still terminated before the deadline. Each exact
root receives one final post-exit snapshot before tracking removes it, closing
the between-polls child spawn/parent exit race. Every Toolhelp parent-PID edge is
checked twice against
creation and exit times read from the exact root, retained-parent, and
newly-opened child handles. Genuine children created before an immediate-exit
parent remain eligible, while a child attached to a recycled root or
intermediate PID is rejected. Failure to retain the primary handle or validate
its identity, create a Toolhelp snapshot, or complete any initial or later
enumeration fails the operation closed; opened child handles are closed before
the error propagates. One deadline covers process exit, initial and terminal
discovery, and inherited output-pipe closure.

Unverified candidate version probes route through
`sandboxed_spawn_argv(..., mode="strict")` on POSIX. The outer sandbox launches
an unverified candidate through the absolute system `/usr/bin/env` entrypoint,
preventing a planted `kiro-cli` basename from selecting the provider's trusted
internal macOS delegation path. The strict wrapper additionally hides the
configured data home, `~/.kiro/crew`, `~/.kirocrew`, and all known Kiro
identity stores, so setup probes cannot read Kiro Crew state or bearer tokens.
Trust is "the CLI runs, and it has a valid login": a Kiro CLI that answers
`--version` is eligible for `whoami` and device login, regardless of install
source, owner, or fixed path. KiroCrew is not the authority on where Kiro CLI
is installed, and Kiro CLI's own self-updater legitimately rewrites its bytes as
the user — so an install-source/owner/path/Developer-ID gate would strand real
installs (toolbox, Homebrew, winget, a self-updated `/Applications` bundle) with
no in-product recovery path, which is the concrete first-run/reauth dead end
this model removes. `whoami` reporting a valid session is what makes readiness
true; a runnable CLI never surfaces an unreachable "repair" state.

**The Kiro CLI is always executed IN PLACE, on every code path** (ACP spawn, auth
commands, `whoami` probes, `/usage`, `--list-models`) — never from a private copy.
The earlier design copied the resolved bytes into a per-call directory below the
staging parent, or into `<data-home>/run/kiro-cli-snapshots` (a sealed memfd on
Linux), and executed the copy, binding the launched process to the bytes just
resolved. That resolve-to-exec byte-binding is **removed**: Kiro CLI 2.15+ is a
multi-call binary that dispatches by exec'ing a sibling `kiro-cli-chat` resolved
relative to its own path, so a copy into a flat directory made every spawn fail
with ENOENT. The TOCTOU it closed requires an attacker who already has write
access to the user's own machine — outside this product's threat model, and not
defended against elsewhere — so the copy is not worth the breakage. Do NOT
reintroduce it. Installation still refuses a no-op (unchanged-digest) or shadowed
install so the Install button cannot silently succeed without producing a working
target.
Auth commands use `mode="standard"`; the fixed `~/.kiro/crew-auth-staging`
parent is on the shared sensitive-path floor and hidden by every agent sandbox.
Sign-in is delegated to Kiro CLI: `login --use-device-flow` runs against the
user's real home and environment with only the Kiro Crew data homes — the
configured home, `~/.kiro/crew`, `~/.kirocrew` — hidden, and the CLI writes
its own credential store where it normally keeps it. KiroCrew stages no
credentials and copies none back, so there is no publication step, no
cross-gateway publication lock, no pre-publication identity-generation scan, no
SQLite backup-API republish, and no "identity changed during sign-in" conflict
for two racing gateways to hit. The real-home run is a subset of an accepted
surface rather than a new one: ACP launches the same resolved Kiro CLI with the
full real environment under the same standard sandbox on every agent session. A
credential-minimal temporary home remains available as an opt-in read-only mode
for callers that must never see the real `~/.aws` / `~/.ssh`: its random
per-call workspace below the staging parent receives HOME/XDG/AppData and holds
only the allowlisted `kiro-auth-token*.json` and Kiro CLI identity SQLite files,
the identity stores are hidden on top of the Kiro Crew data homes, and the
workspace is removed on every exit path — success, failure, timeout,
cancellation, or exception. A matched live identity file that cannot be captured
under the bounded regular-file rules aborts that staging path before the command
runs; it is never omitted as though absent. No production caller currently
selects the isolated mode, since the readiness probe also runs real-home.

The Kiro CLI identity database (`data.sqlite3`) is **projected, never
byte-copied**, and is therefore deliberately exempt from the
`_MAX_AUTH_STORE_FILE_BYTES` (64 MB) cap that governs every other staged
identity file. That database is the CLI's main store: identity occupies two
small tables (`auth_kv`, `migrations`), while `history` / `conversations*` hold
chat transcripts and grow without bound — a real user's store reached ~429 MB.
Byte-copying it both aborted sign-in for those users (with a message naming
neither size nor cause) and read the whole file into memory to write it straight
back out. Projection copies every table/index **DDL** plus the **rows** of the
identity tables only, so the staged file is bounded by the identity data alone
however large the source grows, and the sandboxed CLI receives no transcript
content. `state` is a mixed key/value table — a few rows describe *which*
identity is signed in (Identity Center region + start URL, CodeWhisperer
profile) and the rest is unrelated local state (telemetry ids, onboarding flags,
prompt counters) — so its rows are carried **selectively by key prefix**
(`auth.`, `api.codewhisperer.`), letting `whoami` render its full profile block
without handing the sandboxed CLI the user's telemetry identifiers. The match is
by prefix rather than an exact key list so a newly added `auth.idc.*` key is
carried automatically instead of being silently dropped; `state` itself is
optional, so an older schema without it still stages. The full schema is copied rather than just the identity tables because
`migrations` is projected with its rows: the CLI then treats the schema as
already current and runs no migration, so a store holding only identity tables
would fail with `no such table: history` on first use. Projection keeps the byte
path's defenses — reject a symlink, require a regular file, open read-only — and
creates the destination `0o600` before writing, so identity rows are never
briefly world-readable. A source that is unreadable, is not a database, or
is missing **any** required identity table fails closed and aborts staging,
rather than handing the CLI an empty store it would read as signed-out. The
all-or-nothing table check is deliberate: a future Kiro CLI that renamed one
identity table while keeping the other would satisfy an any-of check and stage a
store whose schema is present but whose identity rows are absent — silently
producing the signed-out outcome the check exists to prevent. Requiring all of
them turns a schema change into a loud abort instead. Consequently the SQLite
sidecar filenames are no longer staged: reading through SQLite already applies
any pending WAL/journal state.

The source is opened `mode=ro` **without** `immutable=1`, deliberately.
`immutable=1` would guarantee no sidecar is ever touched beside the user's live
database, but it also asserts the file cannot change, which makes SQLite **ignore
the `-wal`**: against a store in WAL mode whose newest commits are still
WAL-resident, the token row reads as missing and the staged store presents as
*signed out* — a worse failure than the size abort this projection replaces.
Plain `mode=ro` applies the WAL, so the staged identity always matches what the
CLI itself would read. The accepted cost is that SQLite may create or refresh the
`-shm` shared-memory index beside the live database exactly as any other reader
does; `-shm` carries no identity data, and no bytes are ever written back to the
user's store. A regression test pins the WAL-resident case.
Candidate discovery spans the inherited `PATH`, interpreterScripts directory, and explicit operator override on every OS — a runnable
candidate from any of these is eligible, since trust is "it runs". Status
requests never mutate `KIROCREW_KIRO_BIN`. Electron delegates entirely to this
gateway service and does not execute a second candidate or installer path. For
each local-token request Electron re-resolves the authoritative migrated or
pinned data home, reads exactly that home's one bootstrap secret, and sends it
only to the literal `127.0.0.1` gateway bind address; it never probes canonical
and legacy secrets across multiple loopback addresses. ACP launch does not
re-impose a provenance gate: the shared client/runtime resolver accepts any
runnable candidate and canonicalizes symlinks before its final no-follow open.
Every platform then launches that candidate **in place** — the resolved path
itself, never a private copy of its bytes (see the in-place launch record above:
a multi-call Kiro CLI resolves its sibling subcommand executable relative to its
own path, so a copy strands it). Explicit Kiro classification preserves
internal-sandbox delegation without relying on the executable basename. There is
deliberately **no** resolve-to-exec byte-binding and no install-source/owner
gate: arbitrary unsandboxed same-user native code is outside the enforceable
in-process boundary regardless, gating on origin only strands legitimate
self-updating installs, and a swap between resolve and exec requires local write
access this product does not defend against anywhere else. This is an operator-triggered
system prerequisite, accepts no LLM input, and is absent from the headless MCP
server route set.

**App manifest permission model — advisory (`apps/permissions.py`)**: distinct from the HTTP app-token scope above, the App Kit manifest `permissions` block (`mcpTools`, `network`, `memory`) is currently **advisory, not enforced in-process**. `validate_permissions()` and `format_permissions_summary()` exist but are **not wired into the install or runtime path** — they have no callers outside `test/`, so the manifest `permissions` block is neither enforced nor even surfaced today. `check_tool_permission()` **fails open on an empty `mcpTools` allowlist** (returns `True`) and is not called at the tool-dispatch boundary, so `mcpTools` is a review/display signal rather than a runtime capability gate. (Install-time path-traversal blocking is a separate mechanism: `_check_path_safety(name)` + `manifest.validate()` in `_validate_source_path`, not the permission validator.) Real in-process enforcement (and per-resource `owner_app` ownership) is tracked in `docs/request-for-change/rfc-app-sandbox-isolation.md`; today an installed app runs with the user's full trust, confined only by the HTTP app-token scope, the OS sandbox, the `agent.apps_allow_third_party` off-switch, and destructive-command deny patterns (TRACKING).

**Third-party app execution boundary (`apps/execution.py`)** (CSE SEC-012): admission and governance decide which apps may be installed/activated; this separate runtime boundary decides whether admitted app code may execute. `agent.apps_allow_third_party` defaults to `false`, and only the literal JSON boolean `true` is an explicit grant (truthy strings/numbers and environment variables do not admit). `app_execution_denied()` is the shared provenance/config/audit decision used before in-process module loading, backend dependency setup/adoption/spawn, lifecycle shell commands (`onEnable`/`onDisable`/`onUninstall`), registry detection/build/`onInstall` commands, and `openCommand`. `enable_app()` evaluates it before persisting `enabled=true`, so denial leaves metadata, resources, dependencies, scripts, hooks, and backends untouched; `handle_open_app` separately requires the app already be enabled. A config-load error fails closed. Positively identified shipped builtins are exempt. Every denial emits one `app_execution_admission` SEL event carrying the action and fixed provenance classification; the config/API-derived app name is deliberately omitted from that event. Self-registration cannot claim `origin=builtin`; that provenance is reserved for `register_builtin_apps()`. New repository grants store the normalized coordinate in `agent.apps_trusted_repositories`; new repository-less grants store the name in `agent.apps_trusted_local`. Both markers are inert without the matching `agent.apps_trusted` entry. Registry and installed-app APIs expose a server-overwritten `trustRepository`; the dialog displays and echoes it as consent proof, and the grant endpoint rejects missing or stale proof for repository-backed code. `install_from_registry` compares the stored binding with the freshly resolved row before any repository-controlled bytes are fetched or executed. A bound rebind returns `app_trust_repository_mismatch`; a legacy name grant with no marker is inactive for repository-backed or unknown/fresh sources and returns `app_execution_denied`, requiring one-time re-consent even when the repository is unchanged. Only a still-installed app whose provenance is positively local retains legacy migration compatibility. The trusted-apps snapshot places inactive legacy entries in `ineffective`, revoke still tears them down, and the allow-all falling-edge sweep treats them as blanket-only. Rebind coordinates and embedded credentials never enter denial prose, error responses, or audit events. Provenance-resolution failure logs use only fixed classifications: config-derived grant names and exception text do not cross that logging boundary. Installed metadata sanitizes `source`, `sourceUrl`, and `sourceRegistry` at the write boundary, and list/detail APIs repeat that stripping for legacy records. Sanitization removes HTTP(S) userinfo completely. Username-only SSH/git+ssh userinfo and scp-style `user@host:path` remain because they are transport routing; executable and governance paths reject colon-bearing SSH userinfo because Git treats it as part of that routing username, not as a removable password. **Wire identifier:** a denial that reaches the dashboard carries the stable machine-readable `code: "app_execution_denied"` alongside its advisory `error` prose — emitted by the `openCommand` route, by `install_from_registry`, and by `AppResult.to_dict()` (which serializes `error_code`) for `enable`. The frontend keys its "allow this in Settings → Security" affordance off that code, never off the prose, so the sentence stays free to be reworded; renaming the code is a breaking UI change.

**App admission gate (`apps/admission.py`)** (CWE-829): a contained App Kit admission decision core, gating the app install / update / enable / `register_external_app` / registry paths. It is **distinct** from the CPP-seam plugin admission engine (`platform/admission.py`), which gates signed plugin entry-points from `~/.kiro/crew/admission_policy.json`; this gate governs App Kit apps from a separate `config_dir()/app_admission.json`. The fleet-controlled policy carries a kill-switch (`banned`, always wins), a marketplace `approved` allowlist (non-empty = only-these), and an optional HMAC `require_signature` check (verified against a `trust_keys` secret the *policy* — never the app — holds, over `AppManifest.signing_payload()`). `app_admission_denied()` runs **before** the app's files are copied or its `onInstall` script runs, so a denied app never lands on disk or executes. **Fail-closed** on a present-but-unreadable policy (deny-all + `critical` SEL audit); an **absent** policy admits (interim default preserving today's no-policy behavior — the seeded-default mechanism that makes absence itself fail-closed belongs to the CPP governance seam). Asymmetric signing + trusted-publisher-key distribution + a per-app capability ceiling remain follow-on.

**Federated registry validation & refresh (`apps/routes.py`, `apps/registry.py`)**: external (federated) app registries are configured under `config.registries` (`{name, repo, branch}`) and mutated via the dashboard API. The trust-boundary contract:
- **`repo` validation** — `POST /api/apps/registries` runs every entry's `repo` through `_is_safe_repo_identifier`, which admits **either** a legacy bare name (`^[A-Za-z0-9_-]+$`, kept for companion resolution) **or** a vetted full git URL. URLs must be `https://` (`_SAFE_HTTPS_URL_RE` — plaintext `http://` is rejected, see the CWE-319 threat row) or an explicit `ssh://` remote (`_SAFE_SSH_URL_RE`, userinfo optional — both `ssh://host/path` and `ssh://user@host/path` accepted; authentication is by key via ssh config) or scp-style (`_SAFE_SCP_URL_RE`, `user@` required because a userless scp form is ambiguous with local paths); shell metacharacters, `..` traversal, and `owner/repo` shorthand are rejected. When no explicit `name` is supplied, a bare name defaults to `repo` (legacy) while a URL derives a collision-safe slug via `_derive_registry_name` (host+path slug + short sha256 of the original URL) so two distinct URLs can never share an `_external_registry_cache_path` cache file. `branch` defaults to **`main`** (was `mainline`) and is validated against `^[A-Za-z0-9][A-Za-z0-9_\-./]*$` with `..` rejected.
- **Cache-key injectivity (path traversal, CWE-22/CWE-706)** — because a `repo`/registry `name` can now be a full URL, every cache path derivation (`_safe_cache_stem`, `_external_registry_cache_path`, `_blob_cache_key`) keeps pure-safe names byte-identical (existing caches stay valid) but slugifies + appends a short sha256 for any name carrying disallowed characters — so a hostile `../../config` entry can neither escape `_manifest_cache_dir()` nor collide with another name. `_expire_cache_file` additionally re-checks resolved containment before touching any file. **The blob cache is additionally keyed on provenance, not the `repo` key alone.** `_blob_cache_key(repo, clone_url)` folds the **resolved clone URL** into the digest (`sha256(repo\x00clone_url)`), because a `repo` key is not stable provenance: registry A (private) can cache a blob under key X, be removed, and registry B later be configured reusing key X — a key derived from `repo` alone would then serve A's cached (possibly private) bytes to B. `handle_blob_proxy` resolves `clone_url` **before** the cache lookup (the SAME once-resolved URL that backs the credential decision and the clone) and threads it into the key, so a repo-key reuse across registries lands in a **distinct** cache directory (a miss + a fresh clone of B's own URL) rather than a stale-provenance cross-registry read. The `ref` also becomes a path segment in the blob cache tree (`.../{repo_key}/{ref}/{file_path}`); `_SAFE_REF_RE` permits `.` and `/`, so `handle_blob_proxy` rejects any `..` segment or a leading `/` in `ref` (`if ".." in ref or ref.startswith("/")` → 400) **before** the cache path is built — mirroring the `file_path` guard — so a crafted `ref` (e.g. `../<other-repo-key>/main`) cannot stay under the cache root while crossing into a different repo's cache directory. The resolved-path containment check still guards against any escape out of the cache root.
- **Refresh endpoint — `POST /api/apps/registries/refresh`** (optional body `{"repo": "<git-url-or-name>"}` to scope to one registry; omit to refresh all). Response contract: `{ok, refreshed, failed, results, apps, lastSyncedAt}` where `ok` is True only if every matched registry refetched successfully, and `results` carries per-registry outcome so the UI distinguishes "synced" from "sync failed, serving stale". The refetch is **fetch-then-swap**: `_fetch_and_cache_external_registry` overwrites a registry's cache only on a successful fetch, and manifest caches are expired by mtime-backdating rather than unlink, so a transient forge/network failure degrades to "slightly stale" instead of "apps vanished" (stale > missing). Malformed (non-dict) index items are defensively dropped before normalization so a registry returning e.g. `["oops"]` cannot escape as an HTTP 500.
- **Clone-host trust gate (SSRF + DNS-rebinding, CWE-918)** — a configured external registry's `app-registry.json` is **untrusted content**: it can list an app whose `repo` points at an internal address (e.g. `https://127.0.0.1:8443/x`) or any attacker-chosen host, and such a value passes `_is_safe_repo_identifier` and enters the blob-proxy allowlist. Because the App Store browse/refresh path clones automatically (icons, manifests, install), honoring that host would drive `git clone` against the loopback/internal network — an authenticated backend SSRF. `is_clone_host_trusted` (`apps/registry.py`) fails **closed** and constrains every URL clone to a **host** in the trust set = well-known public forges (`_PUBLIC_GIT_HOSTS`, plus any a companion contributes) **∪** the hosts of the owner's explicitly-configured registries (`_configured_registry_hosts`). It is enforced at the **three** clone chokepoints: `_fetch_git_blob` (blob/icon proxy, `apps/routes.py`), `_fetch_app_manifest` (manifest fetch, `apps/registry.py`), and `_git_clone_or_pull` (the actual clone/pull, which returns the `untrusted_clone_host` error dict). Gating on the hostname — not its re-resolvable IP — makes it **rebinding-proof**; an owner-added internal forge stays allowed precisely because the owner added it, while an index-injected host never is. This is deliberately a **host-level SSRF/rebinding defense, not a supply-chain control** — anything on a trusted forge host (e.g. all of `github.com`) is cloneable, so signature/admission gating (the App Kit admission gate above) remains the second, orthogonal layer. Bare-name legacy repos have no URL host, return `False` here, and are served by the bundled-registry allowlist rather than a URL clone. Operator-visible failure mode: an install/browse against an untrusted host fails with `untrusted_clone_host` (clone path) or a silent skip + warning log (blob/manifest paths).
  - **Host-granular trust residual → credential-free clones (confused-deputy, CWE-441/CWE-668)** — the trust gate above is deliberately **host-granular**, so a host the owner configured for one registry (e.g. their internal forge) is trusted *wholesale*. Since a registry index is untrusted content, it can list an app whose `repo` points at a *sibling* private repo on that same trusted host; the host passes `is_clone_host_trusted`, and a clone that carried the gateway's **ambient git/ssh identity** would be a confused-deputy read of a private sibling repo surfaced back through the App Store. This applies on **two** paths: the **automatic** (browse/refresh-time) `_fetch_app_manifest` / `_fetch_git_blob` clones (no owner action at all), **and** the **install** clone of an app whose registry entry came from an owner-configured *external* index (the owner clicked Install on an index-authored *name/description*, but the `repo` URL behind that button is index-controlled, not typed by the owner). Mitigation on **both** paths: the clone runs **credential-free / anonymous** via `anonymous_git_env()` (`apps/registry.py`) **plus** a forced `mode="strict"` OS sandbox (`~/.ssh` hidden). The env drops the SSH agent + `GIT_SSH`/`GIT_SSH_COMMAND` passthrough (`_GIT_CREDENTIAL_ENV_KEYS`), disables system **and** global git config (`GIT_CONFIG_NOSYSTEM=1` + `GIT_CONFIG_GLOBAL=os.devnull`, so no HTTPS credential helper fires), and forbids prompting (`GIT_TERMINAL_PROMPT=0`, batch-mode `GIT_SSH_COMMAND` with no identity/agent) — so an index-injected private-sibling repo simply fails to clone (→ graceful fallback) instead of authenticating. **Provenance decides the install-path posture**: `install_from_registry` sets `index_originated = bool(entry.get("_registry"))` — external-index entries carry the `_registry` marker (stamped when the index is fetched/cached), so they clone credential-free; **bundled/curated** registry entries (no `_registry` marker) and fetching the owner's **own** configured registry index (`_fetch_external_registry_index`, whose URL the owner typed, not index-injected) remain owner-designated and keep full credentials via `minimal_env()`. `_git_clone_or_pull` takes an `index_originated` keyword that selects the env + sandbox mode for both its fresh-clone and fast-forward-pull branches. Accepted residual: installing (or previewing the icon/manifest of) a **private** app *listed in an external index* no longer works — the correct trade, since an index-controlled URL must not be cloned with the gateway's identity; the owner can still install a private app by configuring it as their own registry (an owner-typed URL). Trust remains host-granular by design; org/path-prefix scoping is a deferred tightening, but the credential-free rule removes the exfiltration lever it would otherwise carry.
    - **Same-repo credential carve-out** — exception to the credential-free rule above. When an index entry's **effective clone URL** (`_entry_git_url(entry)`) is **byte-identical** to the owner-configured `ExternalRegistryConfig.repo` (the URL the owner typed when adding the registry), the confused-deputy argument does not apply: the owner explicitly designated that exact URL, and a clone of it is no different from the credentialed index fetch the gateway already performs. `_is_owner_designated_repo` (`apps/registry.py`) implements this predicate — it looks up the configured registry by the entry's `_registry` name and compares with **exact string equality** (no URL normalization, no host-level matching; host-granular trust is precisely the confused-deputy hole this defense closes). When the predicate is True, all **three** clone chokepoints take the carve-out: `install_from_registry` flips `index_originated` to `False`, `_fetch_app_manifest` receives `owner_designated=True`, and `handle_blob_proxy` passes `owner_designated=True` into `_fetch_git_blob` (the App Store icon/screenshot proxy). Each path then uses `minimal_env()` + `_context_clone_sandbox_mode` (i.e. `standard` for a trusted SSH host, exposing `~/.ssh`), flipping **both** env AND sandbox together (the strict sandbox hiding `~/.ssh` is the load-bearing enforcement on machines with short-lived on-disk SSH certificates (e.g. an SSH CA agent), not the env alone — see the investigation appendix for the live refutation of env-only blocking). Sibling repos on the same host (a *different* URL from the config-stored one) remain anonymous+strict — the carve-out is URL-exact, not host-granular. On the blob chokepoint this URL-exactness is **structural, from a single resolution threaded into the clone, not a fetch-time re-resolution**: `handle_blob_proxy` resolves the clone URL **once** via `_entry_git_url(entry)` — the SAME resolver, over the SAME `entry` object, that the `_is_owner_designated_repo` decision is made against — and threads that one `git_url` into `_fetch_git_blob` for BOTH the credential grant and the clone. `_fetch_git_blob` re-resolves nothing from `repo`; the resolver `_registry_git_url` no longer exists. Because one read of one entry backs both the decision and the clone, `owner_designated` and the URL cloned describe the same value **by identity**, closing the TOCTOU window a second, independent re-read would open (a concurrent registry refresh swapping the entry backing `repo` between the decision and the clone, so a grant decided for one URL clones a private sibling). **Provenance-scoping (the entry selection, not just the entry).** `get_registry_app_by_repo(repo)` selects the entry by `repo` key **alone** (bundled first, then each external registry), so `_is_owner_designated_repo` — sound for the entry it is handed — could be handed registry A's owner-designated entry on a request reachable only through registry B when both publish the same `repo` key (a cross-registry confused-deputy read of A's private repo with A's credentials). The carve-out is therefore gated on **unambiguous single-owner provenance**: `_repo_key_owner_count(repo)` (`apps/routes.py`) counts the distinct configured sources publishing that `repo` key over the SAME union `known_registry_repos` admits (bundled once + each external registry once, local sync caches only, never fetching), and `owner_designated` is honored **only** when exactly one source owns the key; any ambiguity — or an unresolvable count (fails to `2`, treat-as-ambiguous) — downgrades to anonymous+strict and never grants. Ambiguity thus never escalates, so there is no separate refused-escalation branch to SEL-audit on this path — the only credential decision `_fetch_git_blob` makes is the surviving GRANT, which is SEL-audited (`_sel_credential_grant("app_blob_proxy", …)`) against the threaded `git_url` actually cloned. **The grant is also scoped to the entry's CONFIGURED branch, not an attacker-chosen `ref`.** The blob `ref` falls back to the entry's `branch` only when the query param is empty; a caller can otherwise supply any `_SAFE_REF_RE`-valid `ref` (e.g. `iconPath=logo.png&ref=private`), and deciding `owner_designated` on the entry alone would drive an owner-credentialed clone of an **unconfigured** (e.g. private) branch of the owner's repo and serve its image bytes. `handle_blob_proxy` therefore requires the effective `ref` to equal `entry.get("branch", "main")` **before** honoring `owner_designated` (the `_repo_key_owner_count` / `_is_owner_designated_repo` checks are reached only inside that branch-equality gate); a differing `ref` is **not** rejected — the anonymous+strict path still serves a public branch — it simply never attaches credentials. So credentials attach only when the resolved clone URL is byte-identical to the entry's own single-owner registry URL **and** the effective `ref` equals the entry's configured branch. **The blob path's bundled-entry posture is a deliberate conservative asymmetry, not an oversight to "fix" for parity**: a bundled entry carries no `_registry` marker, so `_is_owner_designated_repo` returns False and the blob clone stays anonymous+strict — unlike the install path, which treats a bundled/curated entry as owner-designated. Widening the blob path to match would extend a credentialed clone to the browse-time icon proxy, which runs automatically during App Store browsing with no owner action; the narrower blob posture is intentional. The practical effect: private-forge registries using the monorepo `apps/*` layout (all apps inside the registry repo itself) become fully functional — manifest fetches, installs, AND the store's icon/screenshot rendering all succeed with the owner's credentials, instead of the store listing apps correctly but degrading their icons to a blank/gradient fallback. Pinned by `TestSameRepoCredentialCarveOut` in `test/test_external_registry.py`; the blob-chokepoint posture is pinned by `TestFetchGitBlobCredentialPosture` and `TestBlobProxyOwnerDesignatedWiring` in `test/test_apps_routes_coverage.py`.
    - **Origin-mismatch move-aside + aged sweep (data-loss prevention)** — when `_git_clone_or_pull` detects an origin mismatch (`_clone_origin_matches` returns False), the stale checkout is **moved aside** (atomic same-filesystem rename to a `.stale-<uuid>` sibling inside `app-sources/`) before the fresh clone, NOT deleted. On clone **success**: the moved-aside directory is **retained** (not deleted) so the user can recover local edits; a log line names the retained path. Aged `.stale-*`/`.partial-*` directories are swept by `_sweep_stale_checkouts()` (best-effort, runs at the start of the next `install_from_registry` call) after `_STALE_CHECKOUT_RETENTION_DAYS` (7 days); the sweep targets only immediate children of `app-sources/` matching the fixed naming pattern, containment-checked via symlink resolution against the app-sources root. On clone **failure or timeout**: the moved-aside directory is **restored** to `dest` so the previous checkout survives — no local changes are permanently lost by a transient network/forge failure. If the move-aside rename itself fails (locked files on Windows), the function returns `stale_clone_not_removed` without attempting a clone — **fail-closed** preserved. The mismatched clone is never built from or pulled from under any branch of this flow. The moved-aside path stays inside the `app-sources/` root (uses `dest.with_name(...)`, never escapes the parent). Pinned by `TestOriginMismatchDeleteOrder` in `test/test_external_registry.py`.
- **Untrusted index entry-name filter (path traversal, CWE-22)** — an external registry index is untrusted input, so a hostile/typo entry `name` such as `/tmp/victim` or `../../victim` would otherwise flow through `list_registry → install_from_registry → app_source_dir(name)` (which resolves `_app_sources_dir() / name`, and an absolute or traversing name escapes the app-sources root) and, on a failed clone, reach `shutil.rmtree(dest)` on the attacker-selected path. During index normalization (`apps/registry.py`) every entry name is validated against `KEBAB_RE` (`^[a-z0-9]+(?:-[a-z0-9]+)*$`, the same kebab-case gate `install`/`register_external_app` already enforce via `AppManifest`); a non-string or non-kebab name is **dropped BEFORE it is cached or listed** so it can never reach a filesystem operation. Operator-visible failure mode: the offending entry silently vanishes from the App Store and the drop is **warning-logged only** (`Dropping external registry <reg> entry with invalid name ...`) — no install error surfaces, so an operator diagnosing a "missing app" must consult the gateway log.
- **Same-repo branch override (`_apply_configured_branch`, `apps/registry.py`)** — a same-repo index entry's declared `branch` is index-controlled (untrusted) content and is **overridden by the operator-configured registry branch**, at fetch finalisation (with a divergence warning) and on every cache read that feeds a clone coordinate (listing, `install_from_registry` lookups, provenance candidates, blob-proxy branch resolution — so a cache written before a branch-config change cannot keep an overridden value alive). The index was cloned from exactly the configured branch, so a divergent same-repo declaration names a state that does not exist on the ref the operator asked for, and the override narrows what an index can make the installer clone (the configured value already passed the branch regex gate before the fetch). Cross-repo entries — effective clone URL differing byte-identically from the configured `repo`, the same comparison semantics as the same-repo credential carve-out — keep their declaration, since it names a ref in another repository about which the configured branch carries no information.
- **Untrusted index `subdirectory` filter (path traversal → RCE, CWE-22)** — an external index controls the entire entry, including `subdirectory`, which is joined to the throwaway manifest clone dir (`_fetch_app_manifest`), the persistent app-source dir, and the install-time app-root (`install_from_registry`). An absolute (`/etc`) or traversing (`../../victim`) value would escape those roots and let an attacker-selected `app.json` be read and its `setup.onInstall` executed with gateway privileges. Two layers close it: (1) a **lexical gate** `_is_safe_registry_subdir` (`apps/registry.py`) — rejects non-strings, NUL, backslashes, absolute paths (POSIX/drive-letter), and any `.`/`..` segment — applied during index normalization **and** on every cache read (`_read_external_registry_cache`), so an unsafe entry is **dropped BEFORE it is cached, listed, or installed** (warning-logged only, same silent-vanish failure mode as the name filter); and (2) `_contained_join(root, subdirectory)` at each use site, which resolves symlinks and returns the joined path only if it stays within `root` — catching a hostile clone that ships a symlink (`sub -> /etc`) resolving outside the clone root at read/install time (install refuses with an explicit `unsafe subdirectory ... escapes the app source root` error; manifest fetch returns `None`).
- **Trust-grant audit (`registries.host_trust_granted` SEL event + `newlyTrustedHosts`)** — admitting a new registry host is a genuine **trust grant** (its hosts feed the clone-trust set above and its apps become installable with gateway privileges), not a mere config edit, and the generic `registries.update` event does not record *which* host gained trust — leaving an unreconstructable, one-way-door audit gap. The `PUT /api/apps/registries` handler (`apps/routes.py`) diffs the incoming hosts against the **prior on-disk** config and emits a distinct per-host SEL `registries.host_trust_granted` (`resources=host=<h> repo=<url>`) for each genuinely new host; re-saving an unchanged list — or adding a second path on an already-trusted host — emits nothing, so the audit log records exactly the trust transitions. The response returns `newlyTrustedHosts` so a client can surface the grant (e.g. a UI heads-up) without another round-trip.

**Response security headers** (`server.py:_apply_security_headers`):
- All dashboard responses receive `Cache-Control: no-store`, `Content-Security-Policy` (default-src 'self' plus curated exceptions for tailwind/jsdelivr/esm.sh, `fonts.googleapis.com` in `style-src` + `fonts.gstatic.com` in `font-src` for the dashboard's two brand webfonts, and WebSocket loopback), and `Permissions-Policy: clipboard-write=(self), clipboard-read=(self)`
- The Permissions-Policy grant is required by Chrome 143+, which changed the default policy to DENY `clipboard-write` even on secure contexts (crbug.com/414348233). Without it, `navigator.clipboard.writeText` throws a permissions-policy violation and the Copy-link button on published artifacts fails
- **`/vendor/*` CORS + Private-Network-Access grant** — sandboxed widget/artifact iframes are null-origin (srcdoc/blob) documents, i.e. NON-secure contexts, and on the default deployment the gateway is plain http on loopback, a "more-private address space" under Chrome's Private Network Access policy — so Chrome blocks the iframe's `<script src>` for the vendored Tailwind runtime unless the load goes through CORS with server approval. The live fix (issue #6181, verified against real Chromium): `widgetSrcdoc.ts` emits the runtime `<script>` with `crossorigin="anonymous"` and `_apply_security_headers` adds `Access-Control-Allow-Origin: *` to `/vendor/*` responses only (never any other path — the dashboard's own pages and APIs must not become cross-origin readable). `crossorigin` makes the header MANDATORY, not additive: without it the load hard-fails at the CORS layer, so every origin serving this URL must send it — the gateway is covered here, and in `npm run dev` the runtimes are served by `vite.config.ts`'s `vendorRuntimePlugin` dev middleware, which stamps the same header on exactly those files (Vite ≥6.2's own `server.cors` default is a localhost-origin allowlist that a sandboxed iframe's `Origin: null` does not match, so the upstream default cannot supply it). A dedicated `OPTIONS /vendor/{tail}` route (registered inside the same `vendor`-dir guard as the static mount) additionally answers Chrome's PNA preflight with `Access-Control-Allow-Private-Network: true` (echoed only when the request asks; `add_static` registers GET/HEAD only, so a preflight would otherwise 405 and fail closed) — forward-compat: current Chromium blocks this insecure-initiator load at the CORS layer without ever sending the preflight. `*` leaks nothing: `/vendor/` holds only public, non-secret static JS, already auth-exempt via `token_auth._BYPASS_PREFIXES`
- `frame-src` always admits loopback preview origins — http+https on `127.0.0.1`, `localhost`, `[::1]`, `0.0.0.0` (`_LOOPBACK_FRAME_SRC`) — so the chat side-panel **Web Preview** tab (`WebPreviewPanel`) can frame a local dev/static server in the packaged app, not only when the instances feature is enabled. The framed preview cannot read the dashboard's host-scoped session cookie: `WebPreviewPanel.isolatePreviewHost` rewrites a preview whose host equals the dashboard's (both loopback, incl. `*.localhost`) to a distinct loopback alias, so no `mc_token_<port>` cookie is ever sent to the previewed server. When the instances feature is additionally enabled, `frame-src` is extended with the `http://*.localhost:*` tunnel wildcard so dynamically-connected tunnel ports can be framed
- Defense-in-depth framing/sniffing/referrer/transport headers are set uniformly (all via `setdefault`): CSP `frame-ancestors` (clickjacking) — `'self'` by default plus any **exact operator-trusted origins** (never a wildcard, never a hardcoded port); `X-Frame-Options: SAMEORIGIN` as a legacy backstop set **only** in the default `'self'`-only posture (omitted when an extra ancestor is trusted, since it is origin-exact and cannot express the allowlist); `X-Content-Type-Options: nosniff` (MIME confusion), `Referrer-Policy: strict-origin-when-cross-origin` (avoids leaking the token-bearing dashboard URL cross-origin), and `Strict-Transport-Security: max-age=31536000; includeSubDomains` (inert over the loopback HTTP bind, protects HTTPS tunnel/desktop access). Cross-port embedding of remote dashboards in the Instances viewport is enabled **only** via exact trusted-origin embedding carried in the signed token: at connect the local (embedding) gateway mints the remote token with an `embed_parent_port` claim equal to its own `KIROCREW_PORT`; that claim is carried through the link→session token exchange into the `mc_token_<port>` session cookie (`token_auth_middleware` — the exchange re-mints a fresh session token and must propagate the claim), and the middleware also stashes the validated port on the request **before** it revokes the link nonce. The embedded remote's `_extra_frame_ancestors` reads it in that order — the request-stashed value first (so the FIRST `?token=` framed document, whose link nonce the exchange revokes, still carries the origin), then the query token, then the session cookie (`token_embed_parent_port`) — and adds the parent's loopback origins (all loopback hosts at that port) to `frame-ancestors`. Exact origins only — never a wildcard, never a hardcoded port — and gated on a signed token, so a local page with no token can never inject an ancestor. Neither a loopback-wildcard nor the CSRF `allowed_origins` set is used for framing: both would let a local origin (any port, or a CORS/`dashboard.url`/dev `localhost:3000` entry) frame the authenticated dashboard and receive the `SameSite=Lax` session cookie (clickjacking, per input-validation guidance). Any request without such a token keeps the default `frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN` posture
- Applied via `no_cache_middleware` using `setdefault` so per-handler overrides are preserved

**CSRF protection** (`server.py` + `origin.py`):
- Validates `Origin` (with `Referer` fallback) on POST/PUT/DELETE
- Allowed origins are seeded via `build_allowed_origins()` at startup: `127.0.0.1:{port}`, `localhost:{port}`, `kirocrew.localhost:{port}`, plus configured host and machine hostname when not local-only, plus `localhost:3000` in dev mode. An explicitly enabled but initially unresolved Tailnet origin retries in a non-blocking background task (2-second exponential backoff, capped at 60 seconds); after re-reading the opt-in, re-checking the governance ceiling, and validating the daemon's MagicDNS name, it adds exactly that HTTPS origin to the same live set. The aiohttp app mapping remains frozen; only the pre-created set and runtime state value are mutated on the event loop
- Shared `check_origin()` function used by both CSRF middleware and WebSocket origin check — single source of truth
- Both entrypoints (`start_dashboard` and the headless `start_api_server`) build the barrier from the shared `_make_csrf_middleware` factory, so — exactly as for the Host barrier — the exemption set below is a single decision that cannot be granted on one server and withheld on the other
- **Self-authenticating-webhook exemption (`token_auth.CSRF_EXEMPT_EXACT_METHODS`)**: exactly two paths skip the Origin check, each for POST only — `POST /api/messaging/teams` (`TEAMS_WEBHOOK_PATH`) and `POST /api/hooks/agent` (`AGENT_HOOK_PATH`). Both callers are server-to-server, send neither `Origin` nor `Referer`, and `check_origin` accepts a header-less request only from a loopback peer or the unix socket — so without the exemption each route answers 403 before its handler runs whenever the caller reaches the gateway directly (the Bot Framework Connector against a public hostname on a VM/App Service; a CI runner or review bot posting a hook from off-host), with no setting that widens it. Compensating control: neither handler reads a cookie, so the browser-with-auto-attached-cookies threat CSRF exists for does not apply, and each authenticates its own credential — the Bot Framework JWT (issuer, App-ID audience, RS256 signature over the Bot Framework JWKS, expiry) for Teams, and the webhook bearer token for the hook, where `_verify_hook_token` compares against the sha256 of every stored entry with `hmac.compare_digest` and stays the **sole** gate (401 when none match, including on a fresh install with no token at all). The two credentials are not equally strong and the code says so: the JWT is Microsoft-signed and unforgeable by anyone else, while the hook token is locally generated and user-managed, so its strength is the operator's handling of whichever runner holds it. What the exemption changes is only *reachability* — a leaked hook token was already sufficient from a loopback or proxied peer. Both routes throttle failed auth per source (`webhooks.auth_throttle`), the Teams route additionally caps the body at `TEAMS_MAX_ACTIVITY_BYTES` before delegating, and every hook 401 is recorded in the run history. The map is method-scoped for the same reason the token-auth bypass is, and on the hook path that scope closes a live collision rather than a hypothetical one: the literal `agent` also matches the `{hook_id}` wildcard of the dashboard-authed PUT/DELETE `/api/hooks/{hook_id}` CRUD routes. Any **third** entry is a security review; `test_teams_webhook_hardening.py` pins the whole map and drives the real middleware for both directions on both paths

**Host-header validation (DNS-rebinding defense)** (`server.py` + `origin.py`):
- `host_validation_middleware` (`server.py`) rejects any request whose `Host` header does not name a host the dashboard serves. Both entrypoints (`start_dashboard` and the headless `start_api_server`) build it from the shared `_make_host_validation_middleware` factory — a single exemption point that cannot drift between the two chains. It is registered right after `deny_audit_middleware` and `host_canonical_redirect`, and before `no_cache_middleware`/`csrf_middleware`/token auth
- Runs on **every** HTTP method (not just mutating ones): a GET-based data exfiltration is the rebinding payload, and it is **independent** of the CSRF Origin check and loopback trust — a rebound request is loopback at the socket but forges `Host`
- **Probe exemption (`origin.PROBE_PATHS`)**: `/api/health`, `/api/live`, `/api/ready` bypass the barrier — orchestrator probes (kubelet, Docker HEALTHCHECK, LBs) address the gateway by container/pod IP, which is never in the host allowlist. Compensating control: `_liveness_payload` gates the build-identity fields on `check_host` AND `is_direct_local_request`, so a rebound request learns only `{"ok": true}` — indistinguishable from a bare TCP connect succeeding. The exemption set is frozen and any addition to `PROBE_PATHS` is a security review; regression tests drive disallowed-Host probes through a real middleware chain (`test_api_health.py`)
- `check_host()` (`origin.py`) compares the `Host` header (port-stripped, lower-cased) against `build_allowed_hosts()` (`origin.py`), which derives the host allowlist from the SAME `allowed_origins` set the CSRF check uses (so the two layers never drift) plus the canonical loopback names as a floor. Comparison is **port-independent** (hostname only), so an SSH-tunnel local port still matches
- **Deny-by-default**: a missing/empty `allowed_origins` is treated as a denial (never fail-open); a missing/empty `Host` is allowed **only** from a loopback `request.remote` (local IPC clients like mcp-core/doctor that omit `Host`), positively confirmed rather than blanket-allowed
- Rejects unknown Hosts with `403 Host header not allowed` + a `log_api_access` SEL event (`outcome="denied"`)

**Deny-before-audit boundary** (`server.py`):
- `sel_audit_middleware` is registered INNER to the Host, CSRF and token barriers, so a refusal one of them raises is a 403 that middleware never observes. The three known sites each call the shared `_audit_denied` helper (off the event loop, best-effort), but that is a convention a fourth site can omit — and the omission is invisible in production, since the refusal simply appears in no log
- `deny_audit_middleware` (`_make_deny_audit_middleware`, shared factory, installed on BOTH entrypoints outer to every barrier) makes the recording positional instead: it catches a raised 401/403 on the way out and audits it through the same helper unless an inner layer already claimed the request. A future deny site that forgets everything is still recorded; what forgetting costs is the record's reason DETAIL, not the record
- The audit surface widens by exactly ONE record class, and it is the class this control exists for. A layer CLAIMS a request (`origin.AUDIT_CLAIMED_KEY`, set through `origin.mark_audit_claimed`) exactly when it wrote the specific record itself: the two barriers via `_audit_denied`, `sel_audit_middleware` for the mutating `/api/` requests it actually logs (so its `outcome="error"` entry for a handler's 403 is not doubled), and the two WebSocket-origin handlers that log their own denial (`stt_stream.py`, `handlers/terminal.py`). The claim marker lives in `origin.py` rather than `server.py` because those handlers cannot import `server` without a cycle. `token_auth_middleware` RETURNS its 401/403 rather than raising and audits each with a specific reason code, and returned responses are not inspected here. Only 401/403 count as refusals — a 302 from host canonicalization and a 404 from routing pass through untouched. The one refusal that reaches the boundary unclaimed is `ws.py`'s cross-origin WebSocket 403 (`_check_ws_origin`), which audited nothing of its own and so was previously recorded nowhere. `test_api_health.py` drives these properties through a real middleware chain, including a synthetic barrier that audits nothing, and walks every `raise web.HTTPForbidden`/`HTTPUnauthorized` in the tree asserting that each self-auditing site claims

**WebSocket origin validation** (`ws.py` + `origin.py`):
- `_check_ws_origin()` calls shared `check_origin(require=True)` before `ws.prepare()`
- Reads `app["allowed_origins"]` (same set as CSRF middleware)
- Rejects missing Origin (non-browser clients) and cross-origin requests
- **Same-origin loopback fallback**: when an `Origin` is not in the
  allowed set, it is still accepted if its host is loopback **and** it exactly
  equals the request `Host` header — a genuine same-origin request. This covers
  the multi-instance embedded iframe, which is served at `<host>:<tunnelPort>`
  and opens its WebSocket to that same `location.host` (so `Origin == Host`),
  without reopening SEC-016: an arbitrary-port local page's `Origin` differs
  from the gateway `Host`, and browsers forbid scripts from forging either
  header. Non-loopback `Origin == Host` is **not** auto-trusted (still allowlist-only).

### Slack Owner Authorization

**Deny-by-default owner lock**:
- `_init_socket_mode()` refuses to connect if `KIROCREW_OWNER_ID` is unset/empty
- `_on_event()` rejects all messages when owner ID is missing (secondary guard)

**Interactive button verification** (5 defense-in-depth layers):
1. Owner check in `_handle_interactive()` — deny-by-default (rejects unless positively confirmed)
2. Owner check in `handle_interaction()` — handler defense-in-depth
3. `conversations.info` DM gate for Trust/YOLO actions
4. Trust/YOLO buttons suppressed in group channels
5. `disable_yolo()` + `yolo off` keyword to reverse YOLO

Non-owners receive ephemeral message: "⛔ Only the KiroCrew owner can use these buttons."

**Safety override (YOLO) — time-limited with re-authorization** (`safety_override.py`):

Permanent YOLO mode has been eliminated. All activations go through the `SafetyOverride` singleton, which enforces a single ad-hoc duration shared by every surface (`agent.yolo_duration`, default 6 hours, hard ceiling 24 hours). Per-surface TTLs (Slack 30 min / dashboard 6 h / config 24 h) were removed: the same operator re-enabling the same grant got a different lifetime depending on where they clicked, which was unpredictable without buying any security. The declared `dangerouslySkipPermissions` grant and the `until_shutdown` ad-hoc duration are governed separately (see `safety_override.py`).

After expiry, re-authorization is required. A 5-minute grace window allows `!yolo renew` (Slack) or the dashboard re-auth button to extend the session without creating a new one. Outside the grace window, a fresh activation is needed.

**The grant is process-global; approval modes are per-slot.** `POST /api/chat/mode` sets `normal` / `trust_reads` / `trust` against the slot named in `slot` (or every slot when it is omitted), while `yolo` is the global grant and ignores `slot`. Because the grant covers every slot, a **slot-scoped** `trust`/`trust_reads` does NOT revoke it: that request asks for auto-approval on one slot and cannot be answered by withdrawing authority from slots it never named (the shape that let a programmatic per-slot `trust` end an operator's live grant). Every other mode change still revokes, so `normal` remains the off-switch at any scope. A grant DECLARED in owner-only config is exempt from the narrowing — it has no TTL, and selecting another approval mode is the one action documented to end it. That exemption keys on the grant's **source** (`SafetyOverride.is_declared`), never its permanence: an `until_shutdown` ad-hoc pick is equally permanent and keeps the scope protection, while a declared grant the governance ceiling refused to make permanent is timed and still counts as declared.

SEL audit events are emitted on every lifecycle transition:
- `safety_override:activate` — override enabled
- `safety_override:renew` — session extended within grace window
- `safety_override:expired` — TTL reached, auto-deactivated
- `safety_override:deactivate` — manually disabled; emitted for every explicit deactivation against a grant that exists in any form, including one whose TTL already lapsed (`resources` records the pre-call state: `was_active`, `was_permanent`, `remaining`, `prior_source`). Only a never-activated instance stays silent.

Transitions that create or extend auto-approval authority (`activate`,
`activate_scoped`, `renew`) are audited fail-closed: the SEL event is written
with `critical=True` before the state commits, and a failed write refuses the
grant or extension (`renew` returns `reason: audit_failed` with the deadline
unmoved). Because the SEL write runs outside the state lock, `renew` re-verifies
under the re-acquired lock before committing: a grant deactivated during the
audit window is not resurrected, a fresh activation that landed in that
window keeps its own deadline instead of being overwritten by the stale
renewal, and a renewal that began on a live grant refuses to commit through
the grace window (so a grant that lapsed or was switched off mid-audit stays
off).

Fleet governance endpoints:
- `/api/status` now reports `yolo_active` (bool) and `yolo_expires_at` (ISO 8601) fields
- `/api/admin/compliance/yolo-status` provides full override status (source, remaining time, activation count, renewal history)

Expiry notifications are delivered via Dashboard WebSocket and Slack DM to inform the user before and at override expiration. The Slack expiry DM flows through a shared redacting `_dm_owner` exit point (`dashboard/server.py`): text passes `redact_exfiltration_urls()` then `redact_credentials()` before `post_message`, so any future caller forwarding LLM/user-derived content cannot leak credentials or exfil URLs.

**Challenge-and-redirect for Slack direct requests** — **REMOVED**
(`slack/events.py`, `slack/allowlist.py`):

> The redirect flow intercepted every inbound Slack message and turned it into
> a presigned dashboard-session link (deny-by-default), an enterprise-internal-only
> posture. It has been removed for external/open-source usage: Slack messages
> are processed **inline** and reach the agent directly, gated by the user
> allowlist and the Enterprise Grid origin check. `send_channel_challenge()`
> and the `_CHALLENGE_REDIRECT_ENABLED` gate no longer exist; do not restore
> them on an upstream sync.

**3-tier interactive trust escalation** (`dashboard/chat_runner.py`, `dashboard/chat_handlers.py`):

When the dashboard presents a tool approval prompt, users can now choose from three trust levels:

| Action | Scope | What it trusts |
|--------|-------|---------------|
| `trust_command` | Session-scoped | Exact command/tool (e.g., `ls /tmp`) |
| `trust_base` | Session-scoped | Base command glob (e.g., `ls *` — trusts `ls` with any arguments) |
| `yolo` | Global | All tools across all slots (existing behavior, now time-limited) |

Trust patterns are stored per-slot as session-scoped fnmatch globs
(`slot._trusted_patterns`). For shell tools, both halves of the decision use the
ACTUAL command from `tool_input`: the runner derives the pending grant scope
from it, and later matching evaluates the next call against it. For non-shell
tools with no structured input, both halves use the server/tool pair recovered
by `toolCallId` from the preceding ACP `tool_call` frame's `_meta.kiro` cache.
The UI retains the ACP-compatible `mcp__<server>__<tool>` display spelling, but
durable trust uses a separate versioned key whose independently lowercased
UTF-8 components are hex encoded. This makes the identity injective even when a
server or tool contains `__`; the wire/display spelling is never authorization
authority. Structured params remain attached to a repeated permission event;
their presence disables canonical non-shell grantability and matching, so a
same-`toolCallId` re-prompt cannot turn an argument-bearing call into an inputless
one. A missing server/tool identity or a pre-upgrade pending card without the
internal key fails closed for durable trust while ordinary Allow once and Reject
remain available. Existing broad `*` trust retains its established semantics;
legacy ambiguous exact MCP display patterns do not match the new internal keys.

The `pattern` submitted by the dashboard is a consent proof, not authority: it
must equal the server-derived field on the still-pending approval. Missing,
underivable, redaction-changing, or stale/mismatched patterns return a typed 400
without resolving the approval and are SEL-audited. Exact-command grants escape
fnmatch metacharacters before storage, so trusting the literal command
`rm *.tmp` does not also trust `rm secret.tmp`. Base grants are also derived
server-side; assignment-prefixed bases such as `FOO=bar` are refused rather than
becoming broad globs. Command parsing and matching live in the shared
`trust_patterns.py` module so another approval surface consumes a command-shaped
API instead of importing dashboard runner internals or fabricating a
`Running: ...` title.

**Child-fidelity split: identity vs arguments.** A backend-subagent permission
event whose structured params never reached the tool_call cache is low-fidelity
(`AcpEvent.child_low_fidelity`) and is excluded from every content-matching
auto-approve path — trusted patterns, trust-reads, title-keyed
`auto_approve_tools` — because the agent-authored title/params ARE the matched
input. A remote (HTTP) MCP server legitimately streams empty `rawInput` on its
`tool_call` frames, so every such child call is low-fidelity; but the same
frame's `_meta.kiro` server/tool identity is cache-provenance and
non-model-authored. `AcpEvent.child_mcp_identity_trusted` isolates that half
(requires: child origin, RESOLVED non-shell classification, canonical
server+tool recovered from cache, AND the explicit `mcp_identity_trusted`
provenance flag — set only when the trusted population actually happened: in
`build_permission_event` it is derived from the origin-scoped cache reads
HITTING (never from cache availability or field non-emptiness — a hit whose
cached value is `""` still earns it), the `_meta.kiro` tool_call builders set
it only when their extractors actually produced an identity pair (a frame
without `_meta.kiro` populates nothing and asserts no provenance), and
`_to_llm_event` copies it; it mirrors
`raw_params_trusted`, so a future inline population path fails closed instead
of counting as verified on non-emptiness alone). The grant-eligibility
expression is hoisted to one place,
`AcpEvent.child_unconditional_grant_eligible`
(`not child_low_fidelity or child_mcp_identity_trusted`), consumed by all
three approval surfaces (dashboard runner, Slack gateway, subagent manager):
**unconditional** grants — session trust-all, global YOLO,
`parent_policy=auto`, per-source auto-approve, the `--approval yolo` override —
honor the grant for eligible events: the approve decision consumes no
agent-authored event data, only the arguments remain unverified (the same
blindness the interactive card has; the identity split changes WHO approves,
not what any gate can scan). Shell events never qualify: their deny gates need
the command bytes the event lacks.

### SEL Audit Logging (`sel.py`)

See `docs/system-specs/modules/sel.md` for full spec. Every event carries a `source` stamped by `_infer_source`; that function's return vocabulary IS the set of audited surfaces and is published via `sel.audit_sources()` (consumed by the security-posture view, so the count is derived rather than restated here).

**What counts as an auditable permission decision.** A SEL event is emitted when a decision has a *subject* — a tool/capability that was granted or denied. The audit records grants and denies, not the absence of any decision:

- **Skill triggering** (`skills.py:get_triggered_skills`, runs per message) emits **one** event per call when at least one skill was injected (`outcome="triggered"`, grant) or actively excluded by a negative trigger that would otherwise have matched (`outcome="denied"`, with the excluded skills in `metadata.negated`). When no skill matched and none was negated — the overwhelmingly common case — **no event is emitted**: nothing was granted or injected into LLM context, so there is no permission decision with a subject to record (analogous to not auditing an authz check that had nothing to authorize). This is a deliberate, threat-model-reviewed choice: the prior per-skill "not_triggered" logging was a per-message synchronous-write hot-path cost, and a per-message "matched nothing" event would dwarf the real grant/deny signals and *reduce* the audit trail's usefulness rather than improve it. The message text is already captured in conversation history; skill names are not secret.

### Security Posture Detail Registry (`security_posture.py`)

The Settings → Security "Live Security Posture" card renders from
`GET /api/security/posture`, whose payload is built by
`security_posture.build_posture_snapshot()`. It exists to fix a class of bug, not
just to add a view: the panel previously rendered **hardcoded counts** that had
silently drifted several-fold from reality — every one of `13` sensitive paths,
`42` suspicious patterns, `5` redaction paths, and `12` tool schemas was wrong —
and a reader had no way to see what any count covered.

**Do not write the current values into this doc.** Restating them here is how the
original bug propagated (the dashboard's hardcoded `5` was transcribed from a doc
sentence), and a literal added here goes stale the moment a control grows — as one
did while this very section was being written. Read the live counts from
`GET /api/security/posture`, or from `security_posture.posture_counts()`.

**Derivation invariant.** Every control's `count` is `len(items)` — the pill and the
expanded list can never disagree — and `items` are produced by an `items_fn`
callable resolved **per request** against the live control.
`api_security_stats` re-sources its counts from this registry, so there is exactly
one place a count is computed. Controls split into two classes:

- **Derived (7):** items come straight from the enforcing object —
  `security.sensitive_home_dirs()`, `write_protected_home_paths()`,
  `BUILTIN_DENIED_RULES`, `SUSPICIOUS_BASH_PATTERNS`, the MCP dispatch registries
  (`MCP_CORE_SCHEMAS` / `MCP_CRON_SCHEMAS`, **not** the `*_SCHEMA` naming
  convention — several registered tools are inline/shared schemas with no
  module-level name), `exfil_query_min_len()`, and `sel.audit_sources()`. For these
  drift is structurally impossible.
- **Curated (3):** `_REDACTION_SINKS`, `_CREDENTIAL_FAMILIES`, `_EXFIL_HEURISTICS`
  have no single live list to enumerate (a sink is a *call site*, a family is a
  *regex alternative*, a heuristic is a *branch*). A `len()` of a hand-written
  tuple would merely relocate the original stale-number bug into this module, so
  each is paired with an **omission-detecting** test in
  `test_security_posture.TestOmissionDetection`:
  - the redaction registry is checked against **every** redactor call site in the
    package — each module must be a registered sink or an explicitly-reasoned
    entry in `NON_EGRESS_REDACTION_MODULES` (with a companion test rejecting stale
    allowlist entries), so a new output path cannot be added without classifying it.
    The detector regex must stay broad enough to see **every wrapper**
    (`redact_and_truncate`, `redact_via_context`, qualified `security.redact(...)`,
    `StreamRedactor`), because a form missing from it is the same omission hole one
    level up — a narrow earlier version silently skipped the `redact_and_truncate`
    Slack egress in `dashboard/chat_slack.py` and `slack/blocks.py`. Prefer
    over-matching: an extra module is classified once, whereas a missed one is
    invisible forever;
  - each advertised credential family must have a synthetic sample that
    `redact_credentials()` actually fires on, and the family list and sample table
    must match exactly (so a new regex alternative without a row fails);
  - each advertised exfil heuristic must have a URL that `scan_exfiltration_urls()`
    actually flags, with the same exact-match requirement.

  An omission is the failure mode that shipped "5 output paths" against several times that many.
  Only a test that detects an omission catches it; a `len()` assertion never will.

**Disclosure contract (posture-only, mirroring the governance viewer).** The
payload carries public control *definitions* and derived counts only:

- **Included**: blocked path patterns (the blocklist is already public in
  `docs/architecture/security-deep-dive.md`; knowing `~/.aws` is blocked does not help reach
  it), redaction-sink module names, credential **family** names, heuristic
  descriptions, audited surface names, deny-rule *descriptions*.
- **Excluded**: credential material, governance policy/profile **rule contents**
  (the ceiling the agent is fenced from — this endpoint must not become a side
  channel around the governance viewer's counts-only rule), user data, and the raw
  deny **regexes** (those keep their own opt-out surface in Card A's chevron).
- A pinned test asserts the entire JSON payload passes **both** redaction passes
  (`redact_credentials()` **and** `redact_exfiltration_urls()`, plus the dual-pass
  `redact()`) **unchanged** — so a description written with a live credential or
  long-query-URL shape in it (e.g. a literal bearer-header example) fails CI
  rather than shipping a row that renders as `[REDACTED: …]` wherever the payload
  is itself scanned (the SEL audit log, a Slack-relayed summary). A companion test
  proves the guard is non-vacuous.
- The governance boundary is pinned on **provenance, not key names**: a test
  asserts this module's source contains no reference to the governance machinery
  (`platform.governance`, `governance_profiles`, `resolve_active_scope`,
  `current_context`, `security_policy`/`admission_policy`). A name-only guard
  ("no control key contains `policy`") is trivially bypassed — a control keyed
  `ceiling_scopes` could republish literal policy deny globs and still pass it.
  If the module cannot *reach* governance, it cannot leak it under any key name.

**Honest per-sink coverage.** Most redaction sinks run both scanners; a few run
only one (`task_reporter.py` is exfil-URL-only; `sel.py`'s on-disk writer signs
bytes as-written, so its callers redact before `log`). Those rows say so in their
own detail text, and a test asserts a partially-covered sink cannot be described
without that disclosure. Likewise, the `suspicious_patterns` row states it is
**advisory** (surfaced by the `kirocrew` history scan via `audit_bash_command`),
not enforced at the PreToolUse gate — the gate uses the narrower
`audit_bash_exfiltration` plus the denied-command rules.

**`/api/security/stats` is retained but no longer used by the dashboard**, which
reads `/api/security/posture` (same counts plus the items). It re-sources via
`posture_counts_async`, which resolves every `items_fn` — so the count is still
`len(items)` — without materializing or serializing the ~45 KB item payload to
return three integers. A test pins the two paths to identical values so they
cannot become a second, divergent count source.

**Executor choice.** `build_posture_snapshot_async` / `posture_counts_async` offload to the dedicated
`governance_executor` (`mc-gov`), NOT the shared default pool — the same choice
`build_governance_policy_snapshot_async` makes, for the same reason: this GET is
browser-triggerable, so once a control does filesystem I/O (the case the
per-request `items_fn` design exists to keep safe) default-pool I/O would contend
with the workers the event loop shares for DNS.

**Failure isolation.** A control whose `items_fn` raises degrades to
`{"count": null, "unavailable": true, "items": []}` and the remaining controls
still render. The frontend shows an explicit `unavailable` badge — never `0`,
which would tell an operator that a live control covers nothing.

**Denied-commands count is the one runtime-variable pill.** The registry reports
the **shipped** built-in rule table (137); the panel overrides that row's pill with
the **effective** count from `GET /api/security/denied-commands` (after user
opt-outs and governance pins), because that is what is actually enforced.

**Public accessors.** `security.py` exposes `sensitive_home_dirs()`,
`write_protected_home_paths()`, `crew_home_prefixes()`, and
`exfil_query_min_len()` (returning tuples/ints, so a caller cannot mutate a live
blocklist) — the same decoupling rationale as `get_credential_patterns()`: a
future rename of the private name cannot silently turn the posture view into a
lie. `security_posture.py` is a leaf module; the two `token_auth` TTL constants are
imported function-locally to avoid the `kiro_crew.dashboard` package cycle.

### Frontend Security

- **No `dangerouslySetInnerHTML` with unsanitized content** — all HTML content sanitized via DOMPurify
- **Safe DOM APIs** — `createElement` + `textContent` for error fallbacks (not `innerHTML`)
- **Ref callbacks** for highlight.js output (DOMPurify-sanitized)
- **React text children** instead of `esc()` + `sanitize()` HTML strings
- **No regex URL linkification in HTML strings** — use React elements via `.split()`
- **Shell injection prevention** — `/etc/hosts` update uses `sudo tee -a` (not `sh -c echo`)

## Security Rules for Development

When writing new code, these rules MUST be followed:

### Backend
1. **Never read sensitive paths** — all file reads must go through `hooks.py` which enforces `is_sensitive_path()` and `is_sensitive_bash_command()`
2. **Never trust LLM output** — scan with `redact_exfiltration_urls()` before posting to any external surface (Slack, dashboard, API responses)
3. **Validate all MCP tool inputs** — use `validation.py` schemas; never pass raw LLM input to filesystem, subprocess, or database operations
4. **Deny-by-default for authorization** — reject unless positively confirmed. Never use `if x and y and z` guards where any falsy value skips the check
5. **Sandbox all agent subprocesses** — new subprocess spawning must go through `AcpClient._spawn()` which applies OS-level sandbox
6. **Enforce denied commands** — new destructive CLI-facing tools must be covered by a `DeniedCommandRule` in `BUILTIN_DENIED_RULES` (`security.py`); enforcement is at the hooks PreToolUse gate, never via kiro agent-config injection
7. **Log security events** — all tool invocations and permission *decisions* (a capability granted or denied) must emit SEL events. The absence of a decision — e.g. skill-trigger matching that injected and excluded nothing — is not itself an auditable event (see "What counts as an auditable permission decision" above)

### Frontend
1. **Never use `dangerouslySetInnerHTML`** without DOMPurify sanitization
2. **Never use `innerHTML`** — use `textContent`, `createElement`, or React elements
3. **Never construct HTML strings with user/LLM content** — use React components
4. **Sanitize all external content** — use `md()`, `sanitize()`, or `esc()` from `helpers.ts`
5. **No inline event handlers in HTML strings** — use React event props

### Binary File Handling (`security.py`, `handlers/files.py`, `mcp_core.py`)

The `file_send` MCP tool and outbox handlers support binary media files with a deny-by-default MIME allowlist.

#### BINARY_MIME_ALLOWLIST

Module-level constant in `security.py`. Only these MIME types are accepted for binary (non-UTF-8) files:

| Category | Types |
|----------|-------|
| Audio | `audio/mpeg`, `audio/wav`, `audio/x-wav`, `audio/ogg`, `audio/flac`, `audio/aac`, `audio/mp4`, `audio/webm`, `audio/opus` |
| Video | `video/mp4`, `video/webm`, `video/ogg` |
| Image | `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `image/bmp` |
| Document | `application/pdf` |

**Excluded:** `image/svg+xml` (XSS vector — SVG can contain `<script>` tags).

#### Security Model

| File type | Content scan | MIME check | Disposition |
|-----------|-------------|------------|-------------|
| Text (UTF-8 decodable) | `redact()` for credentials/exfiltration | N/A | `attachment` |
| Binary (in allowlist) | Skipped (can't redact binary) | Must be in `BINARY_MIME_ALLOWLIST` | `inline` (browser renders natively) |
| Binary (not in allowlist) | N/A | Rejected with 400/403 | N/A |
| SVG (UTF-8 decodable) | `redact()` for credentials/exfiltration | Not in allowlist (text path) | `attachment` (never inline — defense-in-depth against XSS) |

#### Response Headers

All outbox downloads include:
- `Content-Type`: from `mimetypes.guess_type()` or `application/octet-stream`
- `Content-Disposition`: `inline` for media, `attachment` for others
- `X-Content-Type-Options: nosniff`: prevents MIME sniffing attacks

#### Invariants

- Path traversal protection unchanged (resolved path must be under `outbox_dir()`)
- Filename sensitivity check unchanged (`redact(filename) == filename`)
- Text content redaction unchanged for UTF-8 files
- Binary files: filename validated, content scan skipped (binary data cannot be meaningfully redacted)
- Dashboard multipart uploads open their destination with `O_BINARY` when the
  host provides it, so Windows cannot translate embedded LF bytes to CRLF and
  corrupt archives or media between validation and the restricted write.

#### Slack Delivery Audience — Strict Caller-Identity Classification (`file_send`)

`file_send` may additionally upload the file to Slack. The Slack **audience**
(which thread, or the whole channel) is decided from the CALLER's own session
identity, resolved **strictly** — gateway-injected `KIROCREW_SESSION_KEY` env
var or an HMAC-sidecar-verified host-pid only; **no** `/proc` ancestor walk and
**no** `session_pid_*.txt` filesystem glob. This closes both the
forged-`session_pid_*.txt` path and the subagent→parent misresolution path
(`_resolve_session_key_strict()` in `mcp_core.py`).

The identity is classified into **three** states by
`_classify_slack_identity() -> (state, thread_ts|None)`. Collapsing the two
non-thread cases into a bare `None` was a channel-root disclosure hazard: an
**unresolved** caller that still supplied an explicit tracked channel would
upload at the **channel ROOT** (`thread_ts=None` + channel), exposing a file
meant for one thread to the entire channel — fail-**OPEN** with respect to
audience. The states:

| State | Meaning | `file_send` disposition |
|-------|---------|-------------------------|
| `thread` | Resolved Slack thread — a canonical `slack:<thread_ts>` key (converted via `messaging.link.legacy_key`) or an already-bare legacy Slack key | Upload **threaded** to that `thread_ts` |
| `non_slack` | Resolved non-Slack session (`dashboard:`/`discord:`/app/channel/future namespace) — identity is KNOWN | Keep existing **authorized** routing (owner DM / session-map-linked thread / explicit tracked channel); none of these broadcast at a channel root for an unknown caller |
| `unresolved` | Strict resolution failed (no gateway env var and no HMAC-verified host-pid) — caller cannot be attributed | **Refuse the Slack upload entirely** (fail CLOSED for audience). The file is still delivered via the dashboard/outbox card |

On the `unresolved` refusal, `file_send` records a SEL
`log_tool_invocation(outcome="denied", downstream_service="slack",
error="slack_identity_unresolved_upload_refused")` event and returns a warning
noting the Slack upload was skipped (dashboard delivery still succeeds).

**Warm-pool sessions:** a warm-pool-claimed Slack session has no strict identity
source (the gateway writes the env var / HMAC sidecar only at sandbox spawn, not
at warm-pool claim), so every one of its `file_send` calls classifies as
`unresolved` → the upload is **refused**, not broadcast. There is no interim
window that broadcasts at a channel root. Restoring proper *threaded* delivery
for warm-pool Slack sessions (by writing the HMAC sidecar at warm-pool claim
time) is a delivery-quality follow-up, not an audience-safety gate — the
disclosure hazard is closed by the refuse-on-unresolved rule above.

#### Slack Upload Authorization Rungs (`file_send`)

Both `file_send` legs resolve their destination through one oracle (`dashboard/upload_destination`), and both run the same two ceilings there before any destination work:

| Rung | Predicate | Denial |
|------|-----------|--------|
| `channels`-scope governance | `upload_destination._slack_egress_permitted` → `vet_and_audit("channels", "slack", fail_closed=True)` | `403` `channels_governance_denied`, SEL governance decision recorded for grant AND denial |
| Restricted-session ceiling | `upload_gate.uploads_restricted(channel_type="slack")` — the predicate the channel leg and the Telegram/Discord renderers' extraction path share | `403` `restricted_session`, SEL-audited by the shared predicate |

On the Slack leg both are **direct calls**, not registry entries. Slack is deliberately absent from `channel_transports` (its dedicated client and streaming path are not registered), so it never reaches the shared send ladder that applies the governance vet for every other channel — the same situation `chat_compaction_notice._channel_egress_permitted` already resolves the same way. Registering Slack to reach the ladder would change what the registry means; a direct call to the audited seam does not.

Why this leg needs them most: Slack is the broadest-audience surface (a tracked channel can be company-visible) and the only leg whose destination a REQUEST can name (`body["channel"]`, falling back to the session-map link then the owner DM), while the channel leg's destination comes exclusively from the caller's own session-map entry. Without these rungs an incognito/temporary session that refuses to write a transcript, read memory or save a title still uploaded local file bytes into a Slack channel or DM, and a profile denying the `channels` scope refused a Telegram upload while allowing a Slack one.

**They precede destination resolution.** A denied caller never opens an owner DM and never reads the session map, so a refusal leaks nothing about where the file would have gone. (The shared admission gate — containment, MIME allowlist, content scans — still runs ahead of the oracle on both legs.)

**The restricted rung restores the durable flags first.** For a channel-native key the ceiling reads `privacy_mode`'s process-local trackers, which only an INBOUND channel message populates. A turn no inbound message drove — a cron, a webhook-resumed session, a monitor/auto-nudge re-injection, an explicit `file_send` — would otherwise read empty trackers after a gateway restart and ship the bytes the user's `!incognito` forbids. `uploads_restricted` therefore calls `privacy_mode.hydrate` before consulting them, the same canonical restore `_is_restricted_session` uses, so the fix lands once for every caller of the shared predicate (both `file_send` legs and the renderers' extraction path) rather than per leg.

**Sessionless callers are not muted.** The owner-DM fallback serves callers with no session of their own (a cron, the heartbeat, an out-of-band host action). An empty `X-Session-Key` is vetted under `HOST_SESSION_KEY` (`_host`), for the same reason `handlers.messaging` uses that sentinel on its channel legs: an empty key classifies as `unknown` and matches no profile at all, so vetting under it would make host-side governance inert here, while `_host` is the stable bind target operators attach it to. The restricted rung reads no slot and no channel privacy mode for such a key, so it answers permitted. Net effect on an ungoverned host: unchanged.

**Identity asymmetry is inherited, not widened.** The Slack leg resolves its caller LENIENTLY (`_resolve_session_key()`, including the `/proc` ancestor walk) while the channel leg is handed the strict key. Both rungs read that same lenient key; neither introduces a new identity source.

**Denials are refusals, not skips.** Both answer `403` with a machine-readable `code`, so the MCP tool surfaces "Slack upload failed" rather than reporting success for a file that never left. This differs deliberately from the channel leg, where "cannot deliver here" is the common case and a skip is correct.
