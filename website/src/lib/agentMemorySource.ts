/**
 * Which memory a pick binds, for telling a same-name crewmate and custom agent
 * apart (#19472).
 *
 * Since #19304 the chat agent picker can list a crewmate next to a custom agent
 * (template) of the same name. The two read the same name, but they are
 * different bindings: a template pick runs on the shared `default` memory, while
 * a crewmate with its own store runs on that store. These helpers name the
 * difference, and only where it needs naming: when the list holds BOTH a
 * crewmate and a template of one name. A name with one row has nothing to tell
 * apart, so it carries no tag.
 *
 * The tag says what the row binds, not what kind of row it is: a crewmate that
 * shares the default memory reads "shared memory" too, because that is the
 * truth of the pick. Only the store id's default-ness is read; no store id is
 * ever rendered.
 */

export type MemorySource = 'own' | 'shared'

type Kind = 'member' | 'template'

export interface MemorySourceRow {
  name: string
  selection_kind?: Kind
  /** A member row's store id. Empty or `default` is the shared store. A template
   *  row carries none: a template pick always binds the shared store. */
  memory_store?: string
}

/** The store a row's pick binds, read from the row alone. */
export function rowMemorySource(row: MemorySourceRow): MemorySource {
  if (row.selection_kind !== 'member') return 'shared'
  const store = row.memory_store || 'default'
  return store === 'default' ? 'shared' : 'own'
}

/** Whether `rows` lists a crewmate AND a template under `name`. */
export function hasSameNamePair(name: string, rows: readonly MemorySourceRow[]): boolean {
  let member = false
  let template = false
  for (const r of rows) {
    if (r.name !== name) continue
    if (r.selection_kind === 'member') member = true
    else if (r.selection_kind === 'template') template = true
  }
  return member && template
}

/**
 * The tag for one picker row: its memory source when `rows` also lists the
 * other kind under its name, otherwise null. A row with no kind (a name-only
 * roster) never gets one.
 */
export function rowMemoryTag(row: MemorySourceRow, rows: readonly MemorySourceRow[]): MemorySource | null {
  if (!row.selection_kind) return null
  return hasSameNamePair(row.name, rows) ? rowMemorySource(row) : null
}

/**
 * Whether a chat is a CREWMATE's, for the picker's "default for new sessions"
 * row and the kind it writes. A slot stamped `member` is one. A slot with no
 * kind (saved before kinds existed, or created through the API without one)
 * is one when the name-folded roster lists its agent as a crewmate -- the row
 * must not offer to make a crewmate the default custom agent, and a kind-less
 * write of that name would land on whichever default the server resolves.
 */
export function isCrewmateChat(
  slotKind: string | undefined,
  agentName: string,
  agents: readonly { name: string; selection_kind?: string }[],
): boolean {
  if (slotKind === 'member') return true
  if (slotKind === 'template') return false
  return agents.some(a => a.name === agentName && a.selection_kind === 'member')
}

/**
 * The tag for a SESSION, for the composer pill and the session-list label: which
 * of a same-name pair the session runs. Resolves the row the way the picker
 * lights it:
 *
 * - an agent-less session runs the default TEMPLATE (#18342);
 * - otherwise `isCrewmateChat` decides, the same rule the picker's footer uses:
 *   a recorded kind wins, and a kind-less slot runs what the backend's
 *   name-first resolution answers for the bare name (the crewmate when one is
 *   listed, else the template).
 *
 * Null when the name has no same-name pair in `rows`, or the resolved row is not
 * listed (the label then claims nothing it cannot read).
 */
export function sessionMemoryTag(
  agent: string | undefined,
  kind: string | undefined,
  defaultAgent: string,
  rows: readonly MemorySourceRow[],
): MemorySource | null {
  const name = agent || defaultAgent
  if (!name || !hasSameNamePair(name, rows)) return null
  const resolved: Kind = agent && isCrewmateChat(kind, name, rows) ? 'member' : 'template'
  const row = rows.find(r => r.name === name && r.selection_kind === resolved)
  return row ? rowMemorySource(row) : null
}
