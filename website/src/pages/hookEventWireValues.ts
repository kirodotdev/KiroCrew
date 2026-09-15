/**
 * Hook lifecycle event names, as the backend spells them.
 *
 * These are WIRE VALUES, not copy: the API matches them by value against the
 * backend's own event allowlist, so a translated or reworded one is rejected. They
 * live here, apart from the page, so that boundary is visible in one place and
 * the i18n literal-string lint can be scoped to exactly this file.
 */
export const EVENTS = [
  'AgentSpawn',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'Stop',
  'SessionLaneChanged',
]

