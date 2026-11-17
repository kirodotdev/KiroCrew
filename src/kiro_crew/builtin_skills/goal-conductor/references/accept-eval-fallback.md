# Handing the evaluator a document, and the shell fallback

Read this before you hand `accept_eval` a document yourself, and before any
shell call to `scripts/accept_eval.py`. The `accept_eval` tool with no
arguments, or with `item_id`, needs none of it.

## Handing it a document yourself

This is for a bar you want judged outside the tool's own `done`-only
selection. Take the `accept_batch` from a full `work_ledger_read` (no
`compact`), **keep only the entries whose item is currently `status: done`** —
each entry carries that status, so the filter is a read of the document you
already have — and pass that filtered document as its arguments
(`{"items": [<the done entries>]}`). Then the filter is yours to apply, and it
is not optional. `accept_batch` is composed from every open item whose
`acceptance` is concrete, whatever its status. An item whose bar still carries
a placeholder (a `TBD` or blank `pr`, an unknown kind) is already left out, and
its `acceptance_concrete` flag says so. It is the two-phase promotion seam, not
a verdict gate. Never pipe the unfiltered document, and never pass it to the
tool.

## Only without the `accept_eval` tool

Pipe the same filtered document through the script with a **quoted heredoc**
(one approval per call):

```bash
python3 <this skill's dir>/scripts/accept_eval.py <<'ACCEPT_BATCH'
<the accept_batch document, with every non-done entry removed>
ACCEPT_BATCH
```

**The heredoc is load-bearing, not style.** `acceptance` holds text you built
from ingested content — an issue title, a file path a worker named — and a
`file` path carrying a single quote would end a `'...'` string early and hand
the rest of the value to the shell as a command, which `execute_bash` then runs
after one approval. A heredoc whose delimiter is quoted (`<<'ACCEPT_BATCH'`) is
the one form the shell copies to stdin without interpreting anything inside it.
Never paste the document into a `printf '%s' '...'` or `echo '...'` argument,
and never let the document contain a line that is exactly `ACCEPT_BATCH`.

**Resolve `<this skill's dir>` from where this SKILL.md was actually loaded
from** — the skill index names its absolute path. Do NOT hardcode a path under
the default skills root: a `KIROCREW_HOME` override moves it, so on such an
install that path does not exist and every evaluator call would fail before
patrol ever ran.

Evaluate every `done` item in ONE call: a shell fallback call costs one
approval prompt.
