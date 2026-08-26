# MCP Probe-Failure Counter

Status: implemented (this PR). The UNMOUNT half is deliberately deferred — see §6.
Owners: `src/kiro_crew/mcp_quarantine.py` (the store),
`dashboard/handlers/mcp.py` (recording, annotation, reset),
`website/src/pages/overview/McpTab.tsx` (the surface)

## 1. Problem

A probe verdict was display-only AND forgotten between rounds. `probe_server` could
time out on the same server every ten minutes for a week and nothing anywhere could
say so: the two probe caches are process memory with a 600s and a 1800s TTL, neither
carries a consecutive-failure count, and the dashboard row showed only the latest
single reading.

So the operator saw an `Error` badge that looked identical whether the server had
failed once on a cold cache or forty times in a row. Those two need different
responses, and nothing distinguished them.

## 2. What this ships

A durable consecutive-failure count per server, surfaced on the row that already
shows the probe status, with a reset control.

- `error` and `timeout` increment; `ok` deletes the record outright.
- Crossing `agent.mcp_quarantine_after_failures` (default 3) marks the server as
  persistently failing, which is what the row's second badge reports.
- The row carries `probeFailures` (the count) and `probeFailing` (crossed the
  threshold). Both are added only when a server has failures on file, so a healthy
  fleet's response shape is unchanged.
- `POST /api/mcp/quarantine/clear` resets one server's count.

## 3. What it deliberately does NOT do

It does not unmount the failing server. The server keeps being spawned by every new
session, which is the cost the originating issue asks to stop paying. §6 records why
that half is not here.

