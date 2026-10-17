# Security Conductor — Field Lessons

These rules come from past audits. Each one closes a mistake an auditor, a
verifier, a fixer or the conductor really made. They are reference, not scope:
the rules of engagement still decide what anyone may touch. When an approved
lesson in the live ledger disagrees with a rule here, the ledger row wins.

The conductor points each seed at the sections it needs. Auditors read
"Finding a defect" and "Writing a proof of concept". Verifiers read "Verifying
and reading the ledger". Fixers and fix contracts use "Scoping a fix". Everyone
reads the last section.

## Finding a defect

- **Check every sibling of a guard.** When one path carries a guard, list the
  other callers of the same resource. Each one needs the same guard. The guarded
  sibling is your evidence, and a comment there naming the threat is the
  strongest kind.
- **Walk the route table, not the resource.** List every registered route of a
  handler family. List every caller of the shared owner check. Diff the two
  lists. A route missing from the second list is a candidate.
- **Group gaps by module.** A handler module with zero calls of the shared owner
  check is one finding, not one per route. Cite its worst route as the proof and
  list the rest in the same record.
- **Census by side effect, not by HTTP verb.** Name the internal function that
  does the harm. Find every handler that reaches it, GET routes included. A read
  route that runs a sweep or a lazy start can do the same harm as the POST.
- **Check body fields that pick the target.** A route may authorize on its path
  but act on a body field such as `source`, `repo`, `target` or `url`. Test the
  caller's own path with someone else's value in that field.
- **Find every writer of a config key.** A key with its own setter route may also
  be written by a generic config route, an import route or the CLI. Scope a
  surface by the state it changes, not by the route prefix. A fix that gates one
  writer and leaves another open is not done.
- **A prefix sibling is not coverage.** A protected `/x/approve` does not protect
  `/x`. Key each row on the exact method and path, and resolve it to its handler.
- **A gate counts only if it compares the caller with the owner.** Read the body
  of every helper named owner, ownership, gate or guard. An app-token check, a
  slot binding check or a store lookup is a different test. It does not stop a
  non-owner.
- **Comments are claims, not evidence.** A comment on an unguarded path that says
  another layer enforces the rule must be traced to the line that decides. When
  the comment is wrong, record it in the finding so the fix corrects it too.
- **A known-debt list is not a verdict.** A route on that list is still missing
  its gate. File it and say it is listed. Also report how much of the class the
  list's ratchet really walks, and how much it never sees.
- **Read a guard's earlier branches.** Before you call a guard bypassed, read the
  function from the top. List each branch that returns or raises first. Cite the
  line where your input really slips through.
- **Read the partner route.** For enable/disable, install/uninstall, start/stop
  and create/delete, audit both in the same pass. A handler is gated only when the
  owner check sits before every branch that changes state.
- **Diff the signed payload against the loader.** Read the list of fields the
  signature covers and the list of fields the loader runs. Any field the loader
  turns into a command, import, argv, server spec or path, and the signature
  skips, is a finding. List every such field, even if the proof pins one.
- **Audit a redactor as a matrix.** Cross each declared secret format with each
  carrier: text, blob, file and structured field. Then list every return path of
  the handler family: version forks, legacy branches, error branches and sibling
  endpoints. Each cell needs its own case. A green suite over one cell proves
  nothing about the others.
- **Read composed controls by asking them.** A control built from other controls
  may not carry the attributes its consumers read. A default value on a missing
  attribute is a silent miss. Grep for each attribute name the consumers expect.
- **Look for the fix inside the accused call.** For a validate-then-use window,
  read the launch call's keyword list. A descriptor-based parameter wired on one
  platform only is both the finding and its fix.
- **Split env hardening into two parts.** Scrubbing inherited secrets is a real
  boundary. Suppression flags are not, when the child runs attacker-written
  command text, because the child can unset them.
- **Audit the delta every round.** Treat the change since the last base as its own
  surface. Look for names that left a security list and new settings that skipped
  their siblings' checks. Name the change that moved them.
- **Dedupe against open findings field by field.** Before filing a coverage gap,
  read the open findings on the same builder or list. File only what is new.

## Writing a proof of concept

- **Assert the precondition, then add a positive control.** Show the setup holds
  before the act. Add a case that passes when the defect is absent. Without both,
  a red test cannot be told apart from a broken one.
- **Call the product, never a copy of it.** Import the seam you accuse. Name the
  production symbol each assertion drives in the docstring. A harness that copies
  old behaviour stays red after the fix.
- **One proof node per claimed path.** A title may name only what the proof ran.
  Mark any path you only read as a static claim, or drop it.
- **Carry the proof to the gain.** Show what the attacker holds after the last
  gate. List every remaining control on the path and show the proof did not open
  one of them itself.
- **Give an owner-gate proof every caller class.** Use the owner as a control, a
  non-owner session, and an app token whose app name matches the route. A 200 for
  the non-owner means a missing gate only when the owner also gets 200.
- **Test writability as the sandboxed user.** Settle a planted-path precondition
  at the exact directory, not by reading the shell denylist. One fence bounds one
  write path among many.
