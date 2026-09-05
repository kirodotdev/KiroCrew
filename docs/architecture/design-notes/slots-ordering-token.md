# A server-stamped slots generation

Status: OWED, not optional. The client-side reconstruction below is what ships today, and it
is complete, tested and correct on its own terms — but it is the mechanism-level half of a
protocol problem, and the wire fix this note enumerates is the cause-level one. Treat the
reconstruction as carrying a debt rather than as a settled design: "The replacement" below is
the scope of that debt, already itemised so it needs no re-derivation. It exists because the
slots list says *what* is true without saying *when*.

EXIT-CONDITION: `SLOTS_GENERATION_ON_WIRE` — enforced, not merely recorded. A test asserts
that neither `serialize_slots` nor `push_slots_update` stamps a per-instance ordering field
(`generation`, `instance_id`, `epoch`, `revision`, `incarnation`), so the change that adds one
FAILS until the deletion below lands with it. The trigger is executable, so it cannot be
forgotten the way a documented convention can.

- **Owner** — whoever next changes the shape `api_chat_slots` serializes. This note
  claims no separate assignee, and none should be read into it.
- **Trigger** — the FIRST time the slot payload gains any per-instance ordering field
  (`generation`, `instance_id`, `epoch`, `revision` or `incarnation`). At that point the
  client reconstruction is redundant rather than merely improvable, and the deletion
  below is owed in the same change rather than deferred once more.
- **Deletion scope** — exactly what "The replacement" enumerates, and nothing outside
  it. That section is the checklist; this bullet only names it as the binding scope.

## Stamp the slots list with a generation

### Problem

`DELETE /api/chat/slots/{key}` pops `_slots` only after its nudge-lock and
app-close-hook awaits, so a `GET` issued before the close can be serialized
while the closing slot is still listed and arrive after it is gone. Applying
that reply reinstates the row — the flicker this machinery exists to stop.

Nothing on the wire distinguishes slot instances or orders two list replies:
`api_chat_slot_resume` restores `slot.created_at`, so `created` cannot tell a
resumed replacement from the original, and a grep for `instance_id`, `epoch`,
`revision`, `generation` or `incarnation` in the slot payload returns nothing
against a positive control (`created_at`, 5 hits). So the client dates replies
against its *own* actions, which is the only ordering fact it holds.

### What the client builds instead

Four coordinated pieces of state plus a second confirming read:

- `closeSeq` — a monotonic count of closes begun.
- `pendingSlotReads` — requestId to the `closeSeq` current when that read was
  issued, which is what makes a reply datable at all.
- `closingSlots` — key to a `CloseTombstone`, withheld from `slots` so no stale
  frame can reinstate the row.
- `CloseTombstone.retireReadId` — the requestId of the close's own post-DELETE
  read, since only that read proves the server popped the slot.

### Known coarseness of the client reconstruction

The ordering rule needs to refuse only the *membership* of a reply issued before the
newest close, but `fetchSlots.fulfilled` refuses such a reply **wholesale**: content
carried in it — titles, previews, running state — is discarded along with the list.
This is deliberate and is bounded today, because live SSE pushes keep applying content
to the rows that remain, so the refusal costs freshness rather than correctness. It is
recorded here rather than narrowed because splitting the refusal would add a second
merge path through `applySlots`, which runs on every slots frame, for no correctness
gain — and the server-stamped generation below removes the refusal entirely.

### The replacement

Have the slots payload carry a monotonically increasing generation, bumped
server-side whenever membership changes. A reply then carries its own position
in the sequence, so the ordering rule collapses to comparing that integer
against the newest one applied, and every piece above becomes deletable: no
tombstones to withhold and retire, no per-read generation capture, no
confirming read. The client would keep an optimistic hide for latency, but it
would no longer be load-bearing for correctness.

Exactly what a generation deletes, which is the scope any issue tracking this work
would carry:

- `CloseTombstone` — the type, and `closingSlots` with it.
- `closeSeq` — the client-side close counter.
- `pendingSlotReads` — the per-read generation capture.
- `CloseTombstone.retireReadId` — and the confirming post-DELETE read it names.
- `membershipMoved` — needed only to date the list from the client side.
- The wholesale refusal described above, and with it that coarseness.
