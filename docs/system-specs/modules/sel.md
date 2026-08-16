# Security Event Log (SEL) Module

## Overview

Immutable, tamper-evident audit trail for all tool invocations, MCP calls, and dashboard API mutations. Implements transactional event logging per Amazon Security Event Logging Standard.

See also the SEL section in [`security.md`](security.md) for the threat-model view of these events.

Storage: `~/.kiro/crew/security_events.jsonl` (append-only JSONL with HMAC-SHA256 chain), plus sealed rotation segments and a sticky eviction marker in the `~/.kiro/crew/sel/` subdirectory (see Rotation).

## Event Schema

Each entry records:

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex identifier |
| `timestamp` | ISO 8601 UTC |
| `event_type` | `tool_invocation`, `api_access`, `config_bounds_clamped`, `governance_decision`, `governance_degraded` |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`, `subagent:123`). API-access events from mixed-internal endpoints that validate `X-Internal-Caller` (the chat folder writes) carry the internal caller's declared **component name** here — e.g. `kirocrew-dashboard`, or `unknown-internal` for an authenticated internal caller that declared no recognized name (a defined, warned state, not log corruption); `source` stays in the interface vocabulary (`mcp`) for those events |
| `agent` | Agent name (`kirocrew`, custom agent name) |
| `source` | Interface: `slack`, `dashboard`, `cli`, `cron`, `subagent`, `taskrunner`, `mcp`, `background`, `acp` (ACP-transport events, e.g. `tool_interrupted`), `token_auth` / `refresh_tokens` (dashboard auth), `host` (the `_host` sentinel — an in-process host action like app activation / workspace admission), `unknown` (empty/unrecognized session key, which must NOT be mis-tagged `slack`). This is a closed interface vocabulary — component attribution does not extend it; see `caller` below |
| `operation` | Tool name or `METHOD /api/path` |
| `tool_kind` | Tool category (`execute_bash`, `fs_write`, `mcp_core`, `mcp_cron`, etc.) |
| `outcome` | `invoked`, `auto_approved`, `approved`, `rejected`, `denied`, `completed`, `failed`, `clamped`, `degraded` (a governance chokepoint failed OPEN) |
| `resources` | Affected resources summary (truncated to 500 chars) |
| `downstream_service` | MCP server name if applicable (`kirocrew-core`, `kirocrew-cron`, `internal-mcp`) |
| `request_id` | ACP permission request ID |
| `error` | Error message if failed/denied |
| `prev_hash` | HMAC of previous entry (chain link) |
| `entry_hash` | HMAC-SHA256 of this entry |
| `metadata` | Additional context (approval reason, step index, etc.) |

The `config_bounds_clamped` event (`outcome=clamped`, `source=background`, `operation=config.load`, `caller_identity=config_loader`) is emitted by `config/loader.py`'s `_log_config_clamp_event` when an out-of-range security-bounded knob (`agent.subagent_auto_max` / `agent.max_subagents` / `agent.subagent_max_turns` / `session.pool_size`) is clamped to its API-enforced ceiling at load time, recording `metadata` `{file_value, clamped_to, min, max}`. Best-effort: a SEL failure never makes config loading raise.

## Integrity

- HMAC-SHA256 chain: each entry signs over the previous entry's hash
- HMAC key: `~/.kiro/crew/trust/sel_hmac.key` (32 random bytes, `chmod 600`, inside an owner-only `trust/` dir, `chmod 700`). The key deliberately lives OUTSIDE the log's directory: an actor who can rewrite the log dir must not also be able to read the key and re-sign a clean-looking chain that `verify_integrity()` accepts. A legacy key at `~/.kiro/crew/sel_hmac.key` is migrated in atomically (`os.replace`) on the next `SecurityEventLog` init — same key bytes, so every existing chain still verifies without re-signing; if BOTH paths exist, the LEGACY key wins and overwrites the destination: `trust/` was not deny-listed before the migration release, so a file already at the destination on a legacy install is untrustworthy (agent-plantable), while the legacy leaf was deny-list-protected all along; a linked `trust` dir or key file is likewise removed (link only, never its target) before use. `sel_hmac_key_path()` reports the RESOLVED path in use, so `session_pid_sig` follows automatically. A read-only config dir that blocks creating `trust/` keeps signing with the legacy file (chain continuity beats relocation). Rollback caveat: downgrading the binary after migration mints a fresh key at the legacy path; on re-upgrade that legacy key wins and replaces the trust-dir key, so chain entries signed BEFORE the downgrade will no longer verify — restore a consistent key from backup around a downgrade if chain continuity matters.
- **Key + log are on the sensitive-path floor (`cdf82704`):** the `trust` directory (whole-dir entry), the legacy `sel_hmac.key` leaf, and `security_events.jsonl` are in `security._SENSITIVE_HOME_DIRS`, so the audited agent's `fs_read`/file-edit tools (gated by `is_sensitive_path()`) cannot read the key to forge the chain or rewrite the log. The gateway's own writer/reader (`sel.py`, `dashboard/session_health.py`) opens the files directly and bypasses that gate. Residual: the key still lives in the agent's home namespace — a deeper out-of-process signer is future hardening.
- **Sealed segments share the floor:** they live in `~/.kiro/crew/sel/`, which is a `security._CREW_SECRET_LEAVES` entry like any other trust root, so `is_sensitive_path()` and every bash matcher block the whole directory. Because the gate treats a registered path as a subtree, this covers a segment number no release has ever emitted the moment it is created — a property the previous dot-suffixed layout could not have, since every matcher there had to be taught a prefix-family regex and each consumer could only ever cover the sibling names that already existed. The entry is derived from `_CREW_HOME_PREFIXES`, the same mechanism the other leaves use, so it covers the current home, the legacy `~/.kirocrew` home, and a custom `KIROCREW_HOME` without a hardcoded literal.
- Verification: `verify_integrity()` walks the chain across ALL segments (oldest sealed → active) and reports tampered entries as `valid<total`
- Append-only: no in-place edits; pruning rewrites with chain rebuild
- **Second protocol anchored on this key — domain-separated:** `session_pid_sig.py`
  authenticates the `session_pid_<pid>.txt` -> session-key mapping consumed by
  strict MCP identity resolvers. It does **not** sign with the raw
  `sel_hmac.key`; it derives a purpose-specific subkey
  (`HMAC(sel_hmac.key, "kirocrew.session_pid.sig.v1")`) so the sidecar MAC and
  the SEL audit chain never share a signing key — a MAC minted under one
  protocol is valueless to the other (no cross-protocol confusion/replay). The
  key file remains a single on-disk trust root; only `SecurityEventLog` ever
  *creates* it. **Recorded acceptance — widened compromise impact:** anchoring
  session identity here means compromise of `sel_hmac.key` no longer only
  permits forging the audit chain — it also permits minting valid
  session-identity sidecars and driving state-mutating MCP tools against
  another session (cross-session state mutation). The likelihood of compromise
  is unchanged (same sensitive-path floor); the *impact* grew, and any future
  hardening of this key (the out-of-process signer above, issue #302) must
  treat `session_pid_sig` as a dependent of equal weight. See
  `docs/system-specs/modules/session.md` for the sidecar contract.

## Rotation

The active file is size-bounded. When it exceeds `max_bytes` it is sealed into
`sel/security_events.jsonl.<N>`, where `N` is the next unused number. Numbers are
**monotonic only while a sealed segment survives**: `_next_segment_index` computes
`max(existing)+1` and falls back to `1` on an empty set, so once the last segment
is pruned the next seal REUSES a number. While segments do exist a higher number
is therefore a NEWER segment, and eviction deletes the LOWEST numbers once more than
`backup_count` of them exist — which also covers an operator lowering
`backup_count` between runs, since the whole excess prefix goes in one pass.
Deleting a segment leaves a permanent gap in the numbering, and a gap means only
"older history aged out". `backup_count=0` keeps no sealed history: on roll it
discards the active file AND any pre-existing segments, then re-anchors the chain
to genesis so verify stays clean. Note that
`backup_count=0` drops up to `max_bytes` of the MOST RECENT events at the
rotation boundary, not just long-tail history — keep `backup_count>=1` to retain
a sealed tail. That contract covers the loss of recent EVENTS and nothing more, so
the discard's ORDER is load-bearing: sealed segments are removed FIRST, then the
active file, then the tip is re-anchored with nothing fallible in between.
`missing_ok=True` suppresses only `FileNotFoundError`, so a sealed unlink failing
for any other reason — a Windows sharing violation against a `recent()` call
holding that segment open, since `recent()` opens segments OUTSIDE `_lock` while
the discard runs under it — propagates. Active-file-first would leave that failure
with the events gone, the sealed segments still present and the tip still naming a
deleted entry, which is a broken CHAIN rather than the documented loss of recent
events. Sealed-first means the same failure aborts with every file and the tip
untouched, and `_flush_batch` degrades to "appending without rotating" for that
cycle. `max_bytes=0` disables rotation (byte-for-byte legacy
append-only). The cap is soft: rotation is checked per write batch, so a sealed
segment can overshoot `max_bytes` by up to one batch before it rolls.

Defaults are `max_bytes` 100 MB, `backup_count` 5, `retention_days` 365 (about
600 MB of audit history at the defaults, versus unbounded before). They are
**operator-settable per host through the environment** — `KIROCREW_SEL_MAX_BYTES`,
`KIROCREW_SEL_BACKUP_COUNT`, `KIROCREW_SEL_RETENTION_DAYS` — falling back to this
module's constants when unset; see "Retention" below for the binding and
malformed-value rules. What remains a follow-up is the *config section*:
`KiroCrewConfig` has no `sel` section, so `sel.py` reads no config at all and a
lookup would be both a static type error and a branch that could never execute
here. Adding it means the loader-side clamps plus wiring into the one
knob-resolution block in `_init_locked`. There are no constructor kwargs — the
knobs are plain instance attributes, which is all the tests need.

`sel.py` normalizes the three knobs itself rather than trusting a caller: a
negative value reads as "disabled" (`max_bytes<=0` disables rotation,
`retention_days<=0` disables age pruning) instead of flowing into the size and
cutoff comparisons as a negative. That floor is the module's own and stays
correct independently of any future loader clamp. There is deliberately no
*upper* floor on `max_bytes` — the tests rely on small caps to exercise rotation
cheaply — and a large `backup_count` cannot wedge a rotation because
`_maybe_rotate` iterates the segments actually present on disk rather than
`range(backup_count)`.

- **One continuous HMAC chain across the seam.** Rotation does NOT re-anchor
  `_last_hash`; the fresh active file's first entry chains off the just-sealed
  segment's tip, so `verify_integrity()`/`recent()` read every segment
  oldest→newest as one stream and validate unbroken across rotation.
- **Eviction seam.** Once retention or `backup_count` evicts the genesis prefix,
  the oldest surviving entry's `prev_hash` references an evicted entry. verify
  adopts that entry's `prev_hash` as the baseline (its own self-HMAC is still
  checked) so a rotated host doesn't false-FAIL. The relaxation is gated ONLY on
  the sticky `sel/evicted` marker — written on every real
  eviction (size-cap overflow, age-drop, AND the `backup_count=0` prefix discard),
  cleared on a `backup_count=0` genesis
  re-anchor — NOT on mere segment existence. The discard path was the gap: it
  unlinks sealed segments in ascending order, so a raise part way through leaves an
  evicted prefix with the file it could not delete still on disk, and it marked
  nothing — so verify enforced the genesis anchor against a survivor whose
  `prev_hash` named an entry that path had itself deleted. Measured before the fix,
  refusing the unlink of the newest of 7 sealed segments: 6 deleted, no marker,
  `total=2 valid=1`. The marker is now written from inside that loop on the FIRST
  successful deletion, and only then — an empty sealed list or a raise on the first
  unlink evicts nothing and must not mark, since marking would hand over the
  relaxation for free. A rotated-but-never-evicted host
  still holds its genesis entry in the oldest sealed segment, so verify enforces
  the genesis anchor there and a head-truncation surfaces as `valid<total`. The
  gate is the marker alone, NOT conjoined with the current `max_bytes>0`: an
  operator who evicted under rotation and then set `max_bytes=0` keeps the
  relaxed baseline, because the physical chain still lacks its genesis prefix.
- **Discard seam (`backup_count=0`), distinct from the eviction seam above.** The
  discard TRUNCATES the active file rather than unlinking it, so a writer in another
  process that already holds an `O_APPEND` fd keeps writing into the live file
  instead of into an orphaned inode. That write survives, but its `prev_hash` names
  the tip the truncate destroyed. The discard therefore records that one tip, MAC'd
  under the SEL key and domain-separated
  (`HMAC(key, "kirocrew.sel.discarded-tip.v1|" + tip)`), and verify attributes a
  re-anchor to it rather than reporting a chain break. This is deliberately NOT the
  sticky marker: the marker relaxes the genesis anchor wholesale, whereas this
  authenticates specific values.
  A single discard can leave TWO legitimately-anchored chains in that file — the
  discarding process re-anchors ITSELF to genesis and keeps logging, while the rival's
  records still link to the destroyed tip — and either can land first. So each of
  those two anchor values (`""` and the recorded tip) is adoptable at most ONCE per
  walk, at any position rather than only at entry 1. Measured before that widening,
  with the owner appending on both sides of the rival: owner-then-rival gave
  `total=2 valid=1` and "chain break at entry 2" with no attribution at all, and
  rival-then-owner attributed entry 1 and then broke on the owner's own genesis
  anchor. Both are ordinary operation, since the process that rolled the log carries
  on logging. Everything outside those two values still breaks, including a head
  truncation on a log that HAS discarded, and with no authenticated record nothing is
  adoptable at all — so the relaxation cannot be had by truncating a log that never
  rolled.
- **The marker is authenticated, not merely present.** Because the marker gates an
  integrity relaxation, a bare touch-file would hand the relaxation to exactly the
  adversary this module already defends against: the actor with write access to
  the log directory, which is *why* the HMAC key lives outside that directory. Such
  an actor could `touch` the marker, head-truncate a never-evicted log, and verify
  would adopt the surviving first entry's `prev_hash` and read clean. The marker's
  CONTENTS are therefore a domain-separated MAC under the SEL key
  (`HMAC(key, "kirocrew.sel.evicted.v1")`), compared in constant time. Anything
  missing, empty, unreadable, or non-matching reads as NO marker — fail-closed, so
  a forgery restores the genesis anchor it was meant to suppress and a corrupt but
  genuine marker merely makes a legitimately-evicted host false-alarm (loud and
  recoverable) rather than going quiet. Domain separation keeps the token valueless
  as a chain entry hash and vice versa. Rotation has never shipped, so there are no
  pre-existing unsigned markers to migrate.
- **Only CANONICAL POSITIVE integer suffixes are segments, and that is a data-loss
  guard.** `_segment_path` maps `index<=0` to the ACTIVE file by design, so a
  `security_events.jsonl.0` sitting in the segment directory resolves to the live log
  and makes both `_evict_over_budget` and `_prune_sealed_by_age` unlink it — measured,
  a planted `.0` with an aged stamp deleted the active file and its five entries
  outright. Nothing in this module writes `.0` (`_next_segment_index` starts at 1),
  but an operator, a partial restore or a pre-upgrade layout can leave one, and the
  cost of accepting it is the current audit log. The zero-padded forms are the second
  half: `.01` is a distinct FILE from `.1` but parses to the same index, so it both
  inflates the eviction budget (one more real segment deleted per roll, the same shape
  as an empty claim) and makes every path operation act on `.1` while `.01` is what was
  listed. Requiring `suffix == str(int(suffix))` and `>= 1` rejects `.0`, `.00`, `.01`
  and every other non-canonical spelling; a legitimate segment is always written as
  `str(int)`, so none is ever refused. `isascii()` continues to exclude the Unicode
  digits `isdigit()` accepts.
- **The retention count is bounded per LINE as well as in aggregate.**
  `_entry_count_of` now reads through `_open_segment` (picking up `O_NOFOLLOW` and
  `S_ISREG`) and caps each line at `_SEGMENT_LINE_CAP`, stopping the count rather than
  allocating an over-cap line. It deliberately does NOT call `_segment_lines`: that
  helper caps each line but accumulates all of them into a list, so it is O(file) in
  aggregate and would load a 100 MB segment whole — the opposite trade from what
  counting needs. The earlier reachability argument for leaving the read unbounded was
  wrong: it rested on `_prune_sealed_by_age` gating on `_newest_timestamp_of` and
  failing closed, which only holds when the oversized line is the LAST one. A 6 MB line
  in the MIDDLE of a segment whose final line carries an ordinary aged stamp passes
  that gate cleanly (measured). On an over-cap line the count becomes a FLOOR, which is
  acceptable because the count is observational — no caller gates on its exactness.
- **The active-file prune compares file IDENTITY, not just size.** Size equality is not
  identity: a rival process that rotates — sealing the active file away and letting
  appends recreate it — produces a different file, and byte-size equality between the
  old and the new one is a coincidence the pre-replace check would read as "nothing
  changed", so the stale `os.replace` would discard the recreated file's events.
  `(st_dev, st_ino)` is captured alongside the size and re-compared, the same
  discriminator `_snapshot_drift` uses against a reused segment number. Where a
  filesystem reports no usable inode both sides compare equal and this degrades to the
  size check rather than false-skipping.
- **A crash-left number claim is swept before the eviction budget is computed.**
  `_next_segment_index` creates the target with `O_EXCL` BEFORE the `os.replace` that
  fills it, so a process killed in that window leaves a zero-byte segment. Both
  in-process failure paths in `_seal_leased` already unlink it, but nothing cleaned up
  after termination, and the residue is inert to every other mechanism: it holds no
  entries, so the chain walks straight through it and verify still reports
  `total == valid` (it does NOT read as a chain break, contrary to what the seal-path
  comment used to imply), and `_newest_timestamp_of` yields no stamp so age-pruning
  fails closed and keeps it forever. What it is not inert to is the eviction budget —
  it inflates `len(indices)`, so every roll for the life of the install evicted one
  additional VALID segment, silently. Measured at `backup_count=3` with segments 8-10
  retained, a zero-byte claim at 11 evicted segment 8 and its 439 bytes of real audit
  history. `_drop_empty_claims` excludes the zero-byte NUMBER from the eviction
  budget and LEAVES THE FILE on disk. Deleting it looked lossless — an empty segment
  carries no audit history by definition — but a zero-byte segment has two
  provenances this code cannot tell apart: a crash-left `O_EXCL` number claim that
  never held history, or a segment that WAS sealed and was later truncated. Nothing
  on disk records which numbers were successfully sealed (there is no sealed
  manifest) and a zero-byte file has no content to authenticate, so the self-HMAC
  that guards age-pruning is unavailable here. With the two indistinguishable the
  only safe direction is to keep the file: the survivor is itself the evidence, and
  `verify_integrity` reports it as unverifiable, forcing `valid < total` until an
  operator resolves it. Truncation of a REAL segment is therefore the SAME case
  rather than a different one — both keep the file and both surface through that
  unverifiable branch. The argument that truncation "stays loud either way" because
  the successor's `prev_hash` still names the tip that was truncated away is FALSE at
  the position eviction deletes from: baseline relaxation applies to the oldest
  surviving entry, and eviction removes a PREFIX. Measured on a 40-entry log with an
  authenticated marker present, truncating the OLDEST sealed segment and keeping the
  file reported `total=40 valid=39` (correctly not clean), while unlinking it
  reported `total=39 valid=39` — `integrity: ok` over an erased record. A stat
  failure deliberately keeps the number — this is a
  budget input, and guessing that an unstattable segment is empty would evict real
  history.
- **A planted segment-dir link is refused on BOTH the write and the read path.**
  The sensitive-path floor protects the registered path `~/.kiro/crew/sel`, not
  wherever that path happens to point, and `mkdir(exist_ok=True)` FOLLOWS an
  existing link — so a `sel` symlink/junction planted before rotation shipped would
  have every sealed segment written to the attacker's target. On the WRITE path
  `_ensure_segment_dir()` removes the LINK (never its target) and creates a real
  directory, refusing to rotate if it cannot, because an un-rolled oversized log is
  recoverable while history written outside the floor is not. That repair is
  reached only by `_rotate_now` and `_next_segment_index`, so it does NOT cover the
  READ paths: `verify_integrity`, `recent` and `prune` Stage 1 all reach
  `_list_sealed_indices`, and with rotation off or simply not yet due nothing
  repairs the link before they list. `_list_sealed_indices` therefore refuses a
  linked directory itself and returns no segments, fail-CLOSED in the same shape as
  `_has_evicted`. It refuses rather than calling `_ensure_segment_dir` on purpose:
  that helper MUTATES (unlinks, `mkdir`s, and raises `OSError` when the result is
  still not a directory), so calling it from a read would make a documented
  read-only verification write to disk and raise into dashboard callers that have
  no handler. Without the read-side half, `iterdir` enumerates the TARGET, so any
  `security_events.jsonl.<n>` sitting there — another install's segment directory
  being the realistic aim — is surfaced by `recent()` as this log's own audit
  events and DELETED by eviction and age-pruning, both of which unlink whatever the
  listing returns.
- **Verify pins segments by open handle.** `_walk_chain` opens every segment under
  `_lock` and hands the handles to `_walk_handles`, which does the reading and HMAC
  work after the lock is released. Pinning is a correctness requirement, not an
  optimization. Under monotonic numbering ONE remaining hazard is an UNLINK: a
  concurrent roll can evict or age-prune a segment the walk has already snapshotted,
  and a path-based read would then either fail or skip it. An open handle follows the
  inode, so the walk still sees the segment's bytes. The sharper form of this bug
  belonged to the shift-rename layout, where a roll moved `.k` → `.k+1` and resealed
  the active file as `.1`: every snapshotted path still *existed* while naming a
  different inode, so the walk read an internally chain-adjacent set that silently
  omitted the renamed-away segment, and the eviction marker suppressed the break that
  would have exposed it — measured then at `total=26 valid=26` (`integrity: ok`) with
  all 30 entries still on disk. Monotonic numbering removes that variant outright,
  since no existing segment is ever renamed. This is also why there is no retry loop: the condition it retried on
  (an `OSError` from a vanished path) was never the condition that occurred.

  **The other hazard is a cross-process SEAL, and handles alone do not cover it.**
  Pinning fixes the inodes the walk reads; it does nothing about a segment that was
  never in the listing. `_lock` is a `threading.Lock`, so it orders nothing against
  the other writer processes this module documents below, and a rival seal
  `os.replace`s the ACTIVE file onto a fresh number, recreating it only on the next
  append. Verify opening the active path inside that window gets `ENOENT`, cannot
  `lstat` it, and takes the ordinary "no active file yet" branch — while the entries
  that were in it now live in a number the listing predates. Everything actually
  opened then validates, so this failed SILENTLY rather than loudly: measured at
  `total=19 valid=19` (`integrity: ok`) with all 20 entries still on disk. Fixed by
  taking the snapshot until it is STABLE — re-list after opening and redo if anything
  moved (`_VERIFY_SNAPSHOT_ATTEMPTS`, 3); once the attempts are spent, every path the
  snapshot could not pin cleanly is counted UNVERIFIED so `total > valid` reports loud
  instead of clean. Deliberately NOT done by holding `_seal_lease`: that lease is
  non-blocking BY DESIGN so rotation skips a roll rather than waiting, so a reader
  holding it would both block the writer and turn every concurrent roll into a
  verify failure. Re-listing plus one `fstat` per segment costs no more than a
  `readdir` and leaves rotation untouched. This is not the retry loop that was removed
  either — that one keyed on an `OSError` that never fired, this keys on the snapshot
  MOVING.

  **Stability is judged by IDENTITY, not by the sealed number set.** Numbering is
  monotonic only while a sealed segment SURVIVES: `_next_segment_index` is
  `max(existing)+1` and falls back to `1` on an empty set, so once the last segment is
  pruned the next seal REUSES its number. A number-set comparison therefore passes
  while a number names a DIFFERENT file, and the handle already pinned keeps reading
  the unlinked inode — so verify vouches for history that is no longer retained while
  never examining the history that is. This is a substitution rather than the tail
  staleness above, which is why it is worth an `fstat`: measured, an aged `.1` pruned
  and the active file resealed onto `1` gave `total=6 valid=6` (`integrity: ok`) over
  6 evicted entries while the 3 retained ones went unread. `_snapshot_drift` compares
  `(st_dev, st_ino)` captured at open against the path's current identity — the same
  discriminator `platform_compat` uses for its bind-mount checks — and degrades
  safely to the number set where a filesystem reports no usable inode, rather than
  false-retrying. The two scenario tests for this are POSIX-only for the same reason
  the handle-pinning ones are: they must unlink a segment while verify holds it OPEN,
  which Windows refuses with WinError 32, so the substitution cannot be constructed
  there at all. `_snapshot_drift` is driven directly by a cross-platform test, so the
  discriminator stays pinned on every platform.

  **Platform note.** The guarantee holds on Windows too, but the OS enforces it a
  different way, and that changes what a competing roll does rather than what verify
  reports. POSIX lets you rename or unlink a file that still has an open handle (the
  inode stays alive for whoever holds it), so there the pin is what keeps the walk
  consistent. Windows refuses that rename outright with `WinError 32` ("used by
  another process"), so the rebinding this bullet describes cannot happen there at
  all — verify still cannot report a vacuous `integrity: ok`. What it costs on
  Windows is that a roll landing while a verify is in flight FAILS instead of
  succeeding. That is already contained and does not lose events: `_flush_batch`
  catches it, logs a warning, increments `kirocrew.sel.rotation_failed.count`, and
  still appends the batch to the un-rotated active file, so the log simply stays
  over `max_bytes` until the next flush rolls it. No SEL code path opens a segment
  and then renames it in the same call, on any platform — every read helper
  (`_entry_count_of`, `_newest_timestamp_of`, `_tip_hash_of`) closes its handle via
  `with open(...)` before the rename, and `prune()`'s `os.replace` runs after both
  of its `with` blocks have exited — so this is only ever reachable through genuine
  concurrency, never self-inflicted. Consequence for tests: the two scenario tests
  that reproduce the race by renaming/unlinking mid-verify are POSIX-only and skip
  on Windows, because the kernel forbids the setup they need. The mechanism itself
  stays covered on every platform by structural guards (`_walk_handles` reads only
  from handles; handles are opened under `_lock`) plus a simulated-`WinError 32`
  test asserting the containment above.
- **Cross-process rotation takes a narrow seal lease.** `_lock` is a `threading.Lock`, so it
  orders writers only within one process — and SEL has more than one writer process on
  a normal host: the dashboard gateway and the MCP gateway daemon
  (`mcp_gateway/gatewayd.py`, `backend.py`, `app_call.py`) each call
  `SecurityEventLog()` with no `base_dir`, so all of them resolve the same file.
  Monotonic numbering is what makes that safe. Each process claims its own number
  with `O_CREAT|O_EXCL` (`_next_segment_index`) so neither can take the other's, and
  the seal is a single `os.replace` of the ACTIVE path — no existing segment is ever a
  rename source. Note the atomic claim is load-bearing, not decoration: a plain
  `max(existing)+1` is a read-modify-write, and two processes rolling at once would
  compute the same number.

  **The atomic claim is necessary but NOT sufficient, so a lease is also taken.**
  It stops two processes claiming the same number; it does not order the seal, and
  the chain depends on that order. A claims N, B claims N+1, B moves the active file
  onto N+1 first, appends recreate the active file, and A then moves that NEWER data
  onto N — a lower number holding newer events, which reads out of order and which
  eviction (deleting the lowest first) would drop before the older history.
  `_seal_lease` closes that window, spanning only claim + replace, so no existing
  segment is ever inside it. It is non-blocking: losing the race means "skip this
  roll", never "wait on the writer thread". The same lease covers the
  `backup_count=0` discard path, where an unlink on a stale size would delete an
  active file another process had already rolled and appends had recreated.

  **This does NOT fix the multi-writer APPEND hazard, and does not claim to.** Two
  processes appending concurrently already corrupt the HMAC chain, because each caches
  its own tip in `_last_hash` and never re-reads it per append: measured with rotation
  disabled, two processes writing 40 events each produced `total=81 valid=8`. That is
  pre-existing, architectural, and strictly larger than rotation. The two failures are
  different in kind, which is why only one is closed here — a broken chain is
  fail-loud with every entry still on disk and forensically recoverable, whereas a
  clobbered segment is silent and gone. Making SEL genuinely multi-writer needs a
  single-writer daemon or an append-path lease, and belongs in its own change.
- **A missing MIDDLE segment = not clean.** The walk covers every numbered segment
  on disk, so a hole makes the segment after it chain off a deleted entry: the
  mismatch lands in `total` and never in `valid`, and the state reads `valid<total`.
  This needs no separate orphan accounting — the previous layout stopped the walk at
  the first gap, so stranded segments had to be found and folded in or they would have
  vanished silently. A gap at the BOTTOM of the numbering is not a fault at all; that
  is what ordinary eviction leaves behind.
- **Chain tip after a roll.** `_read_last_hash()` walks newest→oldest across
  segments, so a restart that finds an empty active file still seeds the tip from
  `.1` instead of restarting the chain at genesis (which verify would report as a
  break at the seam). It reuses the per-segment `_tip_hash_of()`, which keeps the
  existing corrupt-tail-line recovery: a truncated final line is skipped rather
  than resetting the chain.
- **No renumber, so no residue class.** Age-pruning unlinks whole aged segments and
  survivors keep their numbers, so there is no two-loop rename through temporary
  names and therefore no crash-left `.tmp_rot` residue to report, refuse, or reason
  about. The previous layout had to renumber survivors to keep the run contiguous,
  which is what created that residue — and refusing to adopt it was load-bearing,
  because a genuine sealed segment could simply be COPIED to a residue name and
  replayed with real HMACs. Removing the renumber removes the attack surface rather
  than guarding it.
- **Rotation-failure observability.** A rotation failure (disk full, EPERM) is
  swallowed so it never blocks the audit append, but it increments the
  `kirocrew.sel.rotation_failed.count` metric counter so a persistent failure —
  which would silently degrade back to unbounded growth — is visible. A counter,
  not a SEL event: rotation runs inside the writer thread mid-flush, so enqueuing
  an event would recurse into the same writer. The counter is emitted after
  `_lock` is released so a slow metrics backend cannot stall the writer.
- **Accepted limitation.** Without an external tip anchor, verify cannot
  distinguish a legitimately-evicted genesis prefix from a maliciously
  head-truncated one on a rotated host — *provided the marker authenticates*, which
  narrows this residual to hosts that really did evict. This is inherent to bounded
  retention; surviving entries and their tamper detection are unaffected.
- **Downgrade caveat.** Stated in the same spirit as the HMAC key's rollback caveat
  above. Once a host has rotated, an older binary has no notion of sealed segments:
  its `_read_last_hash` and `verify_integrity` see only the active file, so verify
  false-alarms on a non-genesis first entry, and a restart re-anchors the chain at
  genesis — permanently splitting it from the sealed history. That is fail-loud
  rather than data-loss (every sealed segment stays on disk and its own HMACs still
  verify), but the split does not self-heal on re-upgrade: the pre-downgrade
  segments and the post-downgrade active file are two chains. If chain continuity
  across a downgrade matters, archive the segment set first.

## Async Writer

`log()` is off the hot path: callers enqueue the event on an unbounded
`queue.Queue` (never blocking) and a single daemon writer thread drains it,
computing the HMAC chain in enqueue order and batching up to `_QUEUE_DRAIN_BATCH`
events into one `open()`+write. The writer starts lazily on first `log()` and
registers an `atexit` flush.

- **Durability**: eventually-durable, not synchronously-durable — a crash/kill
  can lose at most the events still queued. Acceptable for an audit log; the
  hot path (e.g. per-message skill triggering) no longer pays fsync/lock latency.
- **Read-after-write**: `flush()` runs before every read path (`recent`,
  `verify_integrity`, `prune`) and on exit. It waits on a pending-event counter
  (a `threading.Condition`, race-free vs a bare queue-empty check), bounded by
  `_FLUSH_TIMEOUT_SECS` so a wedged writer can't hang a read.
- **Fallback**: if the writer can't be started, `log()` writes synchronously so
  an event is never silently dropped.
- **`sync=True`**: `SecurityEventLog(base_dir=..., sync=True)` writes each event
  inline (no thread) — used by tests that read the raw JSONL immediately after
  logging.

## Retention

Default 365 days. Pruned daily by the heartbeat service (`_PRUNE_TICKS`), already
offloaded off the event loop via `run_in_executor(maintenance_executor(), sel().prune)`
so the blocking file IO cannot stall the heartbeat. `prune()` is two-stage: (1) drop
whole sealed segments whose newest entry predates the cutoff — clean, because it
never severs a chain mid-segment — then (2) rewrite the active file dropping its own
aged entries. Stage 2 streams the file line-by-line into a temp file and
`os.replace`s it, so memory stays bounded on a `max_bytes`-sized log, and the whole
read+filter+rewrite runs under `_lock` so it cannot drop an entry the writer appends
concurrently or race a rotation rename. `_lock` is released between the two stages so
the writer is not blocked across both; the returned count is therefore best-effort.

The heartbeat calls `prune()` with no argument, which defaults `keep_days` to the
instance's `retention_days` knob rather than reading the module constant directly,
matching the rotation path (`_maybe_rotate` → `_prune_sealed_by_age(self._retention_days)`)
so the two cannot drift apart. Both resolve to the same 365 days unless a caller
overrode the knob at construction, so this is behaviour-preserving today.
`keep_days<=0` is the retention off-switch and BOTH stages no-op — without that
guard Stage 2 would derive a cutoff of `now` and delete every live entry. An
unparseable timestamp fails CLOSED (the entry or segment is kept) rather than being
treated as ancient and deleted.

The rotation knobs (`max_bytes`/`backup_count`/`retention_days`) bind on the SEL
singleton's FIRST construction, so a change takes effect on the next gateway
restart, not live. They are **operator-settable per host** through the
environment — `KIROCREW_SEL_MAX_BYTES`, `KIROCREW_SEL_BACKUP_COUNT`,
`KIROCREW_SEL_RETENTION_DAYS` — falling back to this module's constants when
unset. A malformed value is ignored with a warning rather than guessed at, so a
typo cannot quietly change a deletion bound. There are no constructor kwargs: the
knobs are plain instance attributes, which is all the tests need, so the
constructor exposes no rotation surface at all.

The env layer exists because these three values govern DELETION on an audit
surface. Amazon's audit-log retention guidance requires the retention period to be
operator-settable and security events to be kept at least 365 days, and a
compile-time-only cap leaves a host whose event volume outruns `max_bytes` with no
lever to stop evidence being dropped early. `KIROCREW_SEL_MAX_BYTES=0` disables
size rotation entirely and is the documented opt-out; the 365-day age prune stays
active independently, so a retention ceiling still exists with rotation off. The
config follow-up adds a `sel` section that slots between the environment and these
defaults, wired in the one knob-resolution block.

**Size and retention are independent bounds, and size wins** — it runs first. On a
host whose volume outruns `max_bytes`, eviction can therefore delete a segment the
retention window still wants, which is the "audit evidence deleted before the
review period ends" failure. That case is deliberately loud rather than silent: the
eviction path compares each doomed segment's newest entry against the retention
cutoff and, when it is inside the window, logs a warning naming the count and the
two env knobs to raise, and increments `kirocrew.sel.early_eviction.count`. Being
best-effort, an unparseable or absent timestamp means no warning — never a skipped
eviction, because the size bound must still hold.

## Integration Points

| Surface | What's Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_call` (invoked/denied), `permission_request` (all outcomes) | `slack/handler.py` |
| Dashboard chat | `tool_call` (invoked), `permission_request` (all outcomes) | `dashboard/chat.py` |
| TaskRunner | Permission requests during decomposition and step execution | `taskrunner.py` |
| Subagent | Permission requests during subagent execution | `subagent.py` |
| Background tasks | Permission requests via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `task_run` calls and outcomes | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, etc. calls and outcomes | `mcp_cron.py` |
| Dashboard API | All POST/PUT/DELETE operations via middleware | `dashboard/server.py` |
| ACP worker-pool audit | Per-`tool_call` `auto_approved` `tool_invocation` (`source=subagent`), bounded by `_SEL_AUDIT_TIMEOUT_SECONDS` (5.0s) and offloaded off the event loop so a wedged SEL backend never gates dispatch. Two emitters: the knowledge LLMPool via `AcpClient._maybe_audit_tool_call` (gated on the `audit_source` ctor param, offloaded to `subprocess_executor()`); and **code-review-sage's ReviewPool**, which migrated to the shared `AcpRuntime` (no `audit_source`) and re-emits the same per-tool record itself | `acp/client.py`, `apps/builtins/code_review_sage/sage_lib/review_pool.py` |
| Token auth | `internal_auth`, `app_scope_check`, `dashboard_sessions_revoked`, `refresh_token_initial_mint`, `nonce_evicted` (`source=token_auth`) | `dashboard/token_auth.py` |
| Refresh tokens | `refresh_token_use`, `refresh_token_logout`, `access_cookie_revoked` (`source=refresh_tokens`) | `dashboard/handlers/auth_refresh.py` |
| ACP transport | `tool_interrupted` per-turn cancellation audit (`source=acp`) | `acp/client.py` |

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sel/events?limit=N` | Recent security events (max 1000) |
| GET | `/api/sel/verify` | HMAC chain integrity check |

## CLI

```
kirocrew security events [-n 20]   # Show recent events
kirocrew security verify            # Verify HMAC chain integrity
```

## Thread Safety

Singleton pattern. The chain state (`_last_hash`) and the file append are
guarded by `threading.Lock`, held only inside the writer thread (and the
synchronous fallback / `prune`), never by enqueuing callers. Enqueue is
lock-free via the thread-safe `queue.Queue`. Safe for concurrent access from the
asyncio event loop + MCP server stdio processes.
