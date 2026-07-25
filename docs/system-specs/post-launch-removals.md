# Post-Launch Removals

Code that exists **only** to carry pre-launch users across a data migration, and
should be deleted once the product ships to net-new users (who have no legacy
data to migrate). Track each item here; delete the code and its row together.

> Why a doc and not a `TODO`: these removals span multiple modules and are safe
> to do **only** after launch (they would strand a pre-launch developer mid-
> migration if removed early). Grouping them keeps the "is it safe to delete
> yet?" decision in one place.

## Legacy `~/.kirocrew` → `~/.kiro/crew` data-home migration

**Remove after:** the first public release. All post-launch users are net-new
and start directly in `~/.kiro/crew`; nobody has a `~/.kirocrew` to migrate, so
the entire one-time migration path is dead weight and can go.

**Precondition before removing:** confirm no still-supported upgrade path
originates from a build that wrote `~/.kirocrew`. If any pre-launch user could
still be on such a build, keep the migration until they have upgraded past it
(the migration is idempotent and a no-op once `~/.kirocrew` is gone, so it is
cheap to leave one release longer than strictly necessary).

**What to delete** (all in service of the legacy move — grep `\.kirocrew` and
`LEGACY_CONFIG_DIR_NAME` to find stragglers):

- `src/kiro_crew/home_migration.py` — the whole module (`migrate_home`,
  `_do_migrate`, `_copy_overwrite`, `_make_copy_ignore`, `_verify_copy`,
  `_gateway_is_live`).
- `src/kiro_crew/config/paths.py`:
  - `_maybe_migrate_legacy_home`, `_legacy_home`, `LEGACY_CONFIG_DIR_NAME`,
    `MIGRATION_MARKER_NAME`, `_finalize_fresh_home`.
  - `_sweep_ungated_archive_leftovers` + `_ARCHIVED_LEGACY_DIR_NAME` /
    `_PRE_MIGRATION_BACKUP_DIR_NAME` (cleanup of even older archive/backup
    leftovers — also legacy-only).
  - Simplify `config_dir()` / `ensure_data_home()` to just resolve + `mkdir`
    `~/.kiro/crew` (keep the `KIROCREW_HOME` override and the system-dir guard).
- `src/kiro_crew/security.py` — the `.kirocrew` sensitive-path spelling
  (`_CREW_HOME_PREFIXES`, the `sensitive-file-read-cat-kirocrew-env` rule, and
  its `test/fixtures/denied_commands_golden.json` entry) protects credentials in
  a leftover legacy home from agent file access. Drop the `.kirocrew` spelling
  (keep `.kiro/crew`) **only after** confirming no machine can still have a
  `~/.kirocrew` on disk — i.e. every supported upgrade origin has already run
  the migration and deleted it. Removing it while any legacy home could persist
  would un-gate real credentials. The `kirocrew ... token` credential-exfil rule
  (`.*kirocrew.*token`) matches the CLI *name*, not the path — leave it.
- `test/test_home_migration.py` — the whole file.
- Docs: the "Data Home Location & Migration" section of
  `docs/system-specs/modules/config.md` collapses to "data home is
  `~/.kiro/crew` (override with `KIROCREW_HOME`)"; drop the migration
  subsections. Remove this row from this file.

**Do NOT confuse with (these stay — permanent, not migration scaffolding):**
- the `~/.kiro/crew` resolution itself, the `KIROCREW_HOME` override, and the
  security keystone for `~/.kiro/crew`;
- the recovery breadcrumb (`_write_recovery_breadcrumb`,
  `RECOVERY_BREADCRUMB_NAME`) — it points at the **current** home and lives
  outside `~/.kiro/` specifically to survive a Kiro-family uninstaller wiping
  `~/.kiro/`, so it is a permanent diagnostic, not a signpost for the legacy
  move. (Its message string still names `~/.kirocrew` only as the file's own
  location; that is cosmetic.)
