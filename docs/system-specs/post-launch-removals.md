# Post-Launch Removals

Code that exists **only** to carry pre-launch users across a data migration, and
should be deleted once the product ships to net-new users (who have no legacy
data to migrate). Track each item here; delete the code and its row together.

> Why a doc and not a `TODO`: these removals span multiple modules and are safe
> to do **only** after launch (they would strand a pre-launch developer mid-
> migration if removed early). Grouping them keeps the "is it safe to delete
> yet?" decision in one place.

## Legacy `~/.kirocrew` security-path spelling

The one-time `~/.kirocrew` → `~/.kiro/crew` data-home migration has been removed
(its module, the resolver's migrate branch, its tests, and its row here are
gone). One piece was **deliberately kept**, conservatively diverging from the
"delete the code and its row together" rule above: the `.kirocrew`
**security-path spelling** still gates credentials in any leftover legacy home.

**Remove after:** confirming no supported machine can still have a `~/.kirocrew`
on disk — i.e. every legacy home has already been removed by its owner. Because
nothing migrates or deletes `~/.kirocrew` any more, a legacy home persists until
the user removes it, so this is stricter than a "post-launch" cutoff. Removing
the spelling while any legacy home could persist would un-gate real credentials
(`.env`, `token_signing.key`, `security_policy.json`, …).

**What to delete then:** the `.kirocrew` spelling in `src/kiro_crew/security.py`
(`_CREW_HOME_PREFIXES`, the `sensitive-file-read-cat-kirocrew-env` rule, and its
`test/fixtures/denied_commands_golden.json` entry). Keep `.kiro/crew`. The
`kirocrew ... token` credential-exfil rule (`.*kirocrew.*token`) matches the CLI
*name*, not the path — leave it.

**Do NOT confuse with (these stay — permanent, not migration scaffolding):**
- the `~/.kiro/crew` resolution itself, the `KIROCREW_HOME` override, and the
  security keystone for `~/.kiro/crew`;
- `legacy_home()` / `LEGACY_CONFIG_DIR_NAME` — still consumed by autonudge, seed,
  session storage, and other legacy-path readers, independent of the removed
  migration;
- the recovery breadcrumb (`_write_recovery_breadcrumb`,
  `RECOVERY_BREADCRUMB_NAME`) — it points at the **current** home and lives
  outside `~/.kiro/` specifically to survive a Kiro-family uninstaller wiping
  `~/.kiro/`, so it is a permanent diagnostic, not a signpost for the legacy
  move. (Its message string still names `~/.kirocrew` only as the file's own
  location; that is cosmetic.)
