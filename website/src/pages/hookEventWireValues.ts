/**
 * WIRE VALUES ONLY: the lifecycle event names the hooks API accepts.
 *
 * Every entry is matched BY VALUE against the backend's allowlist and stored
 * verbatim on the hook record, so a translated `AgentSpawn` is rejected by the API
 * and the picker silently stops working in that locale while adding a catalog entry
 * no one can act on. They still reach the screen as picker rows, the way the other
 * wire-value modules' values do.
 *
 * Kept in its own module rather than exempted where it is used: the hooks page
 * carries real user-visible copy, so a file-scoped i18n exemption there would
 * silence the gate over prose it exists to catch.
 *
 * ORDER IS THE PICKER ORDER, and it drives the hooks table's event ordering.
 */
export const EVENTS = [
  'AgentSpawn',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'Stop',
  'SessionLaneChanged',
]
