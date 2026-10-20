# The oversized-image session wedge, and how to unwedge a transcript

If a session started failing every turn with this, you are here for the
recovery command at the bottom:

```
messages.60.content.0.image.source.base64.data: At least one of the image
dimensions exceed max allowed size for many-image requests: 2000 pixels
```

## Why one image kills a whole conversation

The provider rejects the ENTIRE request when a many-image conversation carries
any image wider or taller than `imaging.MAX_IMAGE_EDGE_PX`. kiro-cli replays the
whole message history on every turn, so the offending block sits at a fixed
history index that nothing evicts.

That produces the worst possible split between cause and symptom:

| | |
|---|---|
| The turn that STORES the image | succeeds, silently |
| Turns after it, while the session is small | succeed -- under ~20 images the request is not "many-image" |
| Every turn once enough images accumulate | fails, forever, with a message naming a message index rather than the image |

Capping new captures cannot heal a transcript that already carries one. The
stored bytes have to be rewritten or the conversation is over.

## Where the caps are, and the path that escapes them

| Enforcement point | Covers |
|---|---|
| `acp/prompt_blocks.py` | images entering a PROMPT (what a user attaches) |
| `mcp_gateway/image_budget.py` | images an MCP TOOL returns |
| `computer_use/types.py` `MAX_SCREENSHOT_MAX_PX` | Kiro Crew's own window captures, bounded by the inline cap |

The path that escapes all three: an agent writes a screenshot to disk, then
opens it with kiro-cli's built-in `read` tool in Image mode. That tool reads the
file itself and writes raw bytes straight into kiro-cli's own transcript, so no
Kiro Crew code is on the path and there is nothing to intercept.

This is why the cap has to hold on the FILE, before it is read. Note that no
launch option can guarantee it: a `fullPage` capture takes the whole document
height regardless of viewport or `deviceScaleFactor`, so pinning a viewport
reduces incidence without bounding anything. Skills that instruct a capture
carry a downscale step for this reason
(`builtin_skills/web-verify/scripts/downscale_image.py`).

## Recovery

When a turn is refused for this reason the dashboard error card detects the
provider's many-image dimension rejection (the `image_dimension_wedge` tag,
classified from the raw error frame in `acp/transport_errors.py` and set only for
the kiro-cli backend, whose transcript the repair knows how to rewrite) and
offers **Repair images and resume** instead of Resume. Pressing it `POST`s to
`/api/chat/slots/{slot}/repair-images`, which stops the slot's kiro-cli process
(releasing the transcript lock), runs the repair below on the now-idle transcript
with its `.pre-image-repair.bak` sidecar, re-arms the resume spawn, and re-issues
the interrupted turn. No turns are lost -- unlike `reset-conversation`, which
starts a fresh conversation.

The same repair runs from a terminal against the transcript directly:

```bash
# Dry run first -- it changes nothing and prints what it would rewrite:
python -m kiro_crew.session_image_repair ~/.kiro/sessions/cli/<session-id>.jsonl
# Then, with the session ENDED (not merely idle -- see below):
python -m kiro_crew.session_image_repair --apply ~/.kiro/sessions/cli/<session-id>.jsonl
```

Notes on what it does and does not do:

- It refuses to run against a transcript whose sibling `<session-id>.lock` names a
  live process, because a rewrite while kiro-cli is appending would drop the
  appended turn. `--allow-live` overrides that; you almost certainly do not want
  it. Note that the lock file's mere presence is not the signal -- stale locks
  accumulate in that directory without bound -- so the pid is probed.
- It always writes a `.pre-image-repair.bak` sidecar, through an owner-only
  no-follow write so a symlink planted at that predictable name cannot redirect
  it -- and it REFUSES if a sidecar is already there rather than replacing it.
  The first sidecar is the only copy of the pre-repair transcript, so if an
  earlier run dropped an uncappable image those bytes exist nowhere else. Move
  the old sidecar aside if you really want to repair again.

  This makes dashboard repair a **once-per-chat** recovery, and that is an
  accepted limit, not a gap. Rotating the backup (a numbered or timestamped
  sidecar) so a second repair could run was considered and rejected: it reverses
  this module's single-pre-repair-copy guarantee, and a second wedge in the same
  chat is the uncommon case: the first repair downscales the offending images in
  the stored transcript itself, so replaying that repaired history no longer
  trips the cap. A re-wedge therefore needs a *new* oversized read added to the
  chat after the repair, not a replay of the same images — a fresh event, and
  the general prevention of it is the upstream read-tool cap
  (kirodotdev/Kiro#11663, out of this PR's scope). A second wedge whose
  sidecar already exists is refused with a distinct `image_repair_backup_exists`
  code; the dashboard card states the limit up front ("Works once per chat") and,
  on the refusal, offers a fresh session that keeps the chat on screen rather
  than leaving a dead end.
- It also re-stats the transcript immediately before replacing it and refuses if
  it moved. That narrows the write window to two syscalls; it does not eliminate
  it, since there is no lock protocol to join.
- Untouched lines are passed through byte-for-byte. Only the records being
  parsed are decoded, so a malformed byte sequence elsewhere in the file is
  neither destroyed nor a reason to refuse the repair.
- A generic deep walk runs alongside the anchored traversal purely as a
  tripwire. If it finds image-shaped blocks the anchored shapes could not reach,
  the report says so and refuses to look clean -- because the dangerous failure
  for this tool is not a crash, it is reporting "all within cap" on a transcript
  that is still wedged. Those blocks are never rewritten; a false alarm from a
  tool payload that merely looks like a block is the acceptable direction.
- An image with no compliant rendition becomes a text marker rather than being
  left in place. Losing one image beats losing every later turn.
- It is not a sweeper and not a `kirocrew` subcommand, on purpose: it rewrites
  another tool's data directory, so it stays an explicit operator action. The
  dashboard's "Repair images and resume" button is that explicit operator action
  -- a human pressing "repair this session" -- so wiring the module behind it
  does not weaken the constraint; a background sweeper still would.
