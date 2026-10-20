import { crewDisplayName } from '../components/AgentSelector'

/**
 * The title a session row shows for a crewmate's own DM thread, or null for
 * every other session.
 *
 * That thread is one long-lived conversation with one crewmate, so the row is
 * named after the crewmate -- its display name, else its name -- and never after
 * a generated topic. Read from the thread's member binding (`mode === 'member'`
 * and `agent`) every time the row renders, so a row saved with an old generated
 * title shows the right name without its stored title being rewritten. The
 * crewmate's label is looked up in the roster the sidebar already holds; a
 * crewmate it does not list (or lists later) reads as its name.
 */
export function memberThreadTitle(
  slot: { mode?: string; agent?: string },
  agents: readonly { name: string; display_name?: string; selection_kind?: string }[],
): string | null {
  const name = slot.agent
  if (slot.mode !== 'member' || !name) return null
  const row = agents.find(a => a.name === name && a.selection_kind !== 'template')
  return crewDisplayName(row ?? { name })
}
