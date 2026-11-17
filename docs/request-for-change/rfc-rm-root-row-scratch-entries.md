---
title: "The rm root row stops refusing a plain rm of named /tmp entries"
status: draft
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 51349e12e7
doc-pr: null
implementation-prs: [36536]
tracking-issues: [19473, 35629]
supersedes: []
superseded-by: []
---

# RFC: The rm root row stops refusing a plain rm of named /tmp entries

## 1. Decision

The catalog row `local-destructive-rm-rf-root` keeps its pattern. It stops
refusing a command whose only match is a plain `rm` of named entries directly
under `/tmp`. The repository owner made this decision on 2026-11-12,
after reading the recorded refusals: "why do we need to block deleting
/tmp/x? That seems wrong."

This changes a safety row's behaviour, and `rfc-agent-temp-reclaim.md` lists
"narrow `rm -rf /.*` to the root itself" as a rejected alternative that needs
its own decision. This document is that decision.

## 2. What the row does today

Verified at `51349e12e7`.

- `denied_rules_catalog.py` defines the row with pattern `rm -rf /.*`. Its
  description says it blocks deletion "rooted at the filesystem root".
- `deny_gate.is_denied` matches the pattern anywhere in the command text, so
  any absolute target matches: `rm -rf /tmp/build`.
- Only that literal spelling is refused. All of these are allowed on main:
  `rm -fr /tmp/x`, `rm -r -f /tmp/x`, `rm --recursive --force /tmp/x`,
  `find /tmp/x -delete`, `cd /tmp && rm -rf x`, and `shutil.rmtree` from
  `python3 -c`.
- `rm_floor.py` separately refuses recursive force deletion of `/` and `~`
  themselves in every spelling. That floor is not touched.

In 132 refusals recorded on one host from 2026-10-28 to 2026-11-12, 25 came
from this row. None was a root or system-path wipe.

## 3. Why the rejected-alternative reasoning does not hold for /tmp entries

`rfc-agent-temp-reclaim.md` rejects narrowing because "it loosens a floor
agents rely on, and it still gives an agent no way to know whether a tree is
in use".

- **"A floor agents rely on."** For a `/tmp/<name>` target the row stops one
  spelling of five. Nothing that relies on it is protected; a respelling,
  which the system prompt forbids, gets through.
- **"No way to know whether a tree is in use."** True, and the row never
  answered that either. `kirocrew tmp reclaim` (#32853) is the checked path
  for trees an agent did not make. This decision is for the entry the agent
  just made and is about to recreate (`rm -rf /tmp/x && mkdir -p /tmp/x`),
  the shape of most of the 25 records (17 are relieved by the rule below).

## 4. The rule

The row reads a view of the command in which the words of each `rm` meeting
all of these are blanked. Every other row and the rm floor read the full text:

1. Its program word is exactly an unquoted `rm`: not `/bin/rm`, and not
   after `sudo`, `env` or another wrapper. The only exception is kiro-cli's
   `Running:` title prefix at the start of the text.
2. Every word is literal text, with no `$`, glob, brace, paren, tilde, quote
   or escape, and there is no `--`.
3. Nothing is glued on either edge. The character before `rm` and the
   character after its last word must each be a blank, a separator or the
   end of the text, and the shared reader did not cut this command, or the
   one before it, at a backtick or paren. Either would let bash add
   operands from a substitution's output.
4. Every operand is exactly `/tmp/<name>`, a single component that is not
   `.` or `..`. With no folder on the way, a symlink planted by an earlier
   command (`ln -s / /tmp/l; rm -rf /tmp/l/etc`) cannot redirect the delete.
   A gate-time realpath could not catch that, because the link does not yet
   exist when the gate runs.

Only the argv words are blanked. A redirection, and any substitution inside
it, stays in the text. Only the shipped row reads the view: an operator's
own copy of the same pattern keeps reading the full text. Every deny event
still records the command as submitted.

So these are still refused: `/tmp`, `/tmp/`, `/tmp/*`, `/tmp/x/`,
`/tmp/a/b`, `/var/tmp/x`, `/tmp/../etc`, `/tmp/$X`, a mixed `/tmp/x /etc`,
and a backtick glued to the rm.

## 5. Not covered

- Nested paths (`/tmp/a/b`) and `/var/tmp`. They still go to the row. In the
  recorded set this costs 3 of the 20 cases that would otherwise be relieved.
- `~/.cache/<name>` and other home paths: the `rm -rf ~.*` row is unchanged.
- Globs under `/tmp` (`/tmp/x.*`). They are still refused, because the text
  does not say what a glob expands to.
- Whether the tree is in use or owned by the caller. `kirocrew tmp reclaim`
  handles that.
- Windows and macOS. The rule reads only the text, with no filesystem call,
  so it behaves the same on every OS.
