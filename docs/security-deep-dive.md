# Security Deep Dive

Defense-in-depth security architecture across all KiroCrew layers.

## Threat Model

KiroCrew runs an LLM agent with filesystem and shell access. The primary threat is **Cross-Plugin Injection Attack (XPIA)** — a malicious prompt embedded in content the LLM reads (web pages, files, Slack messages) that tricks it into exfiltrating credentials or executing destructive commands.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Defense-in-Depth Layers                      │
│                                                                 │
│  Layer 5: Audit ──────── SEL event logging on all tool calls    │
│  Layer 4: Output ─────── Credential redaction + URL scanning    │
│  Layer 3: Validation ─── MCP input schemas + length limits      │
│  Layer 2: Command ────── 113 denied patterns + 55 suspicious    │
│  Layer 1: Filesystem ─── Hook-layer path blocking               │
│  Layer 0: OS Sandbox ─── Namespace/Seatbelt process isolation   │
│                                                                 │
│  Cross-cutting: Auth (Slack owner lock + dashboard tokens)      │
│  Cross-cutting: Enterprise Grid workspace validation            │
└─────────────────────────────────────────────────────────────────┘
```

## Layer 0: OS-Level Sandbox (`sandbox.py`)

Hides credential paths from the kiro-cli subprocess tree using platform-native isolation. The parent KiroCrew process is unaffected — only agent subprocesses are sandboxed.

### How It Works

**Linux** — user + mount namespaces:
1. Fork child process
2. Child calls `unshare(CLONE_NEWUSER)` — new user namespace
3. Parent writes identity UID/GID map to `/proc/<child>/{uid_map,gid_map}`
4. Child calls `unshare(CLONE_NEWNS)` — new mount namespace
5. Sets mount propagation private (`MS_REC|MS_PRIVATE`)
6. Bind-mounts empty dirs over credential paths (per mode)
7. Scrubs sensitive env vars
8. Execs the agent binary

Two-pipe synchronization ensures correct ordering. The child retains the real UID/GID so toolchains (JVM, Gradle, npm, build tools) work without workarounds.

**macOS** — Seatbelt sandbox:
- `sandbox-exec` with a generated Seatbelt profile that denies file reads on credential paths
- Same env var scrubbing as Linux

### Sandbox Modes

| Mode | Config | Hidden Paths | Accessible | Env Scrub |
|------|--------|-------------|------------|-----------|
| Standard | `"auto"` (default) | `.gnupg`, `.gpg`, `.config/gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` | `AWS_SECRET*`, `AWS_SESSION*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GIT_ASKPASS` |
| Strict | `"strict"` | All above + `.aws`, `.ssh`, `.kube` | Only `~/.ssh/known_hosts` | Same |
| Off | `"off"` | Nothing | Everything | Nothing |

Config: `agent.sandbox` in `~/.kirocrew/config.json`.

The env scrub additionally strips `PYTHONPATH`/`PYTHONHOME` (`strip_python_env=True`), but **only** on the foreign kiro-cli/agent spawn path — never for KiroCrew's own sandboxed Python children, which import `kiro_crew` via `PYTHONPATH` and would break if it were stripped. This isolates the foreign process from KiroCrew's `PYTHONPATH`, which would otherwise leak in and shadow the agent's (and its MCP servers') own dependencies.

### Why Standard Mode Is Safe

Protection depth depends on the access path:

- **kiro-cli tool reads** (primary attack path): two layers protect `.aws`/`.ssh` — denied command patterns block `cat`/`head`/`tail`/`python open()` on those paths, and output redaction (`redact_credentials()`) catches any credential patterns that leak through tool output.
- **Non-tool reads** (KiroCrew's own file operations): a third hook layer (`safe_read_file()` → `is_sensitive_path()`) blocks reads before they reach the filesystem.

Standard mode allows git-over-SSH via key files, AWS CLI via `credential_process`, and kubectl. Note: `SSH_AUTH_SOCK` is scrubbed in both Standard and Strict modes, so ssh-agent forwarding is unavailable unless the sandbox is set to `"off"`. Users relying on passphrase-protected keys or hardware tokens must either use unencrypted key files directly or set `agent.sandbox` to `"off"`.

### Wrapper-Shim Compatibility

On some installs the agent backend (`kiro-cli`) is a bash shim that re-execs the real binary through a launcher, which can fail inside a user namespace. `_resolve_real_kiro_bin()` bypasses the shim by resolving the real ELF binary (magic byte check rejects shell scripts).

## Layer 1: Filesystem Protection (`security.py` + `hooks.py`)

### Sensitive Path Blocking

`is_sensitive_path(path)` resolves and checks against 13 sensitive directories:

```
~/.aws, ~/.ssh, ~/.gnupg, ~/.gpg, ~/.config/gcloud, ~/.azure,
~/.docker/config.json, ~/.kube/config, ~/.npmrc, ~/.pypirc,
~/.netrc, ~/.git-credentials, ~/.kirocrew/.env
```

### Sensitive Bash Command Detection

`is_sensitive_bash_command(cmd)` regex-matches commands that read credential files:
- `cat`, `head`, `tail`, `less`, `cp`, `scp` targeting sensitive paths
- `python open()` / `python -c` targeting sensitive paths
- Pipe redirects from sensitive paths

### How the Hook Layer Works

`hooks.py:safe_read_file(path)` is the central guarded file read. All file reads outside kiro-cli tool calls go through this function:

```python
def safe_read_file(path: str) -> str:
    resolved = Path(path).expanduser().resolve()
    if is_sensitive_path(str(resolved)):
        raise PermissionError(f"Access denied: {path}")
    return resolved.read_text()
