# Take-away changes

A take-away change removes something main has today: it hides a path, deletes
records, tightens a validator, migrates or prunes data, or removes or renames a
key or a public function. CI rarely catches the break, because the reader that
breaks is usually on a different entry point than the one the author tested.

## The rule

1. Grep `origin/main` for every reader of the thing you take away. Search every
   entry point: chat, cron, subagent, app bundles (`apps/<app>/`), the crew page,
   and the release branch. Do not derive the list from the Goal or from the docs.
2. List each reader under `## Backwards compatibility` in the PR body:

   ```
   Reader: <path>:<symbol> -- <entry: chat|cron|subagent|app|crew page|release> -- <why it still works | test name>
   ```

   When nothing is taken away, write exactly one line: `Removes nothing: <why>`.
3. Add one test per reader that proves it still works. A reader you break on
   purpose is a `Breaking:` change: name it and do the writer sweep the PR
   template asks for.

## What CI checks

PR Hygiene runs `.github/scripts/take_away_check.py` on the merge-base diff. On
a take-away shape (a `*_migration.py` file, a new `extra_hidden_dirs=` argument,
a deleted `config.agents` row, a removed module-level public `def`) in `src/`
it requires at least one well-formed `Reader:` line. `Removes nothing:`
does not satisfy a hit. It checks presence and format only; whether the list is
complete is the reviewers' call. The shape list is small and misses take-aways
it cannot see in one line (a widened mask tuple, a removed config key, a
stricter validator): the rule above still applies to those. The shapes name
today's seams (`extra_hidden_dirs`, `config.agents`); a PR that renames one
updates the detector in the same commit.

Check it locally before pushing:

```bash
git diff "$(git merge-base origin/main HEAD)" HEAD > /tmp/pr.diff
PR_BODY="$(cat pr-body.md)" python3 .github/scripts/take_away_check.py /tmp/pr.diff
```

`diff_signals.py --check-body` in the prepare-pr skill runs only
`pr-description-check.sh`, not this check.

Design Review also runs a TAKE-AWAY CHECK and blocks on a reader it finds broken
and unlisted; a reader a `Breaking:` line names counts as listed. That lane is
described in [ci-and-reviews.md](../../ci/ci-and-reviews.md).

## Worked examples

**#13273, hide.** It masked `<config_dir>/apps` from every cron child. Its body
said "Compatible: ... No documented, reachable capability is removed": it checked
the docs, not the code. The missed reader was every app script cron that imports
its own `apps/<app>/src`. Reverted by #16072.

The reader line it should have carried, with the test that now pins it:

```
Reader: src/kiro_crew/cron_script.py:run_script_sandboxed -- app -- test_app_script_cron_sibling_import.py::TestAScriptCronNamingTheBundle
```

**#12798, delete.** It pruned sync-generated crewmate `config.agents` rows after
checking only the Crew page. Chat resume, subagent continue/spawn and the prompt
builder still read those rows. Fixed by #16016 and #16033. Its reader lines:

```
Reader: src/kiro_crew/session_agent_selection.py:resolve_session_agent_bindings -- chat -- test_resume_after_crewmate_prune_resolves_installed_agent
Reader: src/kiro_crew/subagent_manager/admission/gate.py:resolve_spawn_execution -- subagent -- test_pruned_crewmate_records.py
Reader: src/kiro_crew/execution_context.py:execution_from_record -- subagent -- test_decoder_reads_a_pruned_synced_crewmate_as_its_template
```
