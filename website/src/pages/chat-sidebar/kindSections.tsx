/** "Group by kind": the filter menu's view option that splits each session list into
 *  sections -- Crew Mode, Normal, From Mates. View only: it reorders and labels the
 *  rows a list already draws, and never changes a session's folder, tags or order. */
import type { Slot } from './types'
import { i18nT } from '../../i18n/t'
import { isCrewmateChat } from '../../lib/agentMemorySource'

/** A session's kind, in section order. */
export type SessionKind = 'crew' | 'normal' | 'mate'

export const SESSION_KIND_ORDER: readonly SessionKind[] = ['crew', 'normal', 'mate']

const KIND_LABEL_KEY: Record<SessionKind, string> = {
  crew: 'pages.chatSidebar.kind_section_crew',
  normal: 'pages.chatSidebar.kind_section_normal',
  mate: 'pages.chatSidebar.kind_section_mate',
}

/** The roster shape `isCrewmateChat` reads. */
type Roster = readonly { name: string; selection_kind?: string }[]

/** Whether a row is a crewmate's own session: the sidebar's one crewmate test
 *  (`isCrewmateChat`, which the Broadcast panel uses too), or a row on the
 *  Members surface. A peer row is read by its own stamp only: a local roster
 *  name says nothing about another gateway's agent. */
function isCrewmateRow(s: Pick<Slot, 'agent_kind' | 'surface' | 'agent' | 'peer_id'>, agents: Roster): boolean {
  if (s.surface === 'member') return true
  return s.peer_id ? s.agent_kind === 'member' : isCrewmateChat(s.agent_kind, s.agent ?? '', agents)
}

export function makeKindOf(liveSlots: readonly Slot[], agents: Roster = []): (s: Slot) => SessionKind {
  const byKey = new Map<string, Slot>()
  for (const s of liveSlots) if (s.key && !s.peer_id) byKey.set(s.key, s)
  const cache = new Map<Slot, SessionKind>()
  return (s: Slot) => {
    const hit = cache.get(s)
    if (hit) return hit
    let kind: SessionKind = 'normal'
    if (s.crew_mode_root || s.crew_root) kind = 'crew'
    else if (isCrewmateRow(s, agents)) kind = 'mate'
    else if (!s.peer_id) {
      // Bounded by the live population; `seen` stops a cycle.
      const seen = new Set<string>([s.key])
      let cited = s.parent?.key ?? null
      while (cited && !seen.has(cited)) {
        seen.add(cited)
        const creator = byKey.get(cited)
        if (!creator) break
        if (isCrewmateRow(creator, agents)) { kind = 'mate'; break }
        cited = creator.parent?.key ?? null
      }
    }
    cache.set(s, kind)
    return kind
  }
}

/** The grouping a lane applies, or null when the option is off (or a search is
 *  ranking the list, whose order is the answer). */
export interface KindSections {
  kindOf: (s: Slot) => SessionKind
}

/**
 * Reorder one list into its sections: pinned rows stay first in their own order (the
 * pinned band is not split), then the unpinned rows by kind, each kind keeping the
 * list's order. Returns the list itself when it holds fewer than two kinds, so a list
 * with nothing to split is drawn exactly as before.
 */
export function sectionRows<T extends Slot>(rows: T[], isPinned: (s: Slot) => boolean, sections: KindSections | null): T[] {
  if (!sections) return rows
  const { kindOf } = sections
  const pinned: T[] = []
  const buckets: Record<SessionKind, T[]> = { crew: [], normal: [], mate: [] }
  for (const s of rows) {
    if (isPinned(s)) pinned.push(s)
    else buckets[kindOf(s)].push(s)
  }
  const used = SESSION_KIND_ORDER.filter(k => buckets[k].length > 0)
  if (used.length < 2) return rows
  return [...pinned, ...SESSION_KIND_ORDER.flatMap(k => buckets[k])]
}

/**
 * For each row of a drawn list, the section a header opens before it, or null. A
 * header opens at the first unpinned row of each kind, and only in a list with two or
 * more kinds among its unpinned rows. All nulls when `sections` is null.
 */
export function sectionHeaders(rows: readonly Slot[], isPinned: (s: Slot) => boolean, sections: KindSections | null): (SessionKind | null)[] {
  const out: (SessionKind | null)[] = rows.map(() => null)
  if (!sections) return out
  const { kindOf } = sections
  const kinds = new Set<SessionKind>()
  for (const s of rows) if (!isPinned(s)) kinds.add(kindOf(s))
  if (kinds.size < 2) return out
  rows.forEach((row, i) => {
    if (isPinned(row)) return
    const kind = kindOf(row)
    const prev = i > 0 ? rows[i - 1] : null
    if (prev && !isPinned(prev) && kindOf(prev) === kind) return
    out[i] = kind
  })
  return out
}

/** A row's place in the section order, for the hover hold (pinned rows first, then
 *  each kind), or undefined when the option is off. */
export function sectionRankOf(isPinned: (s: Slot) => boolean, sections: KindSections | null): ((s: Slot) => number) | undefined {
  if (!sections) return undefined
  const { kindOf } = sections
  return s => (isPinned(s) ? 0 : 1 + SESSION_KIND_ORDER.indexOf(kindOf(s)))
}

/** The label a section header reads. */
export function kindSectionLabel(kind: SessionKind): string {
  return i18nT(KIND_LABEL_KEY[kind])
}

/** One section header. A heading, so a screen reader can jump between sections. */
export function KindSectionHeader({ kind }: { kind: SessionKind }) {
  return (
    <div
      role="heading"
      aria-level={3}
      data-kind-section={kind}
      data-testid="kind-section-header"
      className="px-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1"
    >
      {kindSectionLabel(kind)}
    </div>
  )
}