```

**Audited internal carve-out.** `safe_read_file_internal(read_id)` permits a small, hardcoded
allowlist of system-internal reads of otherwise-sensitive paths (today only the kiro-cli SSO
token, read to call the CodeWhisperer `GetUsageLimits` API that powers the dashboard credit
pill). It re-verifies `is_sensitive_path()`, SEL-audits every outcome, and fails closed — a
`success` whose audit cannot be persisted synchronously returns `None`. The token bytes go
only to the hardcoded prod AWS endpoint over TLS (verify on, redirects off) and never reach an
LLM/agent surface; the parsed numeric result is credential-redacted before caching.

## Layer 2: Command Denial (`security.py`)

### Denied Commands (137 built-in rules, default-on, user-disableable)

First-class `DeniedCommandRule` records in `BUILTIN_DENIED_RULES` (`security.py`) —
each a stable `id`, a Python regex `pattern`, a `category`, and a human
`description` — blocking:

**Credential exfiltration:**
- `echo $AWS_SECRET*`, `printenv AWS*`, `env | grep AWS`
- `python boto3.get_credentials`, `python botocore.credentials`
- `curl 169.254.169.254` (IMDS metadata endpoint)
- `curl $AWS_SECRET*` (credential exfil via HTTP)
- `aws s3 cp/mv/sync . s3://` (file upload exfil)
- `cat ~/.aws/*`, `cat ~/.ssh/*` (direct reads)

**Destructive operations:**
- `rm -rf /`, `rm -rf ~`, `git push` to a protected branch (`main`/`mainline` plus the legacy Git default-branch name — force-push to a feature branch stays allowed; `--mirror`/`--all` denied; each push segment of a compound command is validated)
- `aws * delete-*`, `aws ec2 terminate-instances`
- `cdk destroy`, `terraform destroy`
- `DROP TABLE`, `DROP DATABASE`

**Enforcement**: hooks-gate-only. The rules are evaluated at KiroCrew's own
PreToolUse gate (`HookManager.on_tool_call` → `PolicyAuthority.is_denied`), NEVER
injected into a kiro agent config. The former `_enforce_denied_commands()`
agent-config injection + the ~60s re-enforce loop are retired, so a kiro agent
config that edits or omits its own deny list cannot weaken KiroCrew's ceiling.

