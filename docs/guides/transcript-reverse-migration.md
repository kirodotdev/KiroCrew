# Moving sessions back after a hand-set KIRO_HOME

When an operator sets `KIRO_HOME=<path>` in their shell profile, kiro-cli writes
new session transcripts to `<path>/sessions/cli/`.  Unsetting that variable later
re-points kiro-cli at `~/.kiro/sessions/cli/`, leaving already-written transcripts
inaccessible.  This guide covers the steps to move them back.

---

## Two starting states

Choose the path that matches where you are now:

### If `KIRO_HOME` is still set to the old path (preferred)

Stop the gateway **before** unsetting `KIRO_HOME`.  Running a session lookup
after the revert — even one resume attempt — causes `SessionMap.get` to prune
any session whose `<sid>.json` is absent from `kiro_sessions_dir()`, and that
prune is not reversible.

```bash
kirocrew stop        # wait for the gateway to exit
# ... run the file-move recipe in the next section ...
unset KIRO_HOME      # or remove the line from your shell profile
kirocrew gateway
```

### If `KIRO_HOME` has already been reverted

Stop the gateway if it is still running:

```bash
kirocrew stop
```

Check whether the session map entries are still present before running the
recipe.  Look in `~/.kiro/crew/session_map.json` for the session IDs whose
transcript files are in the old path.  If the entries are gone, see
[If mappings were already pruned](#if-mappings-were-already-pruned).

---

## Before you move: identify the directories

| Name | Path |
|---|---|
| Source (old path) | `<path>/sessions/cli/` |
| Destination (host home) | `~/.kiro/sessions/cli/` |

`<path>` is the value your shell profile set `KIRO_HOME` to.  Find it in the
profile file where you wrote the `export KIRO_HOME=…` line (`~/.bashrc`,
`~/.zshrc`, or similar).

Verify files exist:

```bash
ls "<path>/sessions/cli/"
```

Each session leaves two files: `<sid>.jsonl` (the conversation transcript) and
`<sid>.json` (the state witness that makes the session resumable).

---

## Recipe

For each session ID `<sid>` in the source directory, run the following steps
**in order**.  The journal (`.jsonl`) must be copied before the state witness
(`.json`); do not swap the steps.

### 1. Check for a collision

```bash
ls ~/.kiro/sessions/cli/<sid>.json 2>/dev/null && echo "EXISTS"
ls ~/.kiro/sessions/cli/<sid>.jsonl 2>/dev/null && echo "EXISTS"
```

- **Neither file exists** — proceed to step 2.
- **Both exist, byte-equal to the source copies** — the session was already
  moved.  Perform step 4 (flush) and then step 5 (retire); skip steps 2 and 3.
- **Only `.jsonl` present, `.json` absent** — a previous attempt copied the
  journal but crashed before the state file.  Verify it first:
  ```bash
  cmp "<path>/sessions/cli/<sid>.jsonl" ~/.kiro/sessions/cli/<sid>.jsonl
  ```
  If equal, skip step 2 and proceed to step 3.  If different, delete the host
  journal and restart from step 2.
- **Files exist and differ** — divergent residue; do NOT overwrite.  See
  [Divergent residue](#divergent-residue).

### 2. Copy the journal (`.jsonl`) first

```bash
cp "<path>/sessions/cli/<sid>.jsonl" ~/.kiro/sessions/cli/<sid>.jsonl
```

Verify:

```bash
cmp "<path>/sessions/cli/<sid>.jsonl" ~/.kiro/sessions/cli/<sid>.jsonl
```

If `cmp` reports a difference the source was modified during the copy.  Delete
the destination copy and try again.

### 3. Copy the state witness (`.json`) last

```bash
cp "<path>/sessions/cli/<sid>.json" ~/.kiro/sessions/cli/<sid>.json
```

Verify:

```bash
cmp "<path>/sessions/cli/<sid>.json" ~/.kiro/sessions/cli/<sid>.json
```

### 4. Flush to disk

```bash
sync
```

### 5. Retire the source state witness

Delete only the `.json` from the source directory.  The `.jsonl` is kept: if
kiro-cli appended a turn between step 3 and this step, a retained journal keeps
it recoverable; a lone `.jsonl` with no matching `.json` is inert to resumability.

```bash
rm "<path>/sessions/cli/<sid>.json"
```

### 6. (Optional) Remove the retained journal

Once the session resumes correctly from the host home:

```bash
rm "<path>/sessions/cli/<sid>.jsonl"
```

---

## Failure-mode matrix

| Crash point | Source `.json` | Source `.jsonl` | Dest `.json` | Dest `.jsonl` | Retry safe? |
|---|---|---|---|---|---|
| Before step 2 | present | present | absent | absent | Yes — start over |
| After step 2, before step 3 | present | present | absent | present | Yes — verify equality then copy `.json`, continue |
| After step 3, before step 4 | present | present | present | present | Yes — if dest files equal source, flush and retire; if source changed, delete dest and retry |
| After step 4, before step 5 | present | present | present, durable | present, durable | Yes — retire source `.json` |
| After step 5 | absent | present | present, durable | present, durable | Done |
| After step 6 | absent | absent | present, durable | present, durable | Done |

---

## Divergent residue

Both directories hold the same `<sid>` with different content.  Compare:

```bash
wc -l "<path>/sessions/cli/<sid>.jsonl" ~/.kiro/sessions/cli/<sid>.jsonl
diff  "<path>/sessions/cli/<sid>.jsonl" ~/.kiro/sessions/cli/<sid>.jsonl | head -20
```

Do NOT delete either copy without understanding which holds more of the
conversation.  The longer `.jsonl` is usually the canonical one, but a shorter
file is not necessarily a subset — it may be a different conversation branch.
Only delete one copy when you have verified it is a strict byte-prefix of the
other (every line in it also appears at the start of the longer file).

---

## If mappings were already pruned

If `SessionMap.get` already ran under the reverted `KIRO_HOME` (a resume attempt,
a session list load, or a gateway start all trigger it), the effect depends on
the entry:

- **Regular session** (no channel binding, no durable or privacy flag):
  `_repair_or_remove_stale` removes the entry from
  `~/.kiro/crew/session_map.json` entirely.  Moving the transcript files at
  that point restores them to disk under `~/.kiro/sessions/cli/`, but the
  session no longer appears in the session list.

- **Session with a channel binding or a durable/privacy flag**: the entry is
  kept but `_stash_and_clear_sid` moves the `sid` into `discarded_sid` and
  clears the `sid` field.  The binding and history metadata survive; the session
  can be resumed with a code fix that re-populates `sid` from `discarded_sid`,
  but there is no operator-accessible command for this yet.

In both cases the conversation text in the `.jsonl` file is still readable and
complete.  Open it as plain JSON Lines to recover the messages.

---

## After the move

1. Remove the now-empty directory if no sessions remain:

   ```bash
   rmdir "<path>/sessions/cli/" 2>/dev/null || true
   ```

2. Unset `KIRO_HOME` from your shell profile or set it to the host home:

   ```bash
   export KIRO_HOME=~/.kiro
   ```

3. Restart the gateway.  kiro-cli will find the sessions in
   `~/.kiro/sessions/cli/` and resume normally.
