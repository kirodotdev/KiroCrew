# Phase 0: what to settle before a new PR, and the squash

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core.

- **Issue and tier.** The PR's issue exists and has a tier (see *Issue first*).
  A `tier:T3`/`tier:T4` issue waits for `triaged` before you build. A user-visible
  feature, or a whole-PR diff (`origin/<base>...HEAD`) over ~1k lines, also needs
  the maintainer's sign-off on the design **and the UI placement** first: an
  *unreviewed* large change is what turns into a twenty-round loop.
- **File overlap.** Check each file you touch against every open PR, paged, never capped (`references/ci.md`). If another open PR deletes or rewrites (>50% line delta) one of your files, STOP and ask which PR hosts the work.

## Phase 1 step 4: squash to one commit

4. **Squash to one commit** (`single_commit` only). Write the message (subject, then detail) to `$(git rev-parse --absolute-git-dir)/prepare-pr-commit-msg-<branch>.txt` (`/` as `-`; consumed), or pass `--squash FILE` (kept), then `python3 $SKILL_DIR/scripts/push_guard.py --base <base> --squash`. It commits the branch's **tree**, never an index, with hooks and signing. **0** → squashed; **40** → STOP, read stderr: commits ahead, all yours → its `--max-ahead N`, once; paths it did not commit → read each diff, name them after `--` only if all yours; **41**/**64** → follow the remedy; **2** → env.