**User opt-out (Settings > Security)**: built-ins are default-ON but
user-DISABLEABLE — a "disable all" toggle, per-rule toggles, and an add-your-own
field for custom deny patterns. Opt-out state (`disable_all` / `disabled_ids` /
`user_added`) persists in its own **keystone** file
`~/.kirocrew/denied_commands.json`; a disable requires a confirm-modal ack + SEL
audit. Because that state IS a security ceiling, the file lives on
`_SENSITIVE_HOME_DIRS` (full read+write block, same floor as
`security_policy.json`) — the agent can neither read nor write its own deny
ceiling via ANY shell form, so an auto-approved/YOLO shell cannot persist
`disable_all=true` and defeat the ceiling. Only the operator edits it out-of-band
via the dashboard `/api/security/…` endpoints (which open the file directly, not
through the agent tool gate). An enterprise **governance** `commands`-scope
policy (Level-1 `security_policy.json`) or a Level-2 profile can force-pin a rule
as un-opt-out-able (tightest-wins).

The linear-time `_DenyMatcher` (ReDoS-safe fragment split, with a bounded
whole-regex fallback for top-level alternation or greedy-fragment patterns)
evaluates each pattern against the full command so a needle at any offset is
found without catastrophic backtracking.

### Per-Segment Deny Pattern Evaluation (`security.py`)

The `is_denied()` function uses a two-pass evaluation algorithm to balance security (blocking chaining-bypass attacks) with usability (allowing legitimate piped workflows):

**Pass 1 (whole-string deny):** Every deny pattern is matched against the full input. If a pattern matches and no exception pattern also matches the full input, the command is denied outright. This catches evasion vectors that span separator boundaries (e.g., `git$(echo ' ')push origin main`).

**Pass 2 (per-segment evaluation):** Only runs when pass 1 found a deny match AND a matching exception exists. The input is split on shell separators (`;`, `&&`, `||`, `|`, `&`, `$(`, `)`, backticks, newlines) into independent segments. Each segment is re-evaluated against deny patterns + exceptions.

**Threat model preserved:**
- Real `git push` (any form): BLOCKED
- `git push` chained after `stash` via `;`/`&&`/`||`/`|`: BLOCKED (embedded publish is its own segment)
- `git push` via subshell (`$(...)` or backtick): BLOCKED (subshell content is its own segment)
- Path containing "stash" as directory with real push: BLOCKED (exception requires literal ` stash push` with leading space)

**New permissive behavior:**
- `git stash push | tail -3`: ALLOWED (stash exception in segment 1, tail in segment 2 is deny-free)
- `git stash push && git status`: ALLOWED
- `git stash push; git rebase origin/main`: ALLOWED

**Audit:** Every denial emits a `deny_event` SEL event. Every exception grant emits a `deny_exception` SEL event (fail-closed: if SEL logging fails, the exception is not granted).

### Suspicious Bash Patterns (55 patterns)

`SUSPICIOUS_BASH_PATTERNS` checked by `audit_bash_command()` at tool invocation time:

- **Deletion**: `find * -delete`, `xargs rm`, `shred`, `truncate`, `rm -rf /`
- **Exfiltration**: `curl -d @file`, `wget --post-file`, `nc < file`
- **Pipe execution**: `| bash`, `| sh`, `| python`, `| perl`

## Layer 3: Input/Output Validation (`validation.py`)

Centralized validation for all 12 MCP tool handlers:

| Control | Implementation |
|---------|---------------|
| Type-safe schemas | `FieldSpec` + `ToolSchema` declarative validation |
| Unicode normalization | NFC + hidden character stripping (control chars, format chars, private use, surrogates) |
| Allow-lists | Enum enforcement for lesson categories, cron schedule kinds |
| Regex patterns | Agent name, job ID format validation |
| Range checks | Positive numbers for timeouts/intervals, valid timestamps |
| Length limits | Tool names (64), short strings (500), medium (5K), long (50K) |
| Unknown field rejection | Rejects unexpected fields in tool inputs |
| Response truncation | 100K char limit prevents DoS from unbounded tool output |

## Layer 4: Output Scanning

### Credential Redaction (`redact_credentials`)