- **Show the byte that ships.** When two siblings call the same steps in a
  different order, run the correct order on the same input as a control. It is a
  finding only if the wrong order ships a byte the right order withholds, and the
  attacker had no cheaper route to that byte.
- **Confirm the field you tamper exists.** A tamper of an attribute the live model
  does not have proves nothing.
- **Title from what the proof asserts.** Add each further affected route as its
  own entry. Where a sandbox sits on the path, say what it bounds and what it
  does not.
- **Keep attack text out of argv.** A title, reason or docstring that quotes a
  denied command is argv too. Describe the command in words instead.

## Grading severity

- **Grade the gain beyond the caller's grant.** First write what the caller's
  grant already allows through the documented path. Grade only what the bug adds:
  a new target, a new principal or a new capability. Put both sentences in the
  finding.
- **Count the fences that survive.** When a finding switches off one safety fence,
  list the ones that keep enforcing. "One fence is off" is not "every fence is
  off". Grade by `severity_scale` alone, and put the survivors in the finding.
  Read the survivors at the enforcement layer, not from the settings the route
  writes.
- **Grade each site on its own.** One code smell across a census can carry very
  different severities. Record each site's sandbox mode and which host classes
  its scrub fails on. Never copy the worst sibling's grade.
- **A duplicate does not carry its parent's grade.** Grade the new residue on its
  own reach.

## Verifying and reading the ledger

- **Run the skill's scripts with the project's own interpreter.** Use the one
  that has pytest and the project installed, with the worktree's source on the
  import path. "pytest wrote no result report" is a tooling fault. Re-run it; do
  not hand it to a human.
- **Check the proof collects first.** Run `pytest --collect-only -q <nodeid>` in
  the exact tree. "Selected nothing" means the proof file is missing from that
  tree. Fix the setup and re-run.
- **A red result is not always a reproduction.** Open the failure. The assertion
  that names the defect must be the one that failed. An exception raised before
  it means the proof is stale.
- **Check where a clean red came from.** Its inputs must come from imported
  product code, not a helper inside the proof.
- **Run one scanner call per file.** An exit code over a file set proves at least
  one file failed, not which. Before calling a grant too wide, check each job for
  its own permissions and whether it uses them.
- **Run `verify_finding.py` only on the filed tree or on main.** Never run it on a
  fix branch. Use `verify_fix.py` there.
- **Record `fixed` once, after the merge.** Run `verify_finding.py` on main. When
  it reports `rejected` because the proof now passes, record
  `ledger.py record-verdict --finding ID --role verifier --verdict fixed --reason "<PR> <merge sha>"`.
- **Split `rejected` rows by time.** Rows before the fix opened are real
  refutations. A `rejected` row from that main run, saying the proof passed,
  means the fix holds; the `fixed` row after it records that. Count only the
  first kind as a disagreement.

## Scoping a fix

- **Scope a fixer to the findings it names.** A coverage test for a recurring
  class is a separate proposal for the human. A census test that needs hundreds
  of exempt rows and breaks on unrelated routes is too much.
- **Report a recurring class as a rule-scope problem.** When the same missing
  check keeps coming back in new modules, say so to the conductor. A regression
  test that walks a fixed module list will not catch the next one.
- **Fix every writer, every branch, every partner.** A fix is incomplete while
  another writer of the same key, another branch of the same handler, or the
  partner route stays open.
- **Test the compensating control you cite.** Read the control's precondition.
  Drive one input that fails it. If the control skips that input, it is not a
  control.
- **Narrow to one caller class only with a real caller.** Leave app tokens out of
  an owner gate only when a real app-token caller of that route exists. If none
  exists and the side effect reaches the gateway, gate every caller.
- **Build the contract from the worktree.** Set max changed files to the source
  and test files the fix may touch. Check each allowed and forbidden path exists
  on the fix base. When the gate says the contract is violated, list the paths
  it counted before you change the diff.
- **Keep the audit proof separate.** The fixer copies the audit proof in
  untracked, at the exact path the ledger names. The regression test gets its own
  name. Never rename one to match the other.
- **Fix the misleading comment too.** When a finding cites a false comment, the
  fix corrects or deletes it.

## When a policy fence refuses a legitimate call

- **Stop, record, report.** After any refusal, stop that line of work. Record it
  as a `policy_block` event with the command shape only. Report it and wait for
  the retrospective's ruling. Never retry the refused call in another form: no
  re-spelling, encoding, splitting, renaming or shortening.
- **Write calls in a safe shape from the start.** These shapes are how a call
  is written before it runs, never a way to retry one that was refused.
- **Recorded in `golden-paths.json`:**
  - Pass prose by file: `git commit -F <file>`, `gh pr comment --body-file <file>`.
  - Push a literal named branch, with no command substitution on that line.
- **Recommended (not corpus rows):**
  - Run each script as its own single-purpose command, not one long chain.
  - Write a program to a file and run the file, instead of an inline program.
  - Run guards and `gh pr create` as separate calls from the push.
  - Write each run's output to a new directory instead of deleting an old one by
    absolute path. Keep deletes out of long chains.
  - Describe a denied command in words in any title, reason or summary. Never
    quote it.
- **`golden-paths.json` wins.** These lists are a reading aid. When they and the
  JSON corpus disagree, follow the corpus.
