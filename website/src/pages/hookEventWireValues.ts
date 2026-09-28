/**
 * Hook event names, as the backend spells them.
 *
 * These are WIRE VALUES, not copy: the API matches them by value against the
 * backend's own event allowlist (`kiro_crew.hooks.HOOK_EVENTS_ALL`), and the hook
 * table renders the stored value verbatim, so a translated or reworded one is
 * rejected on save and would disagree with its own row. They live here, apart from
 * the page, so that boundary is visible in one place and the i18n literal-string
 * lint can be scoped to exactly this file.
 *
 * The first five are the events the gateway fires. The six after them are triggers
 * a Kiro Agent session owns: authorable and stored, and fired by no event — the
 * gateway has no lifecycle moment for them and no other reader exists, though the
 * row's Test button still runs the command on demand. A Kiro
 * Agent asks its client for hooks from a fixed set of trigger names that includes
 * `preTaskExecution` and `postTaskExecution` and excludes the file and manual
 * ones, so those four are stored for the record rather than for an imminent run.
 *
 * The order is the order the picker offers, and the order `EVENT_ORDER` sorts the
 * PROVIDER hooks table by. The script-hook table sorts its own rows through
 * `hookComparators.event`, a locale collator, so reordering this list does not
 * move those rows.
 */
export const EVENTS = [
  'AgentSpawn',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'Stop',
  'PreTaskExecution',
  'PostTaskExecution',
  'FileCreated',
  'FileEdited',
  'FileDeleted',
  'UserTriggered',
]

/**
 * The six no event fires, and the two of those a Kiro Agent asks its client for.
 *
 * Mirrors `kiro_crew.hooks.HOOK_EVENTS_KAS_ONLY` and
 * `HOOK_EVENTS_AGENT_REQUESTED`; `test_hook_events_kas_triggers.py` pins both
 * against those tuples, so the round that ships delivery moves a name out of a
 * tuple instead of rewriting copy in five places.
 */
export const KAS_ONLY_EVENTS = [
  'PreTaskExecution',
  'PostTaskExecution',
  'FileCreated',
  'FileEdited',
  'FileDeleted',
  'UserTriggered',
]

export const AGENT_REQUESTED_EVENTS = [
  'PreTaskExecution',
  'PostTaskExecution',
]