Scans for plaintext AND base64-encoded credentials:
- `AKIA`/`ASIA` access key IDs
- `SecretAccessKey=`, `aws_secret_access_key=`
- `SessionToken=`, `aws_session_token=`
- Private key headers (`BEGIN RSA/DSA/EC/OPENSSH PRIVATE KEY`)
- Slack tokens (`xoxb-`/`xoxp-`)
- **Third-party provider families**: GitHub (`ghp_`/`gho_`/… + `github_pat_`), GitLab (`glpat-`), Stripe (`sk_live`/`rk_live`/`sk_test`/`rk_test`), SendGrid (`SG.`), OpenAI (`sk-proj-`), Anthropic (`sk-ant-`), npm (`npm_`), PyPI (`pypi-`), DigitalOcean (`do*_v1_`), Google OAuth (`GOCSPX-`)
- **DB connection URIs** with embedded credentials — the `scheme://user:pass@` prefix for `postgres`/`postgresql`, `mysql`, `mongodb`(`+srv`), `redis`(`s`), and `amqp`(`s`) schemes

**JSON-aware key-value matching**: Key-value patterns allow an optional quote (`[\"']?`) between the key name and separator (`[:=]`), matching both bare `key=VALUE` and JSON `"key": "VALUE"` formats. The value class uses `[^\s"',}]+` (bounded, stops at JSON structural delimiters) rather than greedy `\S+`, preventing over-capture in compact JSON that would swallow adjacent fields and mask subsequent credentials.

Base64 detection: finds 40+ char base64 chunks, decodes, checks if decoded content matches any credential pattern.

Applied on ALL 5 output paths:
1. Dashboard streaming (mid-flush)
2. Dashboard streaming (trailing)
3. Dashboard non-chunk messages
4. Dashboard history save (JSONL)
5. Slack final response

The `redact()` dual-pass helper composes both scanners in order (`redact_exfiltration_urls()` then `redact_credentials()`) for a single call site.

### Streaming Redaction (`StreamRedactor`)

Per-chunk redaction misses a credential split across token/streaming boundaries: a chunk ending `...AKIA` and the next starting `IOSFODNN7...` each individually escape `redact_credentials()`, so the raw fragments reach WebSocket/SSE/Slack consumers even though the final assembled message would have been redacted. `StreamRedactor` is a rolling-buffer redactor that feeds all streamed output: it withholds the trailing run of credential-class characters (letters, digits, and URL/base64/connection-string punctuation — the possible start of a not-yet-complete credential) until a non-credential-class terminator arrives or the stream ends, redacting only the confirmed-safe prefix before it is emitted on the wire. The hold-back is bounded at 512 chars so latency/memory stay bounded on a pathologically long unbroken run.

### URL Exfiltration Detection (`scan_exfiltration_urls`)

Domain-agnostic — flags the payload, not the destination:
- Long query strings (≥200 chars)
- Base64 blobs (40+ chars)
- Heavy URL-encoding
- AWS access key IDs in URLs
- SSH keys, private key headers, Slack tokens in URLs

Suspicious URLs replaced with `[REDACTED: suspicious URL to {domain}]`.

## Layer 5: Audit Logging (SEL)

Security Event Log — immutable audit trail integrated across 8 surfaces:
- Slack handler, dashboard chat, task runner, subagent
- Background tasks, MCP core, MCP cron, API middleware

All string fields redacted via `redact()` before forwarding to centralized log integration.

## Authentication & Authorization

### Slack Owner Lock (Deny-by-Default)

5 defense-in-depth layers:
1. `_init_socket_mode()` refuses to connect if `KIROCREW_OWNER_ID` unset
2. `_on_event()` rejects all messages when owner ID missing
3. `conversations.info` DM gate for Trust/YOLO actions
4. Trust/YOLO buttons suppressed in group channels
5. Safety override (YOLO) time-limited: Slack 30min, dashboard 6h, config 24h (no permanent mode)
6. Re-authorization required after expiry (5-minute grace window for renewal)
7. Fleet governance: `/api/status` reports `yolo_active`/`yolo_expires_at`; `/api/admin/compliance/yolo-status` provides full override status
8. SEL audit on every lifecycle event: `safety_override:activate`, `safety_override:renew`, `safety_override:expired`, `safety_override:deactivate`

