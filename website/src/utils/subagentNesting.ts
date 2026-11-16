/** A Crew run's own harness-native children draw one level under that run's
 *  Subagents card. The gateway names such a card `native:<run id>:<child>`
 *  (`subagent_manager/native_child_cards.py`); a run id is hex. A card nests only
 *  when the run it names is a card of the same map, so a chat's own Workflow
 *  agent (`native:<task>:<agent>`) stays flat unless a managed run has that id. */

/** The id shape of a Crew run (`_mint_agent_id`), and of nothing a harness names. */
const RUN_ID = /^[0-9a-f]{4,64}$/

const NATIVE = 'native:'

/** The run card *id* draws under, or undefined for a card that draws at the top. */
export function runParentOf(id: string, subs: Readonly<Record<string, unknown>>): string | undefined {
  if (!id.startsWith(NATIVE)) return undefined
  const rest = id.slice(NATIVE.length)
  const sep = rest.indexOf(':')
  if (sep <= 0 || sep === rest.length - 1) return undefined
  const run = rest.slice(0, sep)
  return RUN_ID.test(run) && Object.prototype.hasOwnProperty.call(subs, run) ? run : undefined
}

/** *ranked* reordered so each run's native cards follow it, in their own rank
 *  order, one level deep; every other card keeps its rank. `parentOf` names the run
 *  each nested card draws under. */
export function nestUnderRuns(
  ranked: readonly string[],
  subs: Readonly<Record<string, unknown>>,
): { ids: string[]; parentOf: ReadonlyMap<string, string> } {
  const parentOf = new Map<string, string>()
  const children = new Map<string, string[]>()
  for (const id of ranked) {
    const parent = runParentOf(id, subs)
    if (!parent) continue
    parentOf.set(id, parent)
    const list = children.get(parent)
    if (list) list.push(id)
    else children.set(parent, [id])
  }
  const ids: string[] = []
  for (const id of ranked) {
    if (parentOf.has(id)) continue
    ids.push(id)
    for (const child of children.get(id) ?? []) ids.push(child)
  }
  return { ids, parentOf }
}
