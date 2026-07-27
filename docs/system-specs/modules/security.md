# Security Module

Latest amendment: 2026-07-26 (pre-request installer redirect validation,
direct-CLI ACP attestation, and fail-closed Windows process enumeration).

Last Updated: 2026-07-24 (`file_send` Slack audience is now decided by a strict three-state caller-identity classification (`_classify_slack_identity` → `thread`/`non_slack`/`unresolved`) resolved via `_resolve_session_key_strict()` only; an `unresolved` caller's Slack upload is REFUSED (SEL `slack_identity_unresolved_upload_refused`, dashboard delivery still succeeds) rather than broadcast at the channel root — see "Slack Delivery Audience" under Binary File Handling. Prior — Data home moved from top-level `~/.kirocrew` to `~/.kiro/crew` (nested under kiro-cli's `~/.kiro/` base). The sensitive-path floor now expands each KiroCrew secret/trust-root leaf under EVERY known crew-home prefix — `_CREW_HOME_PREFIXES = (".kiro/crew", ".kirocrew.archived", ".kirocrew")` — so `.env`/`sel_hmac.key`/`security_events.jsonl`/`app_admission.json`/`security_policy.json`/`profiles`/`admission_policy.json`/`denied_commands.json`/`token_signing.key`/`refresh_chains.json`/`.local_secret`/`run` are read+write-blocked in the current home, the archived rollback copy `~/.kirocrew.archived` (which still holds real secret bytes after the one-time migration), AND a not-yet-migrated pre-move `~/.kirocrew`. `_WRITE_PROTECTED_HOME_PATHS` (`config.json`/`config.local.json`) is expanded the same way; multi-segment leaves are re-joined with the OS separator so a `.kiro/crew/...` target still matches on Windows. The file-explorer crew-home deny-by-default matcher (`_crew_home_index`) now casefolds so an uppercase `~/.KIRO/crew/...` path can't slip past it on case-insensitive filesystems. Prior — 2026-07-24 remote-instance run-marker exec dir on the sensitive floor — the gateway records its own venv `kirocrew` launcher at `<home>/run/gateway-<port>.bin` (`instances/run_marker.py`, written via `atomic_write`) and the SSH token-mint + `restart_remote` `exec` it first, keyed by the remote port (marker-first resolution, falling back to the PATH candidate ladder); `run` was added to the crew-home secret leaves (full read+write block under every prefix) because its contents are exec'd unsandboxed over SSH, so an agent that could write a marker or a sandbox launcher there could escalate to unsandboxed execution — the gateway's own writers open these paths directly and do NOT route through `is_sensitive_path`. Prior: federated registry URL support: `POST /api/apps/registries` now admits a vetted full git URL as `repo` (not just a bare name) via `_is_safe_repo_identifier`, requires `https://` for HTTP-style remotes (plaintext `http://` rejected — CWE-319 MITM), derives a collision-safe default `name` from URL host+path (`_derive_registry_name`), and defaults `branch` to `main` (was `mainline`); all cache-path derivations (`_safe_cache_stem`/`_external_registry_cache_path`/`_blob_cache_key`) keep pure-safe names byte-identical but slug+sha256 disambiguate URL-derived names so a traversal/collision entry can neither escape `_manifest_cache_dir()` nor clobber another's cache; new `POST /api/apps/registries/refresh` fetch-then-swap re-warm returns `{ok, refreshed, failed, results, apps, lastSyncedAt}` and only overwrites a cache on a successful fetch (stale > missing); an untrusted registry index is treated as untrusted input on two trust boundaries — `is_clone_host_trusted` (fail-closed SSRF/rebinding gate at the three clone chokepoints `_fetch_git_blob`/`_fetch_app_manifest`/`_git_clone_or_pull`, host-level not IP) refuses cloning any host outside the public-forge ∪ configured-registry set, and a `KEBAB_RE` entry-name filter drops non-kebab index entries before they reach a filesystem op (path-traversal defense, warning-logged only); admitting a new registry host emits a distinct per-host `registries.host_trust_granted` SEL event and returns `newlyTrustedHosts` so the one-way trust grant is auditable. Prior: user-configurable denied commands — opt-out state on the KEYSTONE floor. The opt-out state (`{disable_all, disabled_ids, user_added}`) lives in `~/.kirocrew/denied_commands.json`, added to `_SENSITIVE_HOME_DIRS` (full read+write block) + the governance boot-integrity `required` tuple — NOT in the agent-readable `config.json`. So the agent can neither read nor write its own deny ceiling via ANY shell form, inheriting the mature `is_sensitive_path` gate (variable-indirection, symlinks, KIROCREW_HOME, interpreters, casefold, realpath) at the same protection level as `security_policy.json`. This REPLACED an earlier config.json + bespoke bash write-matcher approach (removed) that a series of shell-obfuscation vectors kept bypassing. Boot merges the keystone state over config.json's flat `hooks` keys (`hooks_config_from_config_dict`); the 6 dashboard mutations + `_write_denied_state` target the keystone file (0600); `_reload_live_hooks` splices the new opt-out fields onto the live `HookManager` preserving flat keys. Other invariants retained from the feature build: `HooksConfig.from_dict` is fully defensive against malformed hand-edited values (type-checks every field; `_coerce_bool` for booleans so `"false"` is not truthy); `_DenyMatcher` runs the fast fragment-split path ONLY for the RE2-authored, parity-tested built-ins; EVERY user custom regex uses the exact bounded whole-regex engine (backtracking preserved, ReDoS-safe via `is_safe_user_regex`), so a user pattern like `(ab|a).*b` or a greedy `rm .+.*x` cannot under-match the forward-only fragment matcher and let a denied command through — the bound is the first `_DENY_FALLBACK_SCAN_MAX_CHARS` (2000) chars, a DOCUMENTED trade-off (Python `re` can't give exact-semantics + full-input + ReDoS-safety at once, and true full-input would need an RE2 dependency the project avoids); the residual is user-custom-regex-only (the 137 built-in security rules are full-input via the fragment matcher) and the only bypass is a single >2000-char shell segment defeating the user's OWN custom rule; the Settings > Security disable-all toggle stays functional when governance-locked (backend keeps pinned rules enforced under `disable_all`); `pinned_builtin_command_ids()` is ceiling-only for ENFORCEMENT while `pinned_builtin_command_ids_for_snapshot()` unions all profiles for DISPLAY; heartbeat hooks rebuild per run. Prior: macOS sandbox mutual exclusion: kiro-cli >= 2.13 ships an internal agent sandbox toggled by `"sandbox"` in `~/.kiro/settings/amazon-internal.json`; it cannot nest inside KiroCrew's seatbelt (kernel EPERM), so `wrap_argv()` delegates isolation to it for kiro-cli spawns on macOS when enabled (`kiro_internal_sandbox_enabled()`/`_delegate_to_kiro_internal_sandbox()`), audit-or-deny SEL + shared env scrub + fail-toward-KiroCrew's-sandbox; macOS-only. Also Stop hooks now receive the full final assistant segment on stdin as `assistant_text` (env `KIROCREW_HOOK_CONTEXT` capped at 500 for ARG_MAX). Prior: independent source-tabs hardening: authenticated GitHub/GitLab provider spawns now fail closed unless the CLI path is canonical, root-owned end to end, non-writable by the gateway user, and contains no symlinks, which makes path validation stable through exec against a same-UID agent; ordinary user-owned Homebrew/Linuxbrew binaries are deliberately rejected. Provider commands use 1 MiB metadata/check, 2 MiB discussion, and 4 MiB diff stdout ceilings; unique direct full/check tasks hold conservative 64/8 MiB reservations under a 128 MiB aggregate ceiling until task completion, with same-URL coalescing before admission and detached stale work retaining its lease. Frontend source indexing admits only durable messages, and backend/frontend/panel source retention stops at 64 first-seen links per slot. Prior: authenticated GitHub/GitLab source-provider CLI spawns use validated absolute executables, `sandboxed_spawn_argv(..., mode="standard")`, fixed provider environments and public hosts, bounded outputs/payloads, and global process/admission caps; review-thread resolution remains exact-owner-only with coarse redacted SEL audit and pre-dispatch cache-generation invalidation. Prior: edition-neutral agent executable resolver seam: the public core no longer contains edition-specific launcher detection; `PlatformContext.agent_executable` resolves argv[0] before the unchanged outer sandbox, with identity standalone behavior and fail-closed composition semantics. Prior: Sandbox probe cache policy — transient probe failures never cached, prewarm_backend() boot hook + never-block-on-loop invariant; see "Probe failure classification + cache policy". Prior: macOS 26 sandbox restored: removed the wrong hard-coded `major >= 26 -> return False` gate in `_probe_sandbox_exec()` — Seatbelt/`sandbox-exec` still work on macOS 26 (Tahoe), so the empirical probe now decides on all versions. macOS 26 users get full Seatbelt isolation (credential-path + hardlink denies) instead of the fail-closed no-isolation path; verified the real profile compiles, runs kiro-cli, and enforces `~/.aws` denies on 26.5. Commit 92e24570: added the `sandboxed_spawn_argv` chokepoint — `wrap_argv` OS isolation + `scrub_env` credential-env scrub in one call — and routed the three agent-influenced subprocess spawns through it (`mcp_discovery.probe_server`, `task_executor.run_tests`, `git_coord._git`/`_is_git_repo`), which previously ran with the full inherited environment and no wrapper; added `test/test_spawn_audit.py`, an AST tripwire asserting every spawn in `src/kiro_crew` is either chokepoint-routed or in an explicit benign allowlist. Prior: Gateway boot resilience: `apps/backend.py:start_enabled_app_backends()` now wraps each `start_app_backend()` in try/except so a per-app spawn failure — notably `wrap_argv()` fail-closing when no sandbox backend exists, e.g. macOS 26 where `sandbox-exec` is gone — is logged + `error`-audited + skipped instead of crashing the whole gateway. Prior: documented the advisory-only App Kit manifest permission model — `apps/permissions.py:validate_permissions`/`format_permissions_summary` are unwired dead code (only exercised by tests), `check_tool_permission` empty-list fail-open, distinct from the enforced HTTP app-token scope; in-process enforcement deferred to app-sandbox-roadmap.md. Also: contained App Kit admission gate (`apps/admission.py`, banned/approved/optional-HMAC-signature, fail-closed on unreadable policy) on install/update/enable/register/registry, and canonical path-containment on `backend.entryPoint` + absolute-path rejection in `AppManifest.validate` with a runtime backstop in `apps/backend.py`; hard off-switch `agent.apps_allow_third_party` refuses in-process AND out-of-process third-party app Python (per input-validation guidance); reverse-proxy HMAC now binds `sha256(body)`; `~/.kirocrew/app_admission.json` added to the sensitive-path floor. Prior commit 78224f3f: exfil-URL scan now covers the full path (not just query-after-`?`) via `_HARD_CREDENTIAL_RE` and `_URL_RE` matches raw IPv4 / bracketed IPv6 literal hosts, closing the path-embedded-secret and raw-IP-destination bypasses; commit 5682f92b: data-egress/reverse-shell command shapes (`_BASH_EXFIL_PATTERNS` / `audit_bash_exfiltration()`) are now DENIED at the tool-invocation gate (`hooks.on_tool_call` + `mcp_cron`), previously only advisory-audited. Prior: Consolidated redaction/SSRF/XPIA follow-ups: PEM round-3 (05687e60) — truncated-key run crosses a single blank line via `(?=\r?\n[A-Za-z0-9+/=])` so RFC 1421 ENCRYPTED bodies past the `DEK-Info:` blank line are redacted, TWO+ blanks still terminate; JWT/JWE union (cc1d6bdd/a8e5fe6a) — `_CREDENTIAL_PATTERNS` `eyJ` quantifier widened to `(?:\.[A-Za-z0-9_-]*){2,4}` (JWS + 5-seg JWE incl. dir/ECDH-ES empty segment) and Bearer alternative made JSON-aware + case-insensitive (`(?i:Authorization)["']?[:=]["']?(?i:Bearer)`); `StreamRedactor` split-Bearer + ceiling — `_BEARER_ANCHOR_PARTIAL_RE` holds a split `Authorization: Bearer <token>` across chunks, `_PARTIAL_JWT_TAIL_RE` (`{0,4}`) + `_STREAM_HOLDBACK_JWT_MAX=4096` un-bisect a terminal JWT/JWE/opaque-Bearer, and a credential-anchored tail past the ceiling FAILS CLOSED via `_REDACTED_CREDENTIAL_TAG` (plain runs still committed); entropy glued-secret (bf7b1baf) — pass 3 gates each `{40,}` run through `_contains_bare_secret()` sliding a 40-char window so a secret glued to an adjacent base64 char is caught; XPIA round-2 (1fde6107) — case-insensitive/whitespace-tolerant thread-parent fence neutralization (`_neutralize_fence_markers`) + explicit WITHHELD branch for an injection-tripped parent, and `scan_memory()` degrades on ANY import failure (was `ImportError`-only); SSRF trailing-dot (76640a75) — `_resolve_blocked_addr` rstrips a trailing dot before parsing so `169.254.169.254.`/`127.0.0.1.` classify as blocked. Also folds the empty-user DB-URI fix (`://[^\s:/@]*:`). Prior: shared redacting `_dm_owner` owner-DM exit point for the Slack expiry notification; Spec drift sync: documented protected-branch git-push gate (`_PROTECTED_BRANCHES` + ambiguous refs + `_PUSH_ALL_BRANCHES_FLAGS` deny, pure `_is_git_publish` detector, centralized allow/deny + `push_allowed`/deny SEL in `is_denied`, per-segment `_is_push_to_protected_branch`); Host-header DNS-rebinding validation (`check_host`/`build_allowed_hosts`/`host_validation_middleware`); conditional PYTHONPATH/PYTHONHOME strip on the kiro-cli spawn path (`_PYTHON_ENV_PREFIXES`); denied-command count 113→116 + bundled-defaults path fix to `src/kiro_crew/config/defaults.json`. Prior: widget-postMessage forged-turn: backend deny-by-default guard in `api_chat` refuses orchestrator `go`/`go all` auto-run escalation for `meta.origin='widget'` turns — SEL `auto_run_denied` — completing item 5 backend half; frontend human-gesture pre-fill + `origin` tag already shipped. Prior: Pentest round 2 + auth hardening: `StreamRedactor` cross-chunk credential holdback (`_STREAM_HOLDBACK_MAX=512`) + ~12 third-party provider token families (GitHub/GitLab/Stripe/SendGrid/OpenAI/Anthropic/npm/PyPI/DigitalOcean/Google OAuth) + DB connection URIs added to `redact_credentials`; `is_sensitive_path()` symlink resolution (realpath/`Path.resolve` + casefold home-realpath anchor, CWE-59) + `ln`/`cp` symlink-staging block; per-session logout `RevokedNonceStore` denylist + `revoke_access_cookie()` deny-by-default (CWE-613), link-click mints a separate session cookie (`register_nonce=False`) and denylists the link nonce; app-token `_enforce_app_scope` least-privilege confinement (CWE-269); `mc_token_<port>` `Secure` via `origin.is_https_request()`. Prior: challenge-and-redirect for Slack REMOVED — messages processed inline; SEC-009 loud no-isolation fallback + `agent.sandbox_allow_no_isolation`; time-limited safety override replacing permanent YOLO, per-segment deny pattern evaluation, 3-tier interactive trust escalation, SSH tunnel -N flag fix)

## Overview

KiroCrew implements defense-in-depth security across multiple layers: OS-level process isolation, credential path protection, input/output validation, authentication, authorization, and audit logging. This document consolidates all security controls and the vulnerabilities they address.

## Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| XPIA credential theft | LLM reads `~/.aws`, `~/.ssh` via `fs_read` or `cat` | Hook-layer path blocking + OS sandbox |
| XPIA data exfiltration | LLM embeds secrets in URLs posted to Slack/dashboard | Output scanning + URL redaction |
| Cross-origin WebSocket hijack | Malicious page connects to `ws://127.0.0.1:5476/api/ws` | Origin header validation |
| Cross-origin mutation (CSRF) | Malicious page POSTs to dashboard API | Origin/Referer validation on non-safe methods |
| DNS rebinding | Attacker domain resolves to `127.0.0.1`; browser sends forged `Host` to the loopback-bound dashboard (incl. GET exfil) | `Host`-header allowlist validation on every method (`check_host` / `host_validation_middleware`), deny-by-default, 403 + SEL audit |
| Unauthenticated remote access | Dashboard bound to `0.0.0.0` | Loopback-only by default (`127.0.0.1`); when user opts in via `dashboard.url`, token auth middleware requires HMAC-SHA256 signed, IP-pinned, single-use tokens on every request |
| Unauthenticated remote access (AEA tunnel) | `tunnel.enabled` exposes dashboard via public HTTPS URL | Double auth: Tunnels validates Midway OIDC at edge + KiroCrew token auth middleware. Security gate refuses tunnel start without token auth active. Owner-only access (Tunnels restricts by username). SEL audit on connect/disconnect/denial |
| Unauthorized dashboard access | No auth on localhost | Token auth middleware on all requests (loopback bypass removed); file-based IPC secret for internal paths |
| Non-owner Slack interaction | Any workspace member clicks YOLO/approve buttons | 5-layer owner verification |
| Fail-open owner lock | `KIROCREW_OWNER_ID` unset → no check | Deny-by-default: refuse connect + reject messages |
| MCP input injection | Malformed/oversized tool inputs from LLM | Centralized schema validation (`validation.py`) |
| MCP response DoS | Unbounded tool output fills memory | Response truncation at 100K |
| Destructive CLI commands | LLM runs `rm -rf /`, `git push --force` | 130 built-in denied-command rules (`BUILTIN_DENIED_RULES`, default-on / user-disableable) enforced at the hooks PreToolUse gate + governance `commands` force-deny (enterprise, un-opt-out-able) + 55 suspicious bash patterns with per-segment matching (`security.py`) |
| Frontend XSS | `dangerouslySetInnerHTML` with unsanitized content | DOMPurify + safe DOM APIs + Mermaid `securityLevel: 'strict'` (iframe sandbox) |
| Widget postMessage forged turn | LLM-emitted `<script>` in a sandboxed `<mcwidget>` iframe calls `parent.postMessage({type:'mc-widget-action'})`, bypassing the in-iframe `isTrusted` click guard | Frontend requires a human gesture: a widget action only PRE-FILLS the composer (never auto-submits) and tags the resulting user-initiated send `meta.origin='widget'`. Backend deny-by-default: `api_chat` refuses the sole chat-text-reachable privilege escalation — orchestrator `go`/`go all` auto-run — for `origin='widget'` turns (SEL `auto_run_denied`), letting the text fall through to a normal fully-gated turn. Mode changes and tool approvals are on separate endpoints the iframe cannot reach |
| YOLO mode abuse | Unbounded auto-approve window | Time-limited safety override: Slack 30min, dashboard 6h, config 24h (no permanent mode). Re-auth required after expiry. SEL audit on every lifecycle event |
| Trust reads bypass | Read-only command classification tricked into approving writes | Deny-by-default: rejects redirections, command substitutions, newline separator bypasses. Prefix matching only |
| Port-forward auth bypass | socat/ssh -R makes remote traffic appear as 127.0.0.1 | Loopback bypass removed; all requests require token auth. File-based IPC secret for internal paths |
| Observe-mode context poisoning | Non-owner messages in shared channels influence LLM context | `channel_history.push` gated on `_user_authorized` |
| Outbound data exfiltration | LLM exfils data via `curl -d @file`, `nc < file` | Data-egress/reverse-shell command shapes (`_BASH_EXFIL_PATTERNS` / `audit_bash_exfiltration()`) are **denied at the tool-invocation gate** (`hooks.on_tool_call` + `mcp_cron`), not only advisory-audited (commit 5682f92b); + `redact_exfiltration_urls()` on output |
| Credential file permissions | `.env` readable by group/other | `chmod 600` enforced at credential load time + setup wizard |
| SEL event forwarding leaks | Forwarded audit events contain raw credentials | `redact()` applied to all string fields before callback |
| Unsigned/unadmitted app install | Malicious app installs/registers via CLI, registry, or `POST /api/apps/register` with no admission control (`register_external_app` writes `enabled=True`) | Contained App Kit admission gate (`apps/admission.py`) on install/update/enable/register/registry — kill-switch `banned` (always wins) + `approved` allowlist + optional HMAC `require_signature`, fail-closed on an unreadable `app_admission.json`; absent policy admits (interim default) |
| App manifest path traversal | `backend.entryPoint`/`agents`/`skills`/`sops`/`ui.entry` uses `..` or an absolute path to escape the app root | `AppManifest.validate(app_root=...)` canonical containment (resolve + `is_relative_to`) + absolute-path rejection at install/discovery; runtime backstop in `apps/backend.py` rejects an `entryPoint` that resolves outside the app root at boot |
| App over-privilege (advisory-only manifest model) | Malicious/buggy app exceeds its declared manifest `permissions` (extra `mcpTools`, `network`, `shared` memory) | **Advisory today** — `apps/permissions.py:validate_permissions`/`format_permissions_summary` are unwired (only exercised by tests), `check_tool_permission` fails open on empty allowlist; real confinement is the HTTP app-token scope (`token_auth.py`, CWE-269) + OS sandbox, plus the `agent.apps_allow_third_party` off-switch; in-process capability gating tracked in `app-sandbox-roadmap.md` (TRACKING) |
| Plaintext-transport registry MITM (CWE-319) | A federated registry added over `http://` lets a network attacker swap the fetched index + app manifests, whose setup code later runs with gateway privileges (signatures optional by default) | `_SAFE_HTTPS_URL_RE` in `apps/routes.py` accepts **`https://` only** (plaintext `http://` rejected); private remotes use an explicit `ssh://`/scp form. `POST /api/apps/registries` validates every `repo` through `_is_safe_repo_identifier` (bare name **or** vetted git URL — shell metacharacters / traversal / owner/repo shorthand rejected) |
| Registry-index SSRF via injected clone host (CWE-918) | An untrusted external registry index lists an app whose `repo` points at a loopback/internal address (e.g. `https://127.0.0.1:8443/x`); the App Store browse/refresh/install path clones automatically, driving `git clone` against the internal network (authenticated backend SSRF; DNS-rebinding-capable) | `is_clone_host_trusted` (`apps/registry.py`) fails **closed**, constraining every URL clone to a **host** in the public-forge ∪ configured-registry trust set, enforced at all three clone chokepoints (`_fetch_git_blob`, `_fetch_app_manifest`, `_git_clone_or_pull`). Gates on hostname not IP → rebinding-proof. Host-level SSRF defense, **not** a supply-chain control (admission/signature gate is the orthogonal second layer) |
| Registry-index path traversal via entry name (CWE-22) | A hostile/typo index entry `name` (`/tmp/victim`, `../../victim`) flows to `app_source_dir(name)` and, on a failed clone, `shutil.rmtree(dest)` on the attacker-selected path | Every index entry name is validated against `KEBAB_RE` during normalization and **dropped before it is cached or listed** (warning-logged only); non-string / non-kebab names never reach a filesystem operation |

## Modules

### OS-Level Sandbox (`sandbox.py`)

Hides credential paths from kiro-cli subprocess tree using platform-native isolation:

- **Linux**: user + mount namespace — `unshare(CLONE_NEWUSER)` → identity UID/GID map → `unshare(CLONE_NEWNS)` → bind-mount empty dirs
- **macOS**: `sandbox-exec` with Seatbelt profile denying file reads. Backend availability is decided **empirically** by `_probe_sandbox_exec()` (write an `(allow default)` profile, run `sandbox-exec -f <profile>` against a trusted fixed system binary — `/usr/bin/true`, never the user-writable kiro-cli — and require exit 0) — there is **no hard-coded OS-version cutoff**. macOS 26 (Tahoe) is fully supported: Seatbelt is the same kernel subsystem backing App Sandbox/iOS/Chromium and was not removed; an earlier `major >= 26 → return False` gate wrongly disabled a working sandbox and was removed (verified the real profile compiles, runs a sandboxed process, and enforces credential-path denies on macOS 26.5). The `(allow default)` + targeted-deny profile also sidesteps the `(deny default)` sysctl-allowlist pitfall that caused the false "sandbox broken on macOS 26" reports.

#### Sandbox Modes

| Mode | Config value | Hides | Accessible | Env scrub |
|------|-------------|-------|------------|-----------|
| **Standard** | `"auto"` (default) | `.gnupg`, `.gpg`, `.config/gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` | `AWS_SECRET*`, `AWS_SESSION*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GIT_ASKPASS` |
| **Strict** | `"strict"` | All of the above + `.aws`, `.ssh`, `.kube` | Only `~/.ssh/known_hosts` | Same as standard |
| **Off** | `"off"` | Nothing | Everything | Nothing |

**Standard mode** (new default) enables git-over-SSH, AWS CLI via `credential_process`, and kubectl while maintaining OS-level isolation on non-workflow credential stores. Env vars are scrubbed in ALL modes — `credential_process` reads from `~/.aws/config`, not env vars.

**Conditional PYTHONPATH/PYTHONHOME strip** — `PYTHONPATH` and `PYTHONHOME` (`_PYTHON_ENV_PREFIXES`) are stripped **only** on the foreign kiro-cli / agent spawn path (`AcpClient._spawn()` → `acp/client.py`, plus `acp/runtime.py`), threaded through the `strip_python_env=True` kwarg passed at exactly those two spawn sites. They are deliberately **excluded** from `_SENSITIVE_ENV_PREFIXES` so KiroCrew's OWN sandboxed Python children (cron scripts, app backends, code-review workers) keep them and can still `import kiro_crew`. Rationale: KiroCrew exports `PYTHONPATH` at its own site-packages, and a foreign MCP server bundling its own interpreter/deps would otherwise prepend KiroCrew's site-packages to `sys.path` and load KiroCrew's fastmcp/cryptography instead of its own — an ABI collision / init hang. This mirrors the PYTHONPATH/PYTHONHOME pop `mcp_gateway/gatewayd.py` already does for the gateway's own children.

**Fail-closed default when no backend**: when no sandbox backend is available (e.g. macOS >= 26, or Linux without user namespaces), `wrap_argv()` **raises `RuntimeError`** by default rather than executing the agent unsandboxed — the secure default is to refuse, not degrade. The denial also emits a `denied` SEL tool-invocation event. Running unsandboxed is a deliberate opt-in via `agent.sandbox_allow_unsandboxed_exec=true`.

**Nested-sandbox passthrough**: when `wrap_argv()` is called from a process that is *already inside* a KiroCrew namespace sandbox (script-cron ticks, sandboxed agent children), it returns the argv unchanged (one-shot info log) instead of trying to wrap again. Nested sandboxing is impossible by design — the launcher's seccomp-BPF filter denies `unshare`/`setns`, so a nested backend's `unshare` would fail with EPERM and the fail-closed `RuntimeError` above would brick **every** in-sandbox MCP spawn (the probe error was raised on each `ctx.call_tool` and silently swallowed by the caller). This is **not** a fail-open path: the outer namespace + seccomp still confine every descendant, so passthrough spawns within the existing isolation boundary. In-sandbox detection is env-marker based and **deny-by-default** (`_inside_kirocrew_sandbox()` / `_IN_SANDBOX_MARKER`): the gate keys **solely** on the explicit, single-purpose `KIROCREW_SANDBOX_ACTIVE=1`, which the launcher `main()` exports after the env scrub at the same site as `KIROCREW_HOST_PID`. It deliberately does **not** key on `KIROCREW_HOST_PID` — that variable is dual-purpose session-identity plumbing, and gating a security-relevant passthrough on a variable set for unrelated reasons would be a latent bypass. No unsandboxed code path sets the marker. The passthrough is SEL-audited on **every** invocation via `log_tool_invocation(outcome="allowed", metadata={"reason": "nested_sandbox_passthrough"}, critical=True)`, mirroring the `denied` event on the fail-closed path so the security decision is tamper-evidently recorded. `critical=True` gives it the same write reliability as the `denied`/`delegated` audits — the event is written **synchronously** after draining the async backlog, so a slow or wedged background writer cannot silently drop passthrough records. It stops short of full audit-or-deny (re-raise on SEL failure) deliberately: unlike `_delegate_to_kiro_internal_sandbox` — which on audit failure falls back to KiroCrew's own seatbelt, an equally-safe audited layer — a nested passthrough has **no** safe alternative (seccomp denies the re-wrap by design), so failing the spawn on a SEL filesystem error would couple every in-sandbox MCP call to SEL health and reintroduce a prior in-sandbox spawn outage. On a hard write failure it therefore logs loudly and proceeds: the child is confined by the outer namespace + seccomp whether or not the record lands.

**No-isolation fallback is loud (SEC-009)**: *on the opted-in path only* (`sandbox_allow_unsandboxed_exec=true`), `wrap_argv()` runs the agent with no isolation (graceful — the host is not bricked) but never degrades silently: it emits a one-shot loud `SECURITY` warning. A second, distinct flag `agent.sandbox_allow_no_isolation=true` (config-modal editable) acknowledges the risk and demotes that message to info level — it governs *log level only*, not whether execution is permitted (that gate is `allow_unsandboxed_exec`).

**macOS sandbox mutual exclusion**: kiro-cli ≥ 2.13 ships an *internal* agent sandbox in the binary itself, toggled by the `"sandbox"` key in `~/.kiro/settings/amazon-internal.json` (the kiro-cli backend's own settings dir — distinct from KiroCrew's data home `~/.kiro/crew`; the filename is the literal kiro-cli ships). Its in-process seatbelt init cannot nest inside KiroCrew's sandbox-exec wrap: the macOS kernel returns EPERM even under an `(allow default)` outer profile, so **exactly one sandbox layer can be active per kiro-cli spawn**. `wrap_argv()` enforces mutual exclusion on macOS: when `kiro_internal_sandbox_enabled()` is true and the spawn is kiro-cli (argv basename, same convention as `_resolve_kiro_bin`), the seatbelt wrap is skipped and kiro's internal sandbox owns isolation (`_delegate_to_kiro_internal_sandbox()`); when it is false, KiroCrew's seatbelt engages as always. Invariants: (1) this is **not** the forbidden silent unsandboxed fallback (SEC-009) — delegation is config-driven and deterministic, never a reaction to a wrap failure; the child still runs under an OS sandbox; the decision is logged loudly once per process and every delegated spawn emits a SEL audit event (`outcome="delegated"`, `critical=True`) on an **audit-or-deny** basis: if the audit event cannot be written, the delegation is refused and the spawn falls back to KiroCrew's own seatbelt (safety over availability while SEL is broken); (2) the env scrub (`_sandbox_env_unset_args`, shared with `sandbox_exec_argv`) is applied identically on the delegated path; (3) only kiro-cli spawns may delegate — all other agent-influenced spawns keep KiroCrew's wrap regardless of the settings file; (4) the settings read routes through `hooks.safe_read_file` (`is_sensitive_path` on the resolved target + `O_NOFOLLOW` — a symlinked settings file pointing at a sensitive path is refused) and fails toward `False` on any failure (absent/malformed/non-dict JSON, refused read, home-resolution failure → KiroCrew's sandbox stays on); it is uncached so a settings flip applies to the next spawn; (5) macOS-only — Linux namespace isolation is unaffected.