### Challenge-and-Redirect (Slack) — REMOVED

Earlier builds gated inbound Slack messages behind a "challenge-and-redirect"
flow: every message that would reach the agent was intercepted and turned into
a presigned dashboard-session link, denying inline processing by default. This
was an **Amazon-internal-only** security posture and has been **removed** for
external/open-source usage.

Slack messages are now processed **inline** and reach the agent directly, gated
by the user allowlist (`is_allowed_user`) and the Enterprise Grid origin check.
The `send_channel_challenge()` helper and the `_CHALLENGE_REDIRECT_ENABLED` gate
no longer exist. The generic signed-token helpers in `token_auth.py`
(`generate_token`, `extract_claims_from_token`) remain and back the explicit
`/kirocrew dashboard` link command.

### 3-Tier Interactive Trust Escalation

Dashboard tool approval prompts offer three trust levels for user control over auto-approval scope:

| Level | Action | Scope | Example |
|-------|--------|-------|---------|
| 1 | Trust this command | Session, exact match | `ls /tmp` |
| 2 | Trust this tool | Session, base glob | `ls *` (any args) |
| 3 | YOLO | Global, time-limited | All tools, all slots |

Trust patterns are session-scoped fnmatch globs stored per-slot. Security: matching uses the ACTUAL command from `tool_input` (not LLM-controlled display text). Multi-command titles generate patterns for each binary.

### Dashboard Token Auth

HMAC-SHA256 signed tokens with dual expiry:
- 5-minute link click window (`exp`)
- Session TTL up to 20 hours (`session_exp`)
- IP-pinned on first use
- **Every request requires a valid token** — there is no unauthenticated path.

Additional controls:
- **Per-session logout** (CWE-613): the access cookie carries a per-session `nonce`; `POST /api/auth/logout` records that nonce in the `RevokedNonceStore` so the individual session is revoked without affecting others.
- **App-token scope** (CWE-269, least privilege): app tokens are confined to their declared per-app API allowlist via `_enforce_app_scope()` — out-of-scope paths return 403, deny-by-default even on internal paths.
- **Refresh cookie**: a path-restricted refresh token authenticates `POST /api/auth/refresh` (and `/api/auth/logout`), letting the app self-recover after the access cookie expires.
- **Secure flag**: the auth cookies set `Secure` when `is_https_request()` detects the gateway is behind a TLS/HTTPS-terminating tunnel.

### CSRF Protection

Origin/Referer validation on POST/PUT/DELETE. Shared `check_origin()` for both HTTP middleware and WebSocket.

### Host-Header Validation (DNS-Rebinding Defense)

