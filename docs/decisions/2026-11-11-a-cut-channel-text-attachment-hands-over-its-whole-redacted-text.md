# A channel text file or document too long for the prompt keeps its inline part and hands the agent its whole redacted text as a file

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

When the redacted text of a text file or document sent on a channel is longer
than the inline limit (`max_text_inject`, 50 KB by default), the prompt keeps
the same first part as before, the cut says how many characters were shown of
how many, and the whole redacted text is handed to the agent as a temporary
`.txt` file for that turn; text under the limit is unchanged, and text files
above 512 KB stay refused.

## Why

- A channel cut a long text file or document at 50 KB with a bare
  `[… truncated]` and deleted the file, so the agent answered as if it had the
  whole file and could not read the rest; a dashboard upload is handed over as
  a path.
- Keeping the inline part as it is means an agent that does not open files
  loses nothing, and every attachment under the limit behaves as before.
- The handed-over copy is the redacted text, never the raw download, so it
  hides exactly what the inline part hides; a document's copy is its extracted
  text, which the agent's read tool can open.
- Whether text files above 512 KB may be accepted stays open on the issue: it
  would either read them into gateway memory or hand over unredacted text.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32148#issuecomment-6784615979 --
  the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/32148#issuecomment-6759472657 --
  the reporter's request that the cut state its sizes.