**Boot must isolate the fail-closed raise**: because the `RuntimeError` above can fire per-spawn, callers that launch multiple child processes at boot must catch it. `apps/backend.py:start_enabled_app_backends()` wraps each `start_app_backend()` in try/except so one app that cannot be sandboxed (e.g. on macOS 26 where `sandbox-exec` is gone) is logged + `error`-audited + **skipped** (never spawned unsandboxed), and the gateway (Slack + dashboard + every session) still boots — matching the fail-isolated posture of the admission re-vet and MCP reconcile branches in the same loop.

**Why standard is safe**: The hook layer (`is_sensitive_path()`) still blocks direct file reads of `~/.aws/*` and `~/.ssh/*`. Denied commands block `cat`/`head`/`tail`/`python open()` on those paths. `redact_credentials()` catches any credential patterns that leak through tool output. Three independent layers must all be bypassed simultaneously.

Config: `agent.sandbox` in `config.json` — `"auto"` (standard), `"strict"`, or `"off"`.

Wired into `AcpClient._spawn()` — all kiro-cli processes are sandboxed. Parent KiroCrew process is unaffected. Zero new dependencies (stdlib + system binaries only).

**Linux namespace sandbox**: Fork child → child calls `unshare(CLONE_NEWUSER)` → parent writes identity UID/GID map (`uid uid 1` / `gid gid 1`) to `/proc/<child>/{setgroups,uid_map,gid_map}` → child calls `unshare(CLONE_NEWNS)`, sets mount propagation private (`MS_REC|MS_PRIVATE`), bind-mounts empty dirs over credential paths (per mode), scrubs sensitive env vars (`AWS_SECRET*`, `SSH_AUTH_SOCK`, etc.), and execs the agent. Two-pipe synchronization ensures correct ordering. The child retains the real UID/GID so all toolchains (JVM ByteBuddy, Gradle, npm, etc.) work without workarounds. Implemented as a Python launcher script (`_build_launcher_script()`) spawned by `namespace_argv()`.

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
- **Symlink resolution (CWE-59)**: `is_sensitive_path()` resolves symlinks before matching — it checks multiple candidate forms (`os.path.realpath` + `Path.resolve`, plus the lexically-normalized path as a fail-safe when resolution can't complete) and returns True if ANY lands in a sensitive location, `casefold`-comparing against sensitive dirs anchored at BOTH the logical home and its realpath (defeats a home-prefix OS symlink like macOS `/var`→`/private/var`). So a workspace symlink pointing at `~/.aws/credentials` (absolute or `../../.aws/credentials` traversal) cannot be read through the link
- **Relative-traversal block (verb-agnostic)**: home-anchored/absolute references to a sensitive dir are caught by the primary matcher (`_get_sensitive_re()`), but relative-traversal forms (`../../.aws/credentials`) escape it. `is_sensitive_bash_command()` therefore blocks **any** command whose tokens name a sensitive dir via dot-slash traversal (`_RELATIVE_SENSITIVE_RE`), regardless of verb — so `dd`/`base64`/`xxd`/`head`/`tail`/`cp`/`ln` are all covered (it was previously gated on `ln`/`cp` only, letting the others slip past). Returns "command references a sensitive credential path via relative traversal"
- `is_sensitive_bash_command(cmd)` — regex matches `cat`, `head`, `tail`, `less`, `cp`, `scp`, `python open()`, pipe redirects targeting sensitive paths
- `hooks.on_tool_call` runs **both** `is_sensitive_path` and `is_sensitive_bash_command` on the **normalized** tool title regardless of the kiro-cli `Reading: `/`Running: ` display prefix. The claude-agent-acp adapter sets a file-read tool's title to the bare path and a Bash tool's title to the bare command (no prefix), so gating either check on the prefix would let credential reads through on an alternate ACP backend. `is_sensitive_path` resolves the title as a path (a bare `~/.aws/credentials` matches; a `cat ~/.aws/credentials` command resolves to a non-sensitive path and is caught by `is_sensitive_bash_command` instead).
- Sensitive paths: `~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.gpg`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`, `~/.kube/config`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, `~/.kiro/crew/.env`, `~/.kiro/crew/sel_hmac.key`, `~/.kiro/crew/security_events.jsonl`, `~/.kiro/crew/app_admission.json`, `~/.kiro/crew/run`
- **Crew data-home secret/trust-root leaves are covered under EVERY known home prefix.** Since the data home moved from top-level `~/.kirocrew` to `~/.kiro/crew`, each KiroCrew secret / governance trust-root leaf (`.env`, `browser-cookies.txt`, `playwright-storage-state.json`, `sel_hmac.key`, `security_events.jsonl`, `app_admission.json`, `security_policy.json`, `profiles`, `admission_policy.json`, `denied_commands.json`, `token_signing.key`, `refresh_chains.json`, `.local_secret`, `run`) is expanded onto `_SENSITIVE_HOME_DIRS` under each entry of `_CREW_HOME_PREFIXES = (".kiro/crew", ".kirocrew")`. So the same leaf is read+write-blocked in (1) the current home `~/.kiro/crew` and (2) a not-yet-migrated pre-move legacy `~/.kirocrew`. The migration force-deletes `~/.kirocrew` once the move completes — there is no rollback copy to gate. A new secret is added to `_CREW_SECRET_LEAVES` once and is covered in both locations.
- **SEL audit trust root (Talos finding cdf82704)** — `~/.kiro/crew/sel_hmac.key` (the HMAC-SHA256 signing key for the tamper-evident audit chain) and `~/.kiro/crew/security_events.jsonl` (the audit log itself) are on the sensitive floor. Without this an audited agent could `fs_read` the key and forge/rewrite the chain, or rewrite the log directly, erasing evidence of its own actions. The gateway's own SEL writer/reader (`sel.py`, `dashboard/session_health.py`) opens these files directly and does **not** route through `is_sensitive_path()`, so audit logging is unaffected. Residual: the key still lives in the agent's namespace; a deeper out-of-process signer (key never exposed to the agent) is documented future hardening.
- **App admission trust root (keystone invariant)** — `~/.kiro/crew/app_admission.json` (the sole fleet-controlled App Kit admission ceiling loaded by `apps/admission.py`) is on the sensitive floor, read- and write-blocked through the shared file gate. Because an **absent** policy falls open (admit), a governed agent that could `fs_write`/`tee`/`rm` this file could delete or neuter its own admission ceiling and re-admit a banned/unsigned app. Adding it to `_SENSITIVE_HOME_DIRS` also arms the bash write/extract matcher (`tee`/`rm`/redirect). The gateway's own loader (`apps.admission.load_app_admission_policy`) opens it directly via `path.read_text()` and does **not** route through `is_sensitive_path()`, so legitimate policy loading is unaffected.
- **Runtime exec dir trust root (run-marker sandbox-escape)** — the `run/` dir under the crew home is on the sensitive floor (under every `_CREW_HOME_PREFIXES` entry) because the gateway executes its contents **outside** the agent sandbox: the sandbox launcher scripts (`sandbox.py` execs `python <home>/run/kirocrew_sandbox_*.py`) and the remote-instance run-marker `gateway-<port>.bin` (`instances/run_marker.py`), whose recorded launcher path the SSH token-mint **and** the `restart_remote` action read and `exec` on the remote host. Without this floor entry a prompt-injected / sandboxed agent that could `fs_write` (or `tee`/redirect) a marker — or a launcher — into `run/` could point it at an attacker-controlled binary that the next routine token refresh or restart execs unsandboxed (owner + `-x` checks don't help; agent writes run as the same user) — a reachable sandbox escape. Classifying the whole dir also arms the bash write/extract matcher. The gateway's own writers (`instances/run_marker.write_marker` via `atomic_write`, and the sandbox launcher writer) open these paths directly and do **not** route through `is_sensitive_path()`, so gateway startup/spawn is unaffected.

**Write-only config protection** (`is_sensitive_write_path` in `security.py` + `hooks.py`) — runtime config files are protected against *modification* by agent tools while staying *readable*:
- `~/.kiro/crew/config.json` and `~/.kiro/crew/config.local.json` are in a write-only tier (`_WRITE_PROTECTED_HOME_PATHS`, expanded under every `_CREW_HOME_PREFIXES` entry so the pre-move legacy copy is covered too), deliberately NOT in the read+write `_SENSITIVE_HOME_DIRS` list above — the dashboard file viewer, `cat`, and knowledge indexing legitimately read config.
- `is_sensitive_write_path(path)` is a superset of `is_sensitive_path(path)`, sharing the same `_path_in_home_dirs` resolve/casefold core so the two gates can't drift. `hooks.on_tool_call` denies a file-EDIT tool call (ACP `edit` kind) whose `path`/`file_path` resolves to a config file.
- Empty/unknown ACP tool kinds are intentionally left to the load-time clamp backstop rather than hard-denied, to avoid over-blocking config reads that arrive without a kind (governance's shape inference can apply both read+write scopes because it is a permissive policy intersection; this gate is a hard deny). Bash writes (`tee`, `>`, `sed -i`) likewise fall to the clamp.
- The operator edits config out-of-band via the dashboard config API / CLI, which do not route through this gate.

**Data-home completion-marker protection** (`.data-home-ready`) — the marker whose presence makes `~/.kiro/crew` authoritative (migration is skipped once it exists, and a leftover legacy home is treated as debris). Because its mere *presence* is the trust signal — and, unlike config files, **no load-time clamp neutralizes a planted value** — a prompt-injected agent that could create it in a pre-migration home would make the next boot skip migration and ignore the legacy home's governance policy + secrets (deleting it forces a needless re-migration). It is therefore protected on *both* enforcement layers:
- **File-edit tool gate**: the marker is in `_WRITE_PROTECTED_HOME_PATHS` (under every `_CREW_HOME_PREFIXES` entry), so `is_sensitive_write_path` denies an ACP `edit`-kind write to it while reads stay allowed.
- **Bash gate**: the marker leaf is also in `_WRITE_PROTECTED_BASH_LEAVES`, and `is_sensitive_bash_command` matches it **verb-independently** (any command naming the home-anchored marker path, including a trailing-`/` subpath so `mkdir -p …/.data-home-ready/x` — which also materializes it — is caught). This mirrors the verb-independent backstop the sensitive-dir matcher uses, so quoted redirects / `cp` / `python open()` / novel write verbs cannot bypass an enumerated allowlist. Bash *reads* are incidentally blocked too — harmless, since the marker holds no secret (it is deliberately NOT in `_SENSITIVE_HOME_DIRS`, so file-read tools and `is_sensitive_path` are unaffected) and the only legitimate readers (`kirocrew doctor`, the migration code) use Python `os` calls, not bash.
- The migration code stamps the marker directly in Python (not via a tool/bash), so legitimate stamping is unaffected. As with credential paths, the bash gate is home-anchored, defense-in-depth: a `cd`-into-home + bare-relative-leaf write is a pre-existing limitation shared by every sensitive-path rule, not specific to the marker.

**Load-time resource-limit clamp** (`config/loader.py`) — defends against a config-loader bound bypass: the dashboard config API rejects out-of-range writes, but a direct edit of `config.json` (any process as the same OS user, or a prompt-injected agent with file-write access) bypassed that gate.
- `KiroCrewConfig.load()` calls `_clamp_security_bounds(data)` on the disk-read path (before caching) so cache hits and the `GET /api/config/kirocrew` serialization both report clamped values.
- Clamped knobs: `agent.subagent_auto_max` ≤ `SUBAGENT_AUTO_MAX_CEILING` (64), `agent.max_subagents` ≤ 64, `agent.subagent_max_turns` ≤ `SUBAGENT_MAX_TURNS_CEILING` (200), `session.pool_size` ≤ `POOL_SIZE_MAX` (10). Mins match existing runtime floors (0/1); `bool` and non-int values are left untouched for dataclass coercion.
- The ceilings live once in `config.loader` and are imported by the API write-gate (`dashboard/handlers/core.py`) and the runtime pool cap (`session._MAX_POOL`), so the write-gate, runtime cap, and load-time clamp cannot drift.
- A clamp is logged at WARNING and recorded as a `config_bounds_clamped` SEL tamper event (best-effort, never fatal — config loading must not raise). This neutralizes any inflated on-disk value regardless of how it was written.

**URL exfiltration detection** — scans LLM output before posting to Slack/dashboard:
- `scan_exfiltration_urls(text)` — flags the payload not the destination (host-agnostic except one narrow carve-out below)
- Detects: long query strings (≥200 chars), base64 blobs (40+ chars), heavy URL-encoding, AWS access key IDs (`AKIA`/`ASIA`), SSH keys, private key headers, Slack tokens
- Hard credential markers (`_HARD_CREDENTIAL_RE`) are scanned across the **full path AND query**, not just the query after `?`, so a secret embedded in the URL path (`http://host/AKIA…`, no `?`) is caught (Talos 78224f3f). `_URL_RE` matches DNS names, **raw IPv4 literals** (incl. IMDS `169.254.169.254`), and **bracketed IPv6 literals** so a raw-IP exfil destination is not silently skipped. `_URL_RE`'s path/query group starts with `[/?]`, so a query attached **directly to the host with no path segment** (`https://host?leak=<secret>`) is captured and scanned too — previously that group required a leading `/`, so such a URL yielded no path/query group and both scan/redact bailed on `qmark == -1`, skipping the query entirely (exfil bypass). The base64-blob/query-length heuristics stay query-only (long base64 path segments — CDN asset ids, git object hashes — are benign); the S3-presigned exemption is applied before the path scan. Per-URL classification is a single shared helper (`_exfil_url_warning`) used by both scan and redact so the two paths cannot drift. **Exact-host heuristic exemption**: a companion `CredentialPolicy` may supply a set of trusted-tenant hosts (`_exempt_exact_hosts()`; the public Default returns an empty set) that skip **only** the base64-blob and query-length heuristics — the ones that false-positive on legitimate long base64 document pointers (e.g. SharePoint `nav=` links). Hosts are matched **case-insensitively** (both the captured host and the set members are lowercased, per RFC 4343) and **exactly** (not by suffix, so a shared multi-tenant domain does not exempt every tenant). The hard-credential floor (`_HARD_CREDENTIAL_RE`) **and** the heavy percent-encoding detector (`_EXFIL_PERCENT_RE`) stay **unconditional** — an AWS key / SSH-or-PEM header / Slack token / URL-encoded payload on an exempted host is still flagged and redacted.
- `redact_exfiltration_urls(text)` — replaces suspicious URLs with `[REDACTED: suspicious URL to {domain}]`

**Credential output redaction** — catches raw credential patterns in LLM/tool output:
- `redact_credentials(text)` — scans for plaintext AND base64-encoded credentials
- Plaintext patterns: `AKIA`/`ASIA` access key IDs, `SecretAccessKey=`, `aws_secret_access_key=`, `SessionToken=`, `aws_session_token=`, PEM private keys (`-----BEGIN [A-Z ]*PRIVATE KEY-----`), Slack tokens (`xoxb-`/`xoxp-`)
- **Full-block PEM redaction** (`05687e60`): the PEM sub-alternative spans the ENTIRE key block (header + base64 body up to the END marker), not just the header phrase. Because `redact_credentials()` replaces the matched SPAN, a header-only match left the secret base64 body verbatim on every output surface. The body class is `[\s\S]*?` (not base64-only) so encrypted keys — whose `Proc-Type:`/`DEK-Info:` headers carry `:`/`,` — are fully spanned; a truncated block (no END) consumes only subsequent PEM body lines (each must start with a newline), so a `BEGIN` header mentioned inline in prose matches only the header and does not swallow trailing lines to end-of-string. **Round-3:** the trailing `(?=\r?\n[A-Za-z0-9+/=])` lookahead alternative lets the run cross a SINGLE blank line when the next line begins with base64 material — RFC 1421 ENCRYPTED PEMs place a MANDATORY blank line between the `DEK-Info:` header and the base64 body, and without this lookahead the per-line "must contain a base64 char" rule stopped at that blank line and leaked the whole encrypted body (for both a truncated key and a complete encrypted key whose body exceeds the full-block cap). Because the lookahead consumes nothing, TWO+ consecutive blank lines still terminate the run, so trailing prose is preserved (no over-redaction)
- **Third-party provider families**: ~12 distinctive fixed-prefix token formats added beyond AWS/Slack — GitHub (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` PATs + `github_pat_` fine-grained), GitLab (`glpat-`), Stripe (`sk_live_`/`rk_live_`/`_test_`), SendGrid (`SG.`), OpenAI (`sk-proj-`), Anthropic (`sk-ant-`), npm (`npm_`), PyPI (`pypi-`), DigitalOcean (`dop_v1_`/`doo_`/`dor_`), Google OAuth client secrets (`GOCSPX-`) — plus DB connection URIs with embedded credentials (`postgres`/`mysql`/`mongodb`/`redis`/`amqp` `://user:pass@`). Prefixes are case-sensitive with minimum lengths set slightly below real token lengths (over-redaction on a prefix match is the safe direction)
- **JSON-aware key-value matching**: key-value patterns allow an optional quote (`[\"']?`) between the key name and the separator (`[:=]`), matching both bare `aws_secret_access_key=VALUE` and JSON `"aws_secret_access_key": "VALUE"` formats. The value class uses `[^\s"',}]+` (bounded, stops at JSON structural delimiters) rather than greedy `\S+`, preventing over-capture in compact JSON that would swallow adjacent fields and mask subsequent credentials
- **JWT / JWE / OAuth Bearer tokens** (cc1d6bdd; JWE hardening a8e5fe6a; JSON-aware Bearer): JWTs (`eyJ<header>.<payload>.<sig>` — `eyJ` is the base64url of the `{"` header prefix) and HTTP `Authorization: Bearer <token>` headers. The `eyJ` segment quantifier is `(?:\.[A-Za-z0-9_-]*){2,4}` so it redacts both a 3-segment signed JWT (JWS) and a 5-segment encrypted JWT (JWE, RFC 7516 — `header.encrypted_key.iv.ciphertext.tag`) as one whole token — including `dir`/`ECDH-ES` JWEs whose Encrypted Key segment is EMPTY (`header..iv.ciphertext.tag`); the earlier fixed 3-segment pattern truncated a JWE and leaked its ciphertext + tag. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url prefix); the Bearer header name + scheme are matched case-insensitively via scoped `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230 §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is case-insensitive (RFC 6750 §2.1) — so lowercase `authorization: bearer …` from `requests`/`net/http`/HTTP2 frame logs is redacted too. The header/scheme separator is JSON-aware: an optional quote may precede the `:`/`=` and the token (`(?i:Authorization)["']?\s*[:=]\s*["']?(?i:Bearer)…`), so a serialized `{"Authorization": "Bearer <tok>"}` in a structured-log/JSON request dump is redacted, not just the raw HTTP header. Both are scoped tightly — the JWT segment class `[A-Za-z0-9_-]` cannot cross the literal `.` separators, and the Bearer token class (`[A-Za-z0-9._~+/-]+=*`, RFC 6750 `b64token`) stops at whitespace/quotes — so neither over-captures. A `Bearer` header carrying a JWT redacts as a single match (the Bearer alternative's class subsumes the JWT), while a bare JWT is caught independently (defense in depth). Bare `eyJ…` with no `.`-segments and the word `Bearer` without the `Authorization:` prefix are NOT redacted (no false positives)
- Base64 detection: finds 40+ char base64 chunks, decodes them, checks if decoded content matches any credential pattern
- **Bare label-less secret-key detection** (`bf7b1baf`): a 40-char AWS *secret access key* (the value paired with an `AKIA`/`ASIA` ID) is a bare base64 run with NO prefix and NO `key=` label, so the fixed-format patterns above miss it when it appears standalone (echoed alone, in a log line, in a JSON array element). A third redaction pass adds an entropy + structural heuristic: `_BARE_SECRET_RUN_RE` isolates each `[A-Za-z0-9+/]{40,}` run (word-boundary look-arounds so surrounding prose is preserved), then `_looks_like_secret_key()` applies every gate below — a token must clear ALL of them (design bias is toward NOT redacting: a false negative reverts to prior behavior, a false positive corrupts benign output). Gates, cheapest-first: (1) length is EXACTLY 40 (AWS secret-key length); (2) contains lower + upper + digit (rejects all-lower prose, ALL-UPPER constants, base32, digit runs); (3) not an all-hex run (`_HEX_ONLY_RE` rejects 40-char git SHAs and 32/64-char md5/sha256 digests — verified even for mixed-case hex that would otherwise clear the entropy gate); (4) Shannon entropy ≥ `_SECRET_ENTROPY_MIN` (4.3 bits/char — real random keys average ~4.78 and rarely drop below ~4.4, while camelCase identifiers and file paths cluster at 4.0-4.3; the canonical AWS example scores 4.66); (5) does not base64-decode to ≥85% printable ASCII (`_decodes_to_printable_text` leaves encoded-text blobs to the decode-and-scan pass); (6) structural randomness — the longest run of consecutive lowercase letters ≤ `_SECRET_MAX_LOWER_RUN` (5) AND the vowel ratio ≤ `_SECRET_MAX_VOWEL_RATIO` (0.30). Both structural gates apply to EVERY token: unlike a naive design, the presence of `/` or `+` is **not** a free pass to redact, so a 40-char mixed-case file path (e.g. `src/main/java/com/Example/FooBarBazClas1`) — which contains `/` yet is built from dictionary-word segments with long lowercase runs — stays intact. The pass scans the ORIGINAL text (stable offsets) and skips any run already redacted by pass 1/2. Tests (`test_security.py::TestBareSecretKeyRedaction`) prove true positives on real secret shapes and NO over-redaction of git SHAs, UUIDs, sha256/md5 hex, base32, prose, code identifiers, or slash-delimited file paths. **Glued-secret sliding window**: `_looks_like_secret_key()` only accepts an EXACTLY-40-char token (gate 1) — its documented boundary assumption — but `_BARE_SECRET_RUN_RE` captures the *longest* base64 run, so a real 40-char secret glued to an adjacent base64 char with no delimiter (`X`+secret, secret+`A`, `SECRET=`+secret+`ABC`, secret+`X`+secret) forms a 41+ char run that fails the exact-40 gate and would leak verbatim. Pass 3 therefore gates each captured run through `_contains_bare_secret()`, which slides a 40-char window across the run and redacts the whole run when ANY window clears every gate; this stays linear (the regex yields disjoint spans). The sliding window does not over-redact >40-char benign camelCase identifier runs (no window within them looks like a secret)
- Applied on ALL 5 output paths: dashboard streaming (mid-flush + trailing), dashboard non-chunk messages, dashboard history save (JSONL), Slack final response
- **Cross-chunk streaming redaction** (`StreamRedactor`): per-chunk redaction misses a credential split across a token/streaming/Slack chunk boundary (a chunk ending `...AKIA` and the next starting `IOSFODNN7...` each individually escape `redact_credentials()`, so raw fragments reach WebSocket/SSE/Slack consumers). `StreamRedactor` is a rolling-buffer redactor: it withholds the trailing run of credential-class characters (`_CRED_CLASS` — letters/digits + URL/base64/connection-string punctuation, the possible start of a not-yet-complete credential) until a non-credential-class terminator arrives or the stream ends, then rejoins and redacts before emitting on the wire. Holdback is bounded by `_STREAM_HOLDBACK_MAX = 512` (larger than the longest fixed-format credential) so a split token is always rejoined; `flush()` redacts the buffered remainder at segment/stream end. Adds at most one chunk of latency. **Streaming JWT/JWE ceiling** (round-2 + round-3): JWTs (esp. RS256/ES256 with embedded claims) routinely exceed 512 chars, so a terminal token longer than the DoS floor would otherwise be bisected — the first `len-512` chars emitted raw before `flush()` redacts only the held tail. When the withheld tail matches `_PARTIAL_JWT_TAIL_RE` (`eyJ…` optionally followed by up to FOUR `.`-separated base64url segments — `{0,4}`, so a 5-segment compact JWE escalates too, matching the batch JWE ceiling — anchored to buffer end) the cap is raised to `_STREAM_HOLDBACK_JWT_MAX = 4096` so the whole token is rejoined before emission; the 512-char floor still applies to every non-credential run. **Split-Bearer holdback** (a8e5fe6a): an `Authorization: Bearer <token>` header spans whitespace (not in `_CRED_CLASS`), so the cred-class run alone would commit the `Authorization: Bearer ` prefix and leak the token on the next chunk. `_BEARER_ANCHOR_PARTIAL_RE` (case-insensitive, JSON-aware, `\Z`-anchored, matching any prefix of an in-progress `Authorization: Bearer <token>`) makes `feed` pull the commit index back to the anchor start (`i = min(i, anchor.start())`), holding header + token together, and escalates the cap so an opaque OAuth/refresh/SSO Bearer token >512 chars (no `eyJ`) is not bisected either. **Fail-closed ceiling** (round-3): when a credential-anchored tail (JWT/JWE/Bearer) exceeds the 4096 ceiling, `feed` FAILS CLOSED — it redacts+emits the confirmed-safe prefix, appends `_REDACTED_CREDENTIAL_TAG` (`[REDACTED: credential]`, shared with the batch redactor), and DROPS the oversized tail rather than bisecting it; a plain cred-class run with NO credential anchor is still committed verbatim (bisected — no data loss, DoS bound intact)
- Defense against write-then-execute attacks: even if the LLM tricks kiro-cli into running a credential-extracting script, the output is scrubbed before the LLM can use it in follow-up messages

### GitHub AI Review Human Overrides (`.github/workflows/`)

Human judgment is the final authority over the Fable 5, GPT 5.6, and Arbiter
AI-review results. A repository member with `write`, `maintain`, or `admin`
permission can record a false-positive, not-applicable, or accepted-risk
decision with:

```text
/ai-review override <fable|gpt|arbiter|all> <current-sha>: <reason>
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
and exits its original gate successfully. Arbiter's gating check is created via
the Checks API, so the handler updates that check and its marker-keyed PR
comment directly; later Arbiter runs also resolve the same trusted marker before
calling the model. Arbiter also re-reads the trusted marker immediately after
creating its check and patches that exact check plus its summary comment when an
override arrived during model execution. If the override arrives after that
read, the handler sees and patches the newly created check instead; either event
ordering leaves the SHA-scoped human decision authoritative. The broader
`defer-longterm` label remains an accepted-risk override for Arbiter.

All three marker-keyed comments expose the override command to repository
writers. GPT 5.6 and Arbiter also normalize each current-commit result into a
top verdict plus one sentence: `✅ no blocking findings`,
`🔴 changes requested (blocking)`, an incomplete state, or a human-override
state. Arbiter refreshes its comment while waiting for new-commit reviewer
inputs, so a green verdict from the previous commit is never left looking
current.

When no current-SHA override is active, GPT 5.6 captures convergence context
before replacing its marker-keyed comment: the prior bot review, prior
bot-recorded GPT/`all` override decisions, and recent review dispositions from
authors whose current collaborator permission is `write`, `maintain`, or
`admin`. Bodies are capped at 6,000 characters each and the complete bundle at
24,000 bytes. The two discovery outputs are likewise capped at 12,000 bytes
each before prompt assembly. The bundle is nonce-delimited and explicitly
untrusted: old SHA-scoped decisions are evidence only, never authorization for
a new SHA.

GPT still makes exactly three model calls. Passes 1 and 2 discover candidates;
pass 3 receives both bounded discovery outputs plus the bounded cross-round
context, rechecks the full diff, resolves contradictions, deduplicates, and
emits the only verdict exposed to the comment and gate. A materially identical
settled finding, or a reversal of earlier GPT guidance, must identify a concrete
changed-code or newly identified evidence delta; a new SHA alone is not a
delta. A prior disposition never hides a currently provable bug, but absent such
a delta the reconciler drops the repeated or contradictory finding. Any failed
call makes the three-pass review incomplete and leaves no current-SHA reviewed
marker, so the gate fails closed.

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

For same-repository PRs, the aggregate covers the latest PR run for CI, Build,
Code Review, Claude AI Review, GPT 5.6 Review (the reconciled result of its three
calls), and Design Review, plus the managed dynamic CodeQL workflow conclusion
and the API-owned `Arbiter — judge from comments` check. Grading the CodeQL
workflow conclusion, rather than its neutral summary check, preserves failures
from any managed Analyze job. Fork PRs cannot receive repository secrets or
OIDC credentials, and this repository's managed default-setup CodeQL workflow
is not scheduled for fork heads. CodeQL, the three secret-backed AI workflows,
and Arbiter are therefore explicitly ineligible for forks; CI, Build, and Code
Review still determine their readiness. Missing or running eligible lanes
produce `checking`; blocking workflow/check failures produce
`action required`; drafts remain `checking`.
Design Review completion is required as Arbiter input, but its verdict and
infrastructure conclusion are advisory. It emits one `PASS | CONCERNS | BLOCK`
verdict and no separate blast-radius rating. Mergeability, behind-base state,
and human review decisions are not part of this event-driven aggregate because
they can change without an aggregate refresh event; branch protection and the
live `prepare-pr` status check own them.

Every event resolves the PR's current head through the GitHub API. An event or
explicit Arbiter refresh carrying an older expected SHA is ignored, so a late
run cannot relabel the new revision. A code-free `pull_request_target` handler
updates same-repository and fork PRs from the trusted base workflow. Actions
that start or restart validation for the same SHA, including a PR description
edit that re-runs Code Review, force the aggregate to `checking` before run
lookup so an older successful same-SHA run cannot keep readiness green. Trusted
base-repository `workflow_run` events refresh it as eligible lanes finish.
Arbiter dispatches an explicit refresh after replacing its API-owned check
because its own `workflow_run` completion is not associated with the source PR.
The dispatch capability lives in a separate non-model job and runs only after
the check publication succeeds. Readiness-label events cannot recursively
rerun or cancel Arbiter: its label path accepts only `defer-longterm`, and
ignored label events use a per-run concurrency key, so they cannot cancel an
active review or replace a pending authoritative reviewer event.

The bundled `prepare-pr` skill front-loads the same review contract before the
first push. Description/diff reconciliation and every allowed commit mutation
happen before review. After local gates, it dispatches two independent,
read-only subagents over the finished base-to-head diff: one owns correctness,
security, and platform compatibility; the other owns contracts, tests, error
paths, and the user workflow. Both use the canonical severity and output rules
from `.github/workflows/codex-review.yml`. Legitimate Critical/High findings are
fixed before publication; Medium/Low findings remain advisory unless Arbiter
or a human escalates them. If a blocker fix changes code, one focused verifier
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

130 first-class `DeniedCommandRule` records in `BUILTIN_DENIED_RULES` (`security.py`) — each a stable `id`, a Python regex `pattern`, a `category`, and a human `description` — blocking destructive and credential-exfiltrating operations. They are enforced **only** at KiroCrew's own `hooks.py` PreToolUse gate (`HookManager.on_tool_call` → `PolicyAuthority.is_denied`), never by kiro-cli. They are no longer a raw `deniedCommands` array injected into a kiro agent JSON, so there is no `execute_bash`/`shell` tool-settings copy and no project-dir `agents/defaults.json` override for them. Built-ins are **default-ON but user-DISABLEABLE** from Settings → Security (see "Denied-command rules, opt-out state, and read-only auto-approve" below). ada credential patterns are NOT in KiroCrew's denied commands — kiro-cli has its own built-in deny list for `ada credentials` that cannot be overridden via agent config.

**Credential exfiltration blocks**:
- `.*echo.*\$AWS_SECRET.*`, `.*echo.*\$AWS_ACCESS.*`, `.*echo.*\$AWS_SESSION.*` — env var echo
- `.*printenv.*AWS.*`, `.*env.*grep.*AWS.*` — env dump/grep
- `.*python.*boto3.*get_credentials.*`, `.*python.*botocore.*credentials.*` — script-based extraction
- `.*curl.*169\.254\.169\.254.*`, `.*wget.*169\.254\.169\.254.*` — IMDS metadata endpoint (coarse literal-string match)
  - **Encoding-aware IMDS gate** (`_check_imds_access` + `canonicalize_ip`): beyond the literal-string denies above, every IP-like token in a bash command is canonicalized to dotted-quad and compared to `169.254.169.254`, so alternate encodings the OS resolver/`curl` accept are blocked too — single-integer (`2852039166`), hex (`0xa9fea9fe`), octal per-octet, IPv6-mapped (`::ffff:169.254.169.254`), and the inet_aton **2-part (`169.16689662`) and 3-part (`169.254.43518`) short forms** (decimal or hex trailing component). The 2-/3-part forms are resolved via `socket.inet_aton` (the same resolver `curl` uses), which also rejects out-of-range forms (`169.254.11207422`) so benign hosts are not over-blocked.
- `.*curl.*\$AWS_SECRET.*`, `.*curl.*\$AWS_ACCESS.*` — credential exfil via curl
- `aws s3 cp .* s3://.*`, `aws s3 mv .* s3://.*`, `aws s3 sync .* s3://.*` — file upload exfiltration
- `.*cat.*/\.aws/.*`, `.*cat.*/\.ssh/.*`, etc. — direct credential file reads

**Allowed operations** (system prompt explicitly permits):
- `ada credentials update` — blocked by kiro-cli's built-in deny list (not KiroCrew). Users must run ada in their own terminal; `credential_process` in `~/.aws/config` handles automatic refresh for AWS CLI commands
- `ada profile add/list/print/delete` — also blocked by kiro-cli
- `aws sts assume-role` — cross-account access
- AWS CLI commands (`describe-*`, `list-*`, `get-*`, `filter-*`, `s3 cp`, `s3 ls`, etc.) — work via `credential_process`

**Destructive operation blocks**: `rm -rf`, `git push --force`, `aws * delete-*`, `aws ec2 terminate-instances`, `cdk destroy`, `terraform destroy`, etc.

- `is_denied(tool_name, extra_patterns, *, denied_regexes)` evaluates the *effective* denied-command set plus a dedicated verb-anchored git-publish detector. The **regex tier** (`denied_regexes`, matched via `re.search`, case-insensitive) is the enabled subset of `BUILTIN_DENIED_RULES` plus the user's `user_added` patterns from the keystone `denied_commands.json` opt-out state, which the hooks layer resolves via `compute_effective_denied(...)` and passes in; the **glob tier** (`extra_patterns`, fnmatch) carries legacy `auto_deny_tools` + the companion overlay. "Agent-configured patterns" no longer means a kiro agent JSON `deniedCommands` array — that injection path is retired. When `denied_regexes` is `None` the check fails closed to all built-ins enabled. The git-publish detector and protected-branch gate are unchanged always-on floors that run before either tier:
  - **Git publish (verb-anchored regex):** `git push` is detected by `_is_git_publish()` (`_GIT_PUBLISH_RE` + `_GIT_PUBLISH_GLUE_RE`), **not** a substring glob. `push` must be the git *subcommand* (first non-flag token after `git`, allowing intervening `-x` / `-C path` / `-c k=v` options), so a commit message, branch name, grep pattern, or ssh remote payload that merely contains the word "push" is **not** blocked (e.g. `git commit -m '...push...'`, `git log --grep push`, `git switch -c fix/git-push`). Checked on the whole string first to catch command-substitution glue-evasion (`git$(echo ' ')push`, `git\`echo\`push`, `git_push`) and on segment-spanning chains (`git stash push && git push origin main`). Replaces the former broad `*git*push*` glob + ` stash push` exception, which over-blocked benign commands and surfaced as a silent `Tool use aborted` on the removed standalone provider.
  - **Protected-branch gate:** `_is_git_publish()` is a **pure, side-effect-free detector** — it only answers "is this a git push?". Whether the push is *allowed* (feature branch) or *denied* (protected/bare) is decided by `_is_push_to_protected_branch()` at the single enforcement point in `is_denied` (via a deferred `push_allow_pending` flag), which is also where **both** SEL audits fire: `_emit_deny_event` on deny and `_schedule_push_allow_audit` (SEL `push_allowed`, operation `git_push`) on allow. The `push_allowed` audit is deferred to the *final* allow exit, so a compound `<feature push> && <denied command>` chain that later trips a deny pass logs a **deny**, not an allow.
    - `_PROTECTED_BRANCHES` covers `main`/`mainline` plus the legacy Git default-branch name (see `_PROTECTED_BRANCHES` in `security.py`), plus ambiguous runtime-resolved refs `_AMBIGUOUS_REFS` = {`head`, `@`, `fetch_head`} — all matched by `_is_protected_branch_name()`. A push to any of these (or a **bare** `git push` / `git push <remote>` with no explicit branch, since the current branch might be protected) is denied.
    - `_PUSH_ALL_BRANCHES_FLAGS` = {`--mirror`, `--all`} are denied **outright** (they push every local branch, so a per-branch target check cannot vouch for them), kept in lockstep with the `--(mirror|all)` regex in `config/defaults.json`.
    - `_is_push_to_protected_branch()` splits the command with `_split_segments()` and validates **every** `push` segment / refspec (closing the `push origin feat && push origin main` bypass), normalizing `refs/heads/…` paths and `local:remote` refspecs; refspecs with shell/revision syntax (`$`, `` ` ``, `@{…}` — `_AMBIGUOUS_REFSPEC_RE`) are treated as ambiguous and denied. If a push was detected upstream but **no** clean segment parses, it denies to be safe.
    - **Force push:** a force flag (`--force` / `-f` / `--force-with-lease`) does not by itself make a feature-branch push protected (force-push to a feature branch is normal PR/rebase workflow), but force-push to a *protected* branch is still blocked because the target check fires regardless of flags.
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
default-ON catalog of **130** rules across categories `aws-destructive`,
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

**Opt-out state — keystone `denied_commands.json`.** The opt-out state is a
security ceiling, so it lives in its OWN keystone file
`~/.kirocrew/denied_commands.json` (respecting `KIROCREW_HOME`) — NOT in the
agent-readable `config.json`. The file's root IS the opt-out object:

```json
{
  "disable_all": false,
  "disabled_ids": ["<builtin-rule-id>", ...],
  "user_added": [{"id": "user-xxxxxxxx", "pattern": "rm -rf /tmp/mine", "enabled": true}]
}
```

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
  `_write_denied_state` — an atomic (0600) read-modify-write of
  `denied_commands.json` under the shared config lock; `_reload_live_hooks`
  splices the new opt-out fields onto the live `HookManager` (preserving its flat
  hook keys) so the change enforces without a restart. These operator endpoints
  open the file directly and do NOT route through the agent tool gate.

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
and cannot be toggled off. When *any* rule is governance-pinned
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

**Defense-in-depth nuance** — ~45 of the 130 rules overlap an independent,
always-on keystone control (sensitive-file reads, IMDS, git-publish, cred-env
dumps). All 130 rules are disableable, but disabling a rule does **not** disable
its keystone control — such a command stays blocked by defense-in-depth. The
~85 purely-opinionated destructive rules (AWS delete/mutate, `cdk`/`terraform`/
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
redirects/substitution/backgrounding); for a non-shell tool when
`tool_kind in {"read", "fetch"}` or `slack.gateway._is_read_only_tool(tool_name)`
is True. Both classifiers are imported function-locally to avoid an import cycle.

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

**Pull-request provider authorization and audit** (`dashboard/handlers/source_providers.py`): every full-source read, checks read, review-thread mutation, and background sidebar refresh may inherit host `gh`/`glab` credentials. Direct source APIs require the explicit empty `request["app"]` dashboard claim. With a configured `DashboardState.owner_id`, reads and mutations require exact equality with `request["user"]`. With no configured owner, only full-source and checks reads accept the signed machine-local bootstrap subjects `local-app` and `local-startup`; review-thread mutation remains denied. Machine-local startup and local-secret token issuance use the configured owner id as their subject when one exists, so the auto-opened dashboard and `kirocrew token` satisfy the same exact owner check. Missing claims, non-owners, app tokens, unrelated local subjects, and every unconfigured-owner mutation fail closed with 403. Every direct API attempt makes a best-effort SEL access record with only the caller, operation, and coarse reason. URL, thread id, provider text, and credentials are omitted. SEL write failure cannot weaken an authorization denial or replace the request's response or exception. Cancellation during request-body parsing or provider work is recorded as `failed/request_cancelled` when SEL is available, then the original cancellation is re-raised.

`_run_json()` emits credential-free SEL tool-invocation lifecycle events around every provider CLI attempt. Unsupported providers, invalid bounds, Windows sandbox absence, untrusted executables, and sandbox rejection record `denied`. An allowlisted command awaits its synchronous critical `invoked` append on a worker thread immediately before spawn, so an audit filesystem failure denies execution rather than launching a credential-bearing process unaudited, without blocking the gateway event loop. Cancellation while that worker is active remains fail-closed and waits for it to settle; if `invoked` landed, cleanup records `failed/request_cancelled` before re-raising and never spawns the provider. Provider launchers run in a dedicated process group, and timeout, output-overflow, and cancellation cleanup kills and reaps the complete launcher/provider tree so a sandbox wrapper cannot leave `gh` or `glab` orphaned on an unread pipe. Successful JSON decoding records `completed`; spawn, output, timeout, nonzero exit, decode, cancellation, and internal errors record `failed` with only a coarse reason. Audit records contain the logical provider (`gh`/`glab`), not argv, URL, repo path, output, environment, token, thread id, or exception text. Terminal audit failures are best effort and never alter an already-completed provider result.

Sidebar status follows the same read-only boundary. `GET /api/chat/slots` and the WebSocket handshake schedule provider refreshes and opt into cached `ci`/`state` fields only for an exact configured-owner request, or for signed `local-app`/`local-startup` dashboard subjects when no owner is configured. Generic slot serialization omits those fields. `DashboardState` tracks owner-authorized WebSockets separately, sends generic slot updates to all authenticated clients, then overlays credential-backed status only to the owner subset. This prevents a cache populated by an owner request from being replayed to a non-owner or app-token caller. Review-thread cache removal, generation advancement, and stale in-flight detachment still complete after thread ownership validation and before mutation dispatch, so cancellation cannot preserve or repopulate pre-mutation data.

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
passive probes receive a narrower environment that excludes proxy credentials
and desktop-session IPC as well as ambient cloud, Slack, SSH-agent, and
application credentials.

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
Passing `--version` never promotes a user-writable candidate into credential
access. `whoami` and device login require either an immutable system/operator
candidate or an exact path + SHA-256 attestation written after the validated
official installer completes. The attestation file is included in
`_CREW_SECRET_LEAVES`, making the trust decision unreadable and unwritable to
agent tools. Installation can attest only the exact official platform target,
and only when the installer created it or changed its digest; an unchanged or
shadowed pre-existing candidate fails closed. A POSIX operator override is
pinned to its canonical path and SHA-256 on the owning process's first
registration. The gateway registers through its prerequisite service; direct
agent-bearing CLI commands register before their jail gate or provider factory.
Mutation before credential access fails closed, and the digest is required
again while copying the private auth executable. It is rechecked before staging
credentials; POSIX auth commands then execute a private verified-byte snapshot
inside the per-call auth workspace rather than the mutable discovery pathname.
ACP launches use the same byte-binding rule: Linux executes a sealed memfd,
while macOS verifies the official Developer ID source and copies its exact
digest-checked bytes below the agent-protected `<data-home>/run` directory
before spawn. Registered snapshot descriptors and private files are released
after every ACP, usage, and model-list spawn path.
Auth commands use `mode="standard"` only with HOME/XDG/AppData redirected below
the fixed `~/.kiro/crew-auth-staging` parent. That parent is on the shared
sensitive-path floor and hidden by every agent sandbox. The separately
constructed sandbox for the provenance-verified Kiro auth process exposes that
fixed staging root, with HOME directed at its random per-call workspace, while
the complete Kiro Crew data home remains hidden. The staging root contains only
per-call workspaces and a dedicated publication lock; each workspace contains
only the allowlisted `kiro-auth-token*.json` and Kiro CLI identity SQLite files.
Login publishes
changes only after exit zero; nonzero exit, timeout, cancellation, and shutdown
discard the workspace. SQLite sidecars are consolidated through SQLite's
backup API into one atomically replaced main database rather than copied as a
mixed generation. Before publication, the gateway verifies that the live
identity generation still matches the copy taken at operation start. The check
and all allowlisted publications share one cross-process file lock, so two
gateway processes cannot both validate one generation and then overwrite each
other. Lock acquisition is required and fails the publication closed on
Windows as well as POSIX; a concurrent user login wins and the staged
generation is discarded rather than clobbering newer credentials. A matched
live identity file that cannot be captured under the bounded regular-file rules
fails both initial staging and the locked generation scan; it is never omitted
as though absent, so staged login state cannot replace it. The
temporary home is then removed;
unrelated AWS SSO/MCP cache files, SSH, GitHub, Kubernetes, and Kiro Crew state
remain hidden. Windows discovery does not trust user-writable `PATH`
entries or an explicit override because no equivalent OS sandbox protects a
passive probe: it executes only candidates under the fixed Program Files
`Kiro-Cli` tree. If an executable untrusted explicit override would shadow the fixed
candidate in ACP resolution, readiness fails without executing either binary
until the override is removed or moved under that trusted tree; a nonexistent
override is ignored just as it is by the shared resolver. The shared ACP
resolver retains inherited Windows `PATH`, the interpreter Scripts directory,
and the explicit operator override, while setup asks that resolver for fixed
passive-probe locations only. Status requests never mutate
`KIROCREW_KIRO_BIN`. Electron delegates entirely to this gateway service and
does not execute a second candidate or installer path. For each local-token
request Electron re-resolves the authoritative migrated or pinned data home,
reads exactly that home's one bootstrap secret, and sends it only to the
literal `127.0.0.1` gateway bind address; it never probes canonical and legacy
secrets across multiple loopback addresses. ACP launch independently
re-enforces executable provenance. The shared client/runtime resolver requires
the official-installer trust digest, immutable system provenance, or the
operator override digest captured by the process-start registration, and
canonicalizes symlinks before its final no-follow open. Linux copies verified
bytes into a kernel write-sealed `MFD_EXEC` memfd, passes its descriptor through
every wrapper as `/proc/self/fd/<fd>`, and closes the gateway copy after process
creation. Older kernels that reject `MFD_EXEC` with `EINVAL` retry creation
without that flag; other creation errors fail closed. Snapshot registry
ownership is removed synchronously, while descriptor close runs on the
dedicated subprocess executor so cancellation and startup teardown cannot block
the gateway event loop. Because Mach-O does not reliably launch through
`/dev/fd` and a normal drag-installed app bundle is user-owned, macOS executes
only the canonical
official app target after approved protected-installer, process-start override,
or immutable-system provenance and pinned Developer
ID/identifier/team/hardened-runtime checks succeed; explicit Kiro
classification preserves internal-sandbox delegation without relying on the
executable basename. Arbitrary unsandboxed same-user native code is outside the
enforceable in-process boundary on macOS; excluding it would require a
privileged helper or a platform installer ownership change. Windows relies on
its fixed protected Program Files tree: the ACP resolver canonicalizes and
rejects override, inherited-`PATH`, or interpreter-Scripts candidates outside
that tree before spawn. This is an operator-triggered system prerequisite,
accepts no LLM input, and is absent from the headless MCP server route set.

**App manifest permission model — advisory (`apps/permissions.py`)**: distinct from the HTTP app-token scope above, the App Kit manifest `permissions` block (`mcpTools`, `network`, `memory`) is currently **advisory, not enforced in-process**. `validate_permissions()` and `format_permissions_summary()` exist but are **not wired into the install or runtime path** — they have no callers outside `test/`, so the manifest `permissions` block is neither enforced nor even surfaced today. `check_tool_permission()` **fails open on an empty `mcpTools` allowlist** (returns `True`) and is not called at the tool-dispatch boundary, so `mcpTools` is a review/display signal rather than a runtime capability gate. (Install-time path-traversal blocking is a separate mechanism: `_check_path_safety(name)` + `manifest.validate()` in `_validate_source_path`, not the permission validator.) Real in-process enforcement (and per-resource `owner_app` ownership) is tracked in `docs/app-kit/app-sandbox-roadmap.md`; today an installed app runs with the user's full trust, confined only by the HTTP app-token scope, the OS sandbox, the `agent.apps_allow_third_party` off-switch, and destructive-command deny patterns (TRACKING).

**App admission gate (`apps/admission.py`)** (CWE-829): a contained App Kit admission decision core, gating the app install / update / enable / `register_external_app` / registry paths. It is **distinct** from the CPP-seam plugin admission engine (`platform/admission.py`), which gates signed plugin entry-points from `~/.kiro/crew/admission_policy.json`; this gate governs App Kit apps from a separate `config_dir()/app_admission.json`. The fleet-controlled policy carries a kill-switch (`banned`, always wins), a marketplace `approved` allowlist (non-empty = only-these), and an optional HMAC `require_signature` check (verified against a `trust_keys` secret the *policy* — never the app — holds, over `AppManifest.signing_payload()`). `app_admission_denied()` runs **before** the app's files are copied or its `onInstall` script runs, so a denied app never lands on disk or executes. **Fail-closed** on a present-but-unreadable policy (deny-all + `critical` SEL audit); an **absent** policy admits (interim default preserving today's no-policy behavior — the seeded-default mechanism that makes absence itself fail-closed belongs to the CPP governance seam). Asymmetric signing + trusted-publisher-key distribution + a per-app capability ceiling remain follow-on.

**Federated registry validation & refresh (`apps/routes.py`, `apps/registry.py`)**: external (federated) app registries are configured under `config.registries` (`{name, repo, branch}`) and mutated via the dashboard API. The trust-boundary contract:
- **`repo` validation** — `POST /api/apps/registries` runs every entry's `repo` through `_is_safe_repo_identifier`, which admits **either** a legacy bare name (`^[A-Za-z0-9_-]+$`, kept for companion resolution) **or** a vetted full git URL. URLs must be `https://` (`_SAFE_HTTPS_URL_RE` — plaintext `http://` is rejected, see the CWE-319 threat row) or an explicit `ssh://`/scp remote (`_SAFE_SCP_URL_RE`); shell metacharacters, `..` traversal, and `owner/repo` shorthand are rejected. When no explicit `name` is supplied, a bare name defaults to `repo` (legacy) while a URL derives a collision-safe slug via `_derive_registry_name` (host+path slug + short sha256 of the original URL) so two distinct URLs can never share an `_external_registry_cache_path` cache file. `branch` defaults to **`main`** (was `mainline`) and is validated against `^[A-Za-z0-9][A-Za-z0-9_\-./]*$` with `..` rejected.
- **Cache-key injectivity (path traversal, CWE-22/CWE-706)** — because a `repo`/registry `name` can now be a full URL, every cache path derivation (`_safe_cache_stem`, `_external_registry_cache_path`, `_blob_cache_key`) keeps pure-safe names byte-identical (existing caches stay valid) but slugifies + appends a short sha256 of the **original** name for any name carrying disallowed characters — so a hostile `../../config` entry can neither escape `_manifest_cache_dir()` nor collide with another name. `_expire_cache_file` additionally re-checks resolved containment before touching any file.
- **Refresh endpoint — `POST /api/apps/registries/refresh`** (optional body `{"repo": "<git-url-or-name>"}` to scope to one registry; omit to refresh all). Response contract: `{ok, refreshed, failed, results, apps, lastSyncedAt}` where `ok` is True only if every matched registry refetched successfully, and `results` carries per-registry outcome so the UI distinguishes "synced" from "sync failed, serving stale". The refetch is **fetch-then-swap**: `_fetch_and_cache_external_registry` overwrites a registry's cache only on a successful fetch, and manifest caches are expired by mtime-backdating rather than unlink, so a transient forge/network failure degrades to "slightly stale" instead of "apps vanished" (stale > missing). Malformed (non-dict) index items are defensively dropped before normalization so a registry returning e.g. `["oops"]` cannot escape as an HTTP 500.
- **Clone-host trust gate (SSRF + DNS-rebinding, CWE-918)** — a configured external registry's `app-registry.json` is **untrusted content**: it can list an app whose `repo` points at an internal address (e.g. `https://127.0.0.1:8443/x`) or any attacker-chosen host, and such a value passes `_is_safe_repo_identifier` and enters the blob-proxy allowlist. Because the App Store browse/refresh path clones automatically (icons, manifests, install), honoring that host would drive `git clone` against the loopback/internal network — an authenticated backend SSRF. `is_clone_host_trusted` (`apps/registry.py`) fails **closed** and constrains every URL clone to a **host** in the trust set = well-known public forges (`_PUBLIC_GIT_HOSTS`, plus any a companion contributes) **∪** the hosts of the owner's explicitly-configured registries (`_configured_registry_hosts`). It is enforced at the **three** clone chokepoints: `_fetch_git_blob` (blob/icon proxy, `apps/routes.py`), `_fetch_app_manifest` (manifest fetch, `apps/registry.py`), and `_git_clone_or_pull` (the actual clone/pull, which returns the `untrusted_clone_host` error dict). Gating on the hostname — not its re-resolvable IP — makes it **rebinding-proof**; an owner-added internal forge stays allowed precisely because the owner added it, while an index-injected host never is. This is deliberately a **host-level SSRF/rebinding defense, not a supply-chain control** — anything on a trusted forge host (e.g. all of `github.com`) is cloneable, so signature/admission gating (the App Kit admission gate above) remains the second, orthogonal layer. Bare-name legacy repos have no URL host, return `False` here, and are served by the bundled-registry allowlist rather than a URL clone. Operator-visible failure mode: an install/browse against an untrusted host fails with `untrusted_clone_host` (clone path) or a silent skip + warning log (blob/manifest paths).
  - **Host-granular trust residual → credential-free clones (confused-deputy, CWE-441/CWE-668)** — the trust gate above is deliberately **host-granular**, so a host the owner configured for one registry (e.g. their internal forge) is trusted *wholesale*. Since a registry index is untrusted content, it can list an app whose `repo` points at a *sibling* private repo on that same trusted host; the host passes `is_clone_host_trusted`, and a clone that carried the gateway's **ambient git/ssh identity** would be a confused-deputy read of a private sibling repo surfaced back through the App Store. This applies on **two** paths: the **automatic** (browse/refresh-time) `_fetch_app_manifest` / `_fetch_git_blob` clones (no owner action at all), **and** the **install** clone of an app whose registry entry came from an owner-configured *external* index (the owner clicked Install on an index-authored *name/description*, but the `repo` URL behind that button is index-controlled, not typed by the owner). Mitigation on **both** paths: the clone runs **credential-free / anonymous** via `anonymous_git_env()` (`apps/registry.py`) **plus** a forced `mode="strict"` OS sandbox (`~/.ssh` hidden). The env drops the SSH agent + `GIT_SSH`/`GIT_SSH_COMMAND` passthrough (`_GIT_CREDENTIAL_ENV_KEYS`), disables system **and** global git config (`GIT_CONFIG_NOSYSTEM=1` + `GIT_CONFIG_GLOBAL=os.devnull`, so no HTTPS credential helper fires), and forbids prompting (`GIT_TERMINAL_PROMPT=0`, batch-mode `GIT_SSH_COMMAND` with no identity/agent) — so an index-injected private-sibling repo simply fails to clone (→ graceful fallback) instead of authenticating. **Provenance decides the install-path posture**: `install_from_registry` sets `index_originated = bool(entry.get("_registry"))` — external-index entries carry the `_registry` marker (stamped when the index is fetched/cached), so they clone credential-free; **bundled/curated** registry entries (no `_registry` marker) and fetching the owner's **own** configured registry index (`_fetch_external_registry_index`, whose URL the owner typed, not index-injected) remain owner-designated and keep full credentials via `minimal_env()`. `_git_clone_or_pull` takes an `index_originated` keyword that selects the env + sandbox mode for both its fresh-clone and fast-forward-pull branches. Accepted residual: installing (or previewing the icon/manifest of) a **private** app *listed in an external index* no longer works — the correct trade, since an index-controlled URL must not be cloned with the gateway's identity; the owner can still install a private app by configuring it as their own registry (an owner-typed URL). Trust remains host-granular by design; org/path-prefix scoping is a deferred tightening, but the credential-free rule removes the exfiltration lever it would otherwise carry.
- **Untrusted index entry-name filter (path traversal, CWE-22)** — an external registry index is untrusted input, so a hostile/typo entry `name` such as `/tmp/victim` or `../../victim` would otherwise flow through `list_registry → install_from_registry → app_source_dir(name)` (which resolves `_app_sources_dir() / name`, and an absolute or traversing name escapes the app-sources root) and, on a failed clone, reach `shutil.rmtree(dest)` on the attacker-selected path. During index normalization (`apps/registry.py`) every entry name is validated against `KEBAB_RE` (`^[a-z0-9]+(?:-[a-z0-9]+)*$`, the same kebab-case gate `install`/`register_external_app` already enforce via `AppManifest`); a non-string or non-kebab name is **dropped BEFORE it is cached or listed** so it can never reach a filesystem operation. Operator-visible failure mode: the offending entry silently vanishes from the App Store and the drop is **warning-logged only** (`Dropping external registry <reg> entry with invalid name ...`) — no install error surfaces, so an operator diagnosing a "missing app" must consult the gateway log.
- **Untrusted index `subdirectory` filter (path traversal → RCE, CWE-22)** — an external index controls the entire entry, including `subdirectory`, which is joined to the throwaway manifest clone dir (`_fetch_app_manifest`), the persistent app-source dir, and the install-time app-root (`install_from_registry`). An absolute (`/etc`) or traversing (`../../victim`) value would escape those roots and let an attacker-selected `app.json` be read and its `setup.onInstall` executed with gateway privileges. Two layers close it: (1) a **lexical gate** `_is_safe_registry_subdir` (`apps/registry.py`) — rejects non-strings, NUL, backslashes, absolute paths (POSIX/drive-letter), and any `.`/`..` segment — applied during index normalization **and** on every cache read (`_read_external_registry_cache`), so an unsafe entry is **dropped BEFORE it is cached, listed, or installed** (warning-logged only, same silent-vanish failure mode as the name filter); and (2) `_contained_join(root, subdirectory)` at each use site, which resolves symlinks and returns the joined path only if it stays within `root` — catching a hostile clone that ships a symlink (`sub -> /etc`) resolving outside the clone root at read/install time (install refuses with an explicit `unsafe subdirectory ... escapes the app source root` error; manifest fetch returns `None`).
- **Trust-grant audit (`registries.host_trust_granted` SEL event + `newlyTrustedHosts`)** — admitting a new registry host is a genuine **trust grant** (its hosts feed the clone-trust set above and its apps become installable with gateway privileges), not a mere config edit, and the generic `registries.update` event does not record *which* host gained trust — leaving an unreconstructable, one-way-door audit gap. The `PUT /api/apps/registries` handler (`apps/routes.py`) diffs the incoming hosts against the **prior on-disk** config and emits a distinct per-host SEL `registries.host_trust_granted` (`resources=host=<h> repo=<url>`) for each genuinely new host; re-saving an unchanged list — or adding a second path on an already-trusted host — emits nothing, so the audit log records exactly the trust transitions. The response returns `newlyTrustedHosts` so a client can surface the grant (e.g. a UI heads-up) without another round-trip.

**Response security headers** (`server.py:_apply_security_headers`):
- All dashboard responses receive `Cache-Control: no-store`, `Content-Security-Policy` (default-src 'self' plus curated exceptions for tailwind/jsdelivr/WebSocket loopback), and `Permissions-Policy: clipboard-write=(self), clipboard-read=(self)`
- The Permissions-Policy grant is required by Chrome 143+, which changed the default policy to DENY `clipboard-write` even on secure contexts (crbug.com/414348233). Without it, `navigator.clipboard.writeText` throws a permissions-policy violation and the Copy-link button on published artifacts fails
- When the instances feature is enabled, `frame-src` is extended with `http://127.0.0.1:*`, `http://localhost:*`, and `http://*.localhost:*` so dynamically-connected tunnel ports can be framed
- Defense-in-depth framing/sniffing/referrer/transport headers are set uniformly (all via `setdefault`): CSP `frame-ancestors` (clickjacking) — `'self'` by default plus any **exact operator-trusted origins** (never a wildcard, never a hardcoded port); `X-Frame-Options: SAMEORIGIN` as a legacy backstop set **only** in the default `'self'`-only posture (omitted when an extra ancestor is trusted, since it is origin-exact and cannot express the allowlist); `X-Content-Type-Options: nosniff` (MIME confusion), `Referrer-Policy: strict-origin-when-cross-origin` (avoids leaking the token-bearing dashboard URL cross-origin), and `Strict-Transport-Security: max-age=31536000; includeSubDomains` (inert over the loopback HTTP bind, protects HTTPS tunnel/desktop access). Cross-port embedding of remote dashboards in the Instances viewport is enabled **only** via exact trusted-origin embedding carried in the signed token: at connect the local (embedding) gateway mints the remote token with an `embed_parent_port` claim equal to its own `KIROCREW_PORT`; that claim is carried through the link→session token exchange into the `mc_token_<port>` session cookie (`token_auth_middleware` — the exchange re-mints a fresh session token and must propagate the claim), and the middleware also stashes the validated port on the request **before** it revokes the link nonce. The embedded remote's `_extra_frame_ancestors` reads it in that order — the request-stashed value first (so the FIRST `?token=` framed document, whose link nonce the exchange revokes, still carries the origin), then the query token, then the session cookie (`token_embed_parent_port`) — and adds the parent's loopback origins (all loopback hosts at that port) to `frame-ancestors`. Exact origins only — never a wildcard, never a hardcoded port — and gated on a signed token, so a local page with no token can never inject an ancestor. Neither a loopback-wildcard nor the CSRF `allowed_origins` set is used for framing: both would let a local origin (any port, or a CORS/`dashboard.url`/dev `localhost:3000` entry) frame the authenticated dashboard and receive the `SameSite=Lax` session cookie (clickjacking, per input-validation guidance). Any request without such a token keeps the default `frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN` posture
- Applied via `no_cache_middleware` using `setdefault` so per-handler overrides are preserved

**CSRF protection** (`server.py` + `origin.py`):
- Validates `Origin` (with `Referer` fallback) on POST/PUT/DELETE
- Allowed origins computed once via `build_allowed_origins()` at startup: `127.0.0.1:{port}`, `localhost:{port}`, `kirocrew.localhost:{port}`, plus configured host and machine hostname when not local-only, plus `localhost:3000` in dev mode
- Shared `check_origin()` function used by both CSRF middleware and WebSocket origin check — single source of truth

**Host-header validation (DNS-rebinding defense)** (`server.py` + `origin.py`):
- `host_validation_middleware` (`server.py`) rejects any request whose `Host` header does not name a host the dashboard serves. It is registered **second** in the middleware chain (right after `host_canonical_redirect`, before `no_cache_middleware`/`csrf_middleware`/token auth)
- Runs on **every** HTTP method (not just mutating ones): a GET-based data exfiltration is the rebinding payload, and it is **independent** of the CSRF Origin check and loopback trust — a rebound request is loopback at the socket but forges `Host`
- `check_host()` (`origin.py`) compares the `Host` header (port-stripped, lower-cased) against `build_allowed_hosts()` (`origin.py`), which derives the host allowlist from the SAME `allowed_origins` set the CSRF check uses (so the two layers never drift) plus the canonical loopback names as a floor. Comparison is **port-independent** (hostname only), so an SSH-tunnel local port still matches
- **Deny-by-default**: a missing/empty `allowed_origins` is treated as a denial (never fail-open); a missing/empty `Host` is allowed **only** from a loopback `request.remote` (local IPC clients like mcp-core/doctor that omit `Host`), positively confirmed rather than blanket-allowed
- Rejects unknown Hosts with `403 Host header not allowed` + a `log_api_access` SEL event (`outcome="denied"`)

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

Permanent YOLO mode has been eliminated. All activations go through the `SafetyOverride` singleton which enforces a hard ceiling of 24 hours. The tiered TTL defaults are:

| Source | Default TTL | Max TTL |
|--------|------------|---------|
| Slack (`!yolo on`) | 30 minutes | 24 hours |
| Dashboard YOLO button | 6 hours | 24 hours |
| Config `approval_mode: "auto"` | 24 hours | 24 hours |

After expiry, re-authorization is required. A 5-minute grace window allows `!yolo renew` (Slack) or the dashboard re-auth button to extend the session without creating a new one. Outside the grace window, a fresh activation is needed.

SEL audit events are emitted on every lifecycle transition:
- `safety_override:activate` — override enabled
- `safety_override:renew` — session extended within grace window
- `safety_override:expired` — TTL reached, auto-deactivated
- `safety_override:deactivate` — manually disabled

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

Trust patterns are stored per-slot as session-scoped fnmatch globs (`slot._trusted_patterns`). Pattern matching uses the ACTUAL command from `tool_input` (not the LLM-controlled display text) for security. For non-shell MCP tools without `tool_input`, `event.title` is used as it IS the provider-controlled tool name. Multi-command titles (e.g., `cat,wc`) generate patterns for each binary.

### SEL Audit Logging (`sel.py`)

See `docs/system-specs/modules/sel.md` for full spec. Integrated across 8 surfaces: Slack handler, dashboard chat, task runner, subagent, background tasks, MCP core, MCP cron, API middleware.

**What counts as an auditable permission decision.** A SEL event is emitted when a decision has a *subject* — a tool/capability that was granted or denied. The audit records grants and denies, not the absence of any decision:

- **Skill triggering** (`skills.py:get_triggered_skills`, runs per message) emits **one** event per call when at least one skill was injected (`outcome="triggered"`, grant) or actively excluded by a negative trigger that would otherwise have matched (`outcome="denied"`, with the excluded skills in `metadata.negated`). When no skill matched and none was negated — the overwhelmingly common case — **no event is emitted**: nothing was granted or injected into LLM context, so there is no permission decision with a subject to record (analogous to not auditing an authz check that had nothing to authorize). This is a deliberate, threat-model-reviewed choice: the prior per-skill "not_triggered" logging was a per-message synchronous-write hot-path cost, and a per-message "matched nothing" event would dwarf the real grant/deny signals and *reduce* the audit trail's usefulness rather than improve it. The message text is already captured in conversation history; skill names are not secret.

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
