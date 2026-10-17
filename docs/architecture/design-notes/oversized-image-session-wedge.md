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

Dry run first -- it changes nothing and prints what it would rewrite:

```bash
python -m kiro_crew.session_image_repair ~/.kiro/sessions/cli/<session-id>.jsonl
```

Then, with the session ENDED (not merely idle -- see below):

```bash
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
  another tool's data directory, so it stays an explicit operator action.