Because nothing is unmounted, the surface says so: the badge reads as a health
reading ("Failing", "{{failures}} consecutive probes failed. The server is still
mounted; this is a health reading, not a change.") and the action is "Reset count".
An earlier revision called these "Quarantined" and "Remount", which would now be a
label claiming an action the code does not take.

## 4. Mechanism

**Counting.** Both probe paths (`_run_mcp_probe` and `POST /api/mcp/probe`) fold
their rows into `record_verdicts` in one write, off the event loop. Statuses other
than `error` / `timeout` / `ok` carry no verdict and leave the record untouched —
`disabled` (never probed), `unknown` / `outdated` (no fresh result), and `needs_auth`,
which is a server working correctly and asking for a token. Counting `needs_auth`
would label every OAuth connection the user has not signed into yet.

The same rule excludes a row by MODE, not only by status: `probeMode: "declared"` is
dropped before it reaches `record_verdicts`. When a managed server cannot be probed
under the sandbox, discovery lists the tools the package declares and reports `ok` —
its own comment says "nothing verified the server can START". That `ok` is not a
handshake, so it must neither clear a streak nor add to one; letting it through would
make a server broken for a week look healthy the moment the sandbox went unavailable.
Only a status that actually reports a handshake attempt may move the counter.

A counter rather than a single failure because one probe failure is routinely
transient (cold npm cache, a laptop that just woke, a registry blip). The claim being
made is "consistently unreachable".

Every server is counted, including Kiro Crew's own managed ones. An earlier revision
filtered them out so no badge could claim an unmount that did not happen; with no
unmount the count is a plain diagnostic and withholding it would only hide
information.

**Reading.** `_annotate_quarantine` stamps the two fields at RESPONSE time on all
four row-returning endpoints, including `GET /api/mcp` — the one the table actually
loads from. Annotating only the probe endpoints (the first version) meant a failing
server rendered as a plain error row until the user happened to press Probe.

**Resetting.** `clear` drops the record, counter and flag together: resetting to
one-short-of-the-threshold would make the button look broken.

**Concurrency.** Every load-modify-save runs under one process-local lock. Both
writers live in the gateway (the probe fan-out and the reset endpoint), so without it
a probe round and a reset race and whichever saves last discards the other.

**Fail-open for READERS.** An unreadable or malformed store reads as no records. The
records only ever ADD a diagnostic to a row, so failing closed would let one bad byte
mislabel the whole fleet. `UnicodeDecodeError` is caught explicitly: it is neither an
`OSError` nor a `JSONDecodeError` but a `ValueError` from the strict decode, and it
escaped both other arms.

**But a WRITER distinguishes two kinds of unreadable**, because the same fail-open
inside a read-modify-write is data loss -- a transient read failure would present as
an empty store and the save that follows would erase every counter on disk:

| outcome | cause | a mutation... |
|---|---|---|
| `unreadable` | `OSError` -- a Windows sharing violation against the antivirus scanner, EIO, a permission flip | **aborts.** The file may hold good records we could not reach this instant, and retrying may succeed. Costs one skipped increment. |
| `corrupt` | anything the parse raises -- invalid JSON, invalid UTF-8, an integer past the digit limit, nesting past the recursion limit -- or the wrong shape | **may overwrite.** Bytes that are not our format never become readable and hold nothing to protect. This is the only path back to a working store -- aborting here too would wedge the counter permanently, recoverable only by hand-deleting the file. |

`FileNotFoundError` is neither: a missing store is the normal state on a machine where
nothing has ever failed a probe. It is caught ahead of the `OSError` arm, which it
would otherwise match.

The `corrupt` arm catches `Exception` around the parse rather than naming error types.
Four review rounds found four different ones escaping successively wider tuples:
`JSONDecodeError`, then `UnicodeDecodeError` (a `ValueError` from the strict decode),
then a plain `ValueError` from the scanner's own `int()` past the 4300-digit limit, then
`RecursionError` (a `RuntimeError`, not a `ValueError` at all) from a deeply nested
document. The classification is a fact about the FILE, not about which Python error
surfaced, and the store is not fenced under `_CREW_SECRET_LEAVES`, so the set of ways a
parse can fail is open-ended and grows with the interpreter. Enumerating them was the
bug.

Catching that broadly is only safe because of how the read is structured: **two separate
`try` blocks, each holding exactly one operation**, with **bytes as the seam**. The I/O
half calls `read_bytes` and classifies only `OSError`; the parse half calls `json.loads`
on those bytes and classifies everything else. Decoding belongs to the parse half — a
`read_text` in the I/O half would raise `UnicodeDecodeError` there, which is neither an
`OSError` nor something that half should be judging. The split is what guarantees the
broad arm cannot absorb a read failure and quietly relabel recoverable data as
overwritable, and that there is no application logic inside it for it to mask.

The reset endpoint surfaces `unreadable` as a 500 rather than reporting a reset it
could not make: its caller tells the user the count is clear.

**Bounded, regular-file-only read.** The store path is agent-writable, and `read_bytes`
reads to EOF, so a link pre-planted at the leaf pointing at `/dev/zero` turned every
`GET /api/mcp` into an unbounded allocation that takes the gateway down. A FIFO is the
same shape with a different ending — `open` blocks until a writer appears, so the request
hangs instead. Four independent guards, each covering a different shape (verified
individually by disabling them one at a time):

| guard | refuses |
|---|---|
| `O_NOFOLLOW` | a symlink planted AT the store path — refused outright rather than followed and then judged by its target |
| `fstat` on the DESCRIPTOR + `S_ISREG` | a special file created at the path directly (the descriptor, not the path, so the thing measured is the thing read) |
| `O_NONBLOCK` | a FIFO blocking inside `open`, before `fstat` could reject it |
| an 8 MiB cap on BYTES READ | a merely huge regular file; enforced on bytes rather than `st_size`, which a file growing under us understates |

`O_NOFOLLOW` / `O_NONBLOCK` / `O_BINARY` are applied via `getattr(os, name, 0)` — the
first two do not exist on Windows and are no-ops for a regular file anyway.

Only the LEAF is this module's problem: a redirect planted at a parent is
`atomic_write._refuse_linked_parent`'s job, and `os.replace` does not follow a leaf link,
so a write replaces a planted link with a real file. Reading was the half with no bound.

**Bounded records, rebuilt from an allowlist.** `_sanitize` does not copy a loaded record
and patch it — it REBUILDS it from the five fields this module owns (`fails`,
`last_status`, `last_error`, `last_failed_at`, `crossed_at`), each coerced to a bounded
scalar. Unknown keys are dropped. That is what makes the write safe by construction:
nothing from the file reaches `json.dumps`, so it cannot fail on content.

A passthrough copy could not be made safe field by field. Three separate payloads reached
the encoder through it: a `fails` of 4300 nines (parses, then `+ 1` cannot be encoded); an
extra key holding ~900 nested arrays (parses, then `json.dumps` raises `RecursionError` —
a `RuntimeError`, so it escaped the save guard, and only when few frames remain, which is
why it reproduces through a handler and not from a shallow stack); and non-finite floats,
which `json.dumps` emits as bare `NaN` / `Infinity` — valid Python, invalid JSON, so the
file stops round-tripping.

`bool` is excluded from both the counter and the timestamps: `isinstance(True, int)` is
true in Python, so `fails: true` would have counted as 1 and `crossed_at: true` would have
become the timestamp 1.0 and read as "crossed". `crossed_at` normalises absent to `0.0`,
which every reader already treats identically to absent.

`_FAILS_MAX` (1,000,000) is a serialization limit, not a policy: it is a failing probe
every ten minutes for ~19 years, so it cannot clamp a genuine count or hold a configured
threshold out of reach.

**Off switch.** `0` disables the mechanism, and the threshold is re-read on every
read, so turning it off also clears what it already flagged.

## 5. Audit

A reset records the SEL operation `mcp_probe_failures_reset`.

## 6. Deferred: the unmount

The originating issue asks for the failing server to stop being mounted. Three
distinct levers were each implemented and each shown unsafe:

1. **Drop the entry from the generated agent config.** Destroys anything that exists
   only there. The rebuild merges onto the EXISTING entry, so a whole server reachable
   only through the agent config (`kiro-cli mcp add --agent kirocrew`, a hand-edit),
   and equally a single field on an otherwise-shared server, is the sole persisted
   copy. Dropping it is unrecoverable.
2. **Stamp `disabled: true` on the emitted entry.** Preserves the spec exactly, but
   `mcp_discovery.list_servers` adds such a name to `disabled_in_agent` and then
   refuses to introduce it from any other scope — so the server's own row disappears,
   taking the badge that explains the state and the control that clears it.
3. **Shelve the spec in this store before dropping, restore on release.** The store is
   not in `security._CREW_SECRET_LEAVES`, so an agent's file tools can write it; a
   shelved spec replayed into the agent config with its tool ref is arbitrary MCP
   execution outside the PreToolUse gate. It also still lost fields whose values
   conflicted with the shared spec, and shelve/unshelve could not be made atomic with
   the agent-config write.

There is no fourth lever at this layer: the generated agent config is simultaneously
the mount decision AND the only home for agent-only configuration, and Kiro Crew owns
no other point between the probe verdict and the spawn. A safe unmount needs a
mechanism that is not that file — which is a design task, not a patch.
