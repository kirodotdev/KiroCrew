# Regenerate the shared files last

Read this when a rebase conflicts in a generated or counted file, or a branch
changes the inputs of one.

## Why

Rebase conflicts are the top loop class in the study behind RFC D6
(`docs/request-for-change/rfc-review-loop-and-escaped-bug-reduction.md`):
1,017 loops in 426 of 1,980 pull requests. The same few files are named again and
again, and most of them are rebuilt by a tool.

## Regenerate last, on a fresh rebase

Order: rebase, resolve conflicts, `regen_shared.py`, amend, then the gates.

```bash
git fetch origin && git rebase refs/remotes/origin/<base>
python3 $SKILL_DIR/scripts/regen_shared.py          # --list shows the table
python3 $SKILL_DIR/scripts/push_guard.py --amend -- <the files it printed>
```

It runs a generator when the branch (or the base) changed the generated files **or
what the generator reads** (`website/src/**` for the UI index; a path a baseline lists
for the two baselines), so a file whose conflict you resolved by taking the base side
is still rebuilt. Expect the black baseline to take minutes when it runs. Exit **0**: nothing was stale, or the generators changed
nothing. Exit **20**: it rewrote files, so amend exactly those. Exit **2**: read the
message (a tool is missing, a generator failed, or a generator touched a file outside
its list; do not amend that).

**When the rebase stops on a file a generator owns**, do not merge it by hand. Take
the base side, finish the rebase, then run the script:

```bash
git checkout --ours -- <file>      # during a rebase, "ours" is the base you rebase onto
git add <file>
```

Verified on a throwaway repository: during `git rebase`, `--ours` is the upstream
side and `--theirs` is your commit, the reverse of a merge.

## Which files

Run `regen_shared.py --list` for the current table. At the time of writing:

| File | Generator |
|---|---|
| `src/kiro_crew/docs/ui-index.generated.json`, `website/src/uiLocations/guidePlans.gen.ts` | `npm --prefix website run gen:ui` (needs `npm ci` in `website/`) |
| `.github/file-line-cap-baseline.txt` | `scripts/check_file_line_cap.py --update-baseline` (deletes rows and lowers counts for files you touched, never adds) |
| `.github/black-baseline.txt` | `scripts/check_black_formatting.py --update-baseline` (checks the whole tree with `black --check` to find rows that are now clean; takes minutes, rewrites no source file) |

**Not covered, no generator:** `docs/feature-map/README.md` (hand-written rows),
`docs/architecture/mcp.md`, `test/test_security_gate_liveness.py` (the hand-counted
`_PACKAGE_LINE_BUDGET`) and `website/src/i18n/en.context.json`. After a rebase,
re-read these against the new tree by hand; the script prints a `HAND:` line when
your branch touches one.
