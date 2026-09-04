# Ephemeral memory contract

Today a task boots by downloading EVERY object under `crews/<crew>/` onto its local
disk (`container/backup/restore.py`, the `for full_key in sorted(listing)` loop). So
one task holds every conversation that crew has ever had, and the backend can read
all of them. Isolation is per CREW, enforced by the IAM prefix. It is not per
CUSTOMER.

For a crew that serves one customer that is fine. For a crew that serves many, it
is not: one customer's context sits on the same disk as another's.

This change removes the boot-time bulk restore and fetches a conversation on
demand instead.

## The property this actually achieves, stated exactly

**A task only ever holds the conversations it itself served, and loses them when it
exits.**

NOT "the task holds nothing". That is not achievable and claiming it would be a
lie: the Kiro Crew backend's session store IS a filesystem (`sessions/*.jsonl` under
a flock, `open_slots.json`, `session_map.json`), so a served conversation is on disk
while it is being served. The disk is scratch, not a store.

Write the property the way it is above wherever it is described. A reader who is
told "nothing is kept" and then finds a jsonl file has been misled.

## What must still be restored at boot, and why it cannot be ephemeral

`session_map.json` and `open_slots.json` are KIROCREW's files, not ours. Per
`container/common/config.py:120`: without `session_map.json` there is no resume at
all, and `open_slots.json` is the authoritative record of which conversations exist.
The backend writes them; we only name their paths.

So boot restores the `config/` namespace and NOTHING under `data/sessions/`. Both
files are small and the restore stays fast. Do not attempt to make them per-session
or to synthesise them.

## The seam

```
boot:      restore config/ only. Transcripts are absent by design, not by failure.
per turn:  the front process ensures THIS slot's transcript is on disk, then forwards.
write:     unchanged. The sidecar keeps uploading what changed.
exit:      the disk goes with the task. No eviction step.
```

**Eviction is deliberately not implemented.** The backend may hold a session open,
and deleting a file underneath it is a corruption we would have to detect. The wipe
is the task exiting. Say so rather than shipping a half-eviction.

## Track A owns: restore becomes config-only

Files: `container/backup/restore.py`, `container/backup/layout.py` if a helper is
needed, and their tests.

- Restore the `config/` namespace only. Leave `data/sessions/**` alone.
- `artifacts/` are write-once and not conversation context. Decide whether they are
  still restored and say why in your report.
- The completeness check that requires both authority files STAYS. An absent
  `session_map.json` is still a degraded restore and must still say so.
- The `RestoreResult` must report how many TRANSCRIPTS it restored, and that number
  must be 0. This is the number the deploy gate reads, so it has to be in the boot
  log as a stable, greppable line.
- Keep the boot ordering. Restore still runs to completion before the backend
  starts, because `open_slots.json` landing late is the exact race the three-phase
  boot exists to prevent (`container/supervisor/__main__.py`, phase 1).

## Track B owns: fetch one transcript on demand

Files: `container/front/` and a store helper it can share, plus tests.

The hook point is already correct and already serialized:

```python
async with serializer.for_slot(slot_id):
    return await backend.forward_completion(client, settings, body)
```

`serializer.for_slot` guarantees one turn per slot at a time, so a fetch inside it
cannot race itself. Put the fetch there, after `judge_addressed_crew` has accepted
the turn and before the body reaches the backend.

Rules:

- Fetch ONLY this slot's transcript. Never list the prefix and never fetch a second
  object, or the change is undone at the first turn.
- Absent in S3 is NORMAL, not an error: a new conversation has no transcript yet.
- Already on disk means already served by this task. Do not re-fetch and do not
  overwrite: the local copy is newer than S3 by up to one backup interval, and
  overwriting it would silently roll a customer's conversation backwards.
- A fetch that FAILS must fail the turn, not silently serve an empty history. A
  customer whose conversation appears to have been forgotten is worse than an error.
- The transcript filename is `<thread>_<slot>.jsonl`, not `<slot>.jsonl`. That
  mapping is real and was found the hard way; `control/observe.py resolve_open_slots`
  documents both forms with the evidence.
- Never log a transcript's contents. Log the sid and the byte count.

## Track C, mine: the gate and the contract

A property with no gate is a claim. The gate asserts the boot log reports zero
transcripts restored, using the line Track A stabilises.

## Non-negotiables

- Read-only S3 on the fetch path. The sidecar remains the only writer.
- A transcript is not append-only: it is atomically replaced and can get SHORTER,
  and its mtime is restored after a rewrite. Compare size plus ETag, never mtime,
  and never a byte offset.
- No new AWS permission. The task role already grants
  `s3:GetObject` on `crews/<crew>/*`; if you find yourself needing more, stop and
  report rather than widening the role.
- Every existing suite stays green: 128 container, 97 gate, seam, cfn-lint, and the
  dry run reaching 12 gate PASS.
