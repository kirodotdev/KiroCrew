---
title: "Shell floors read a cat/tee here-document body as data"
status: draft
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 51349e12e7
doc-pr: null
implementation-prs: []
tracking-issues: [23351, 34754]
supersedes: []
superseded-by: []
---

# RFC: Shell floors read a cat/tee here-document body as data

## 1. Proposal

The argv floors (ssh-self, self-kill, credential mint, git-publish) and the
two regex rows that guard the same actions (`self-protection-kill`,
`credential-exfil-kirocrew-token`) skip a here-document body only when all
of these hold:

1. The body's command is `cat` or `tee`, after optional `NAME=value`
   prefixes. The program word must be a plain literal. `bash`, `sh`,
   `python3 -` and every other reader keep their body scanned.
2. The tag is quoted: `<<'TAG'`, `<<"TAG"` or `<<\TAG`, where `TAG` is
   letters, digits and `_` only. Bash then runs no expansion inside the body.
   Any other tag spelling, such as `<<"E\\OF"`, keeps the body scanned.
3. A tag line closes the body. An unclosed body keeps its text scanned.
4. The command does not pipe its output (`cat <<'EOF' | sh` keeps its body).

A skipped body is blanked to spaces with its newlines kept, so every offset
still points at the submitted command. Every other row, the rm floor and the
argv rules keep reading the full text.

The code is not built. It waits for this RFC to be accepted.

## 2. Why now, and why this small

[#23351](https://github.com/kirodotdev/KiroCrew/issues/23351) parked this gap
because every earlier attempt (#23303, #19892, #19951, #19986) failed review on
a new bypass shape. It asks for a design before more masking. This document
is that design, kept to the one consumer whose behaviour is obvious: `cat`
and `tee` copy their input and run nothing.

Measured on one host, 2026-10-28 to 2026-11-12: 132 refusals, 99 of them
false positives. On `51349e12e7`, a prototype of this rule frees 8
ssh-self refusals and 1 self-kill refusal. The result is the same under
`PYTHONHASHSEED` 0, 3 and 6. Every recorded true positive is still refused.

### Rejected: also skip stdin-Python bodies

An earlier prototype also skipped `python3 - <<'EOF'` bodies. It used an `ast`
check that allowed only "safe" stdlib imports and no `exec`/`eval`/`__import__`
names. That version freed 15 more records: 9 mint, 6 kill-row and 3 ssh-self
whose verdict depends on DNS timing. It was dropped because a static
allowlist is not a sandbox. Each of these passes the check and runs a
command the floor exists to stop:

- `pathlib.os.system(...)`, `glob.os.system(...)`: an allowed module re-exports
  `os` as a plain attribute.
- `sys.modules['os'].system(...)`: `os` is already imported at startup.
- `sys._getframe().f_builtins['exec'](...)` and
  `(x for x in ()).gi_frame.f_builtins[...]`: frame objects reach the
  builtins without naming them.
- `typing.get_type_hints` on string annotations runs `eval`.
- `operator.attrgetter('os')(pathlib)` reads an attribute without naming it.

Each could be patched with another denylist entry. That is the whack-a-mole
#23351 warns about. Python edit scripts can use the file-edit tool or a
`.py` file instead.

## 3. The four #23351 constraints

1. **Case.** Body spans come from the shared reader
   `shell_commands.read_commands(text, heredocs=True)`, run on the raw
   `tool_name`, before the deny tiers lowercase it. A body line `eof` does
   not close tag `EOF`.
2. **Line continuations.** The reader is run on the unfolded text. With a
   quoted tag it folds no `\`+newline inside the body
   (`shell_commands._read_bodies`), as bash does.
3. **Delimiter dequoting.** Rule 2 of section 1 accepts only a
   `[A-Za-z0-9_]+` tag inside one quote pair or after one backslash, so no
   escape removal needs modelling. Any other spelling is not data.
4. **Performance.** One `read_commands` pass per `is_denied` call computes
   the view. The floors read it instead of the raw text. No per-`$(`
   rescan is added. The implementation PR must include the 5.8 KB
   `$(...)`-assignment script from #23351 as a timing test.

## 4. Known limit

`cat > a.sh <<'EOF' ... EOF; bash a.sh` writes a script and then runs it. The
body is skipped, and `bash a.sh` names nothing the floors read. The same
two-step shape already passes on main through the file-write tool or
`echo ... > a.sh; sh a.sh`. This proposal does not open it.

## 5. Not covered

- Any reader other than `cat`/`tee` (Python, node, `git commit -F -`).
- The ssh-self floor's DNS-timing dependence (`argv_floor_hosts.py`
  `_resolved_host_verdict`). It is a separate follow-up.
- The `rm -rf` rows. `rfc-rm-root-row-scratch-entries.md` covers them.