A parallel `Host`-header barrier runs alongside the Origin check as the second middleware in the chain (`host_validation_middleware`, registered before `csrf_middleware`). Unlike CSRF, it runs on **every** method — GET-based data exfiltration is the DNS-rebinding payload, and it does **not** trust a loopback `request.remote` (a rebound request *is* loopback at the socket while `Host` carries the attacker's forged domain). `check_host()` derives a hostname allowlist from `app['allowed_origins']` plus a canonical-loopback floor via `build_allowed_hosts()`, so the Host and Origin layers share one source of truth and cannot drift. The comparison is port-independent (hostname only). It is deny-by-default: an empty/missing `allowed_origins` is treated as a denial (never fail-open), and a missing `Host` is allowed only from a loopback remote (non-browser local IPC). On rejection it returns 403 and emits a SEL audit event.

### Enterprise Grid Validation

Optional two-layer defense against data exfiltration to personal/external Slack workspaces. Default-open: with no `slack.allowed_enterprise_ids` configured, all workspaces the bot can reach are allowed.
1. **Startup**: `auth.test` records the bot's workspace `team_id`; on Enterprise Grid it also captures the org-level enterprise ID. Operators may restrict the bot to specific workspaces via the `slack.allowed_enterprise_ids` allowlist.
2. **Per-message**: compares event `team` field against the cached/allowlisted IDs.

## Observe Mode Context Isolation

`channel_history.push` in observe-mode channels is gated on `_user_authorized`. Only messages from the owner or allowlisted users are recorded. This prevents non-owner messages from influencing LLM context via prompt injection through shared channel traffic.

## Frontend Security

| Control | Implementation |
|---------|---------------|
| XSS prevention | DOMPurify on all HTML content |
| Safe DOM APIs | `createElement` + `textContent` for error fallbacks |
| Mermaid sandboxing | `securityLevel: 'strict'` — iframe sandbox |
| No `innerHTML` | React text children instead of HTML string construction |
| No regex URL linkification | React elements via `.split()` |

## Credential File Permissions

`load_credentials()` enforces `chmod 600` on `~/.kirocrew/.env` at load time. Too-open permissions are tightened automatically.

---

## Gaps & Suggested Features

### Gap 1: No Network Egress Control
**Problem**: The sandbox hides credential files but doesn't restrict network access. A compromised agent could `curl` data to an external server using non-credential data.
**Suggestion**: Add optional network namespace isolation (Linux) or outbound firewall rules. Allow-list trusted domains (e.g. your LLM provider and any internal services) and block all other egress.

### Gap 2: No cgroups/ulimits for Resource Isolation
**Problem**: Agent subprocesses can consume unlimited CPU/memory. A runaway process can OOM the host (documented in `resource-protection.md` as known gap).
**Suggestion**: Add cgroup v2 limits for agent subprocesses: memory cap (e.g., 4GB), CPU quota, PID limit. Configurable via `agent.resource_limits` in config.

### Gap 3: Runtime Integrity of the Deny List — CLOSED
**Was**: If an attacker modified `agents/defaults.json` to remove denied commands, there was no detection mechanism.
**Now**: Denied commands are no longer injected into any agent config. They are first-class `BUILTIN_DENIED_RULES` in `security.py`, enforced solely at KiroCrew's PreToolUse gate, so editing/removing a kiro agent config's own deny list cannot weaken the ceiling. The user opt-out lives in the keystone `~/.kirocrew/denied_commands.json` (on the `_SENSITIVE_HOME_DIRS` read+write floor, so the agent can neither read nor write its own ceiling — same protection as `security_policy.json`), and an enterprise governance `commands`-scope pin is un-opt-out-able. The remaining residual is the deny *set contents* being code-defined (updated via release), not runtime-verified against a signed manifest — a much smaller surface than the former mutable-agent-config vector.

### Gap 4: Denied Command Patterns Are Regex-Based
**Problem**: Regex patterns can be bypassed with creative shell tricks (e.g., `c"a"t ~/.aws/credentials`, `$(echo cat) ~/.ssh/id_rsa`, variable expansion).
**Suggestion**: Add AST-level bash parsing (e.g., `bashlex`) to normalize commands before pattern matching. Alternatively, use a shell wrapper that intercepts `execve` syscalls.

### Gap 5: No Audit Dashboard
**Problem**: SEL events are logged but there's no UI to browse, search, or alert on security events.
**Suggestion**: Add a Security tab in the dashboard with: event timeline, filter by severity/operation, anomaly detection (e.g., spike in denied commands), export to SIEM.

### Gap 6: No Sandbox Escape Detection
**Problem**: If the sandbox fails (e.g., namespace setup error), the agent runs unsandboxed with only a log warning.
**Suggestion**: Add a health check that verifies sandbox is active from within the agent process (e.g., try to read a canary file that should be hidden). Fail-closed: refuse to start agent if sandbox verification fails.

### Gap 7: Base64 Credential Detection Has Blind Spots
**Problem**: `redact_credentials()` only checks base64 chunks ≥40 chars. Shorter encoded fragments or split-across-messages exfiltration could bypass detection.
**Suggestion**: Add cross-message correlation — track partial credential patterns across consecutive messages. Add entropy-based detection for high-entropy strings that may be encoded credentials.

### Gap 8: No File Write Auditing
**Problem**: The agent can write arbitrary files. While reads are guarded, writes to sensitive locations (e.g., `~/.bashrc`, `~/.ssh/authorized_keys`) are not blocked.
**Suggestion**: Add write-path protection: deny writes to sensitive directories, require explicit user approval for writes outside the workspace directory.
