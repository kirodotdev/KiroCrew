/**
 * Command Bar root index.
 *
 * The first page is a LAUNCHER, not a search: it carries only rows that are
 * already in memory — contributed commands, app destinations, quicklinks and
 * system settings. Nothing here queries a backend, which is what makes typing in
 * the root cost nothing regardless of how much history the instance holds.
 *
 * Content searches (sessions, knowledge, artifacts) are deliberately absent. They
 * live behind a `view` row: entering it is the activation event that lets their
 * engine load, so a keystroke in the root can never fan out to a full-corpus
 * scan.
 *
 * Kept pure and dependency-light so the "root issues no requests" property is
 * provable by unit test rather than by reading the component.
 */
import { fuzzyMatch } from '../../utils/fuzzyMatch'
import { compareText } from '../../i18n/format'

import { frecencyScore, type UsageMap } from './frecency'

/** What activating a root row does. */
export type RootRowKind =
  /** Enter a scoped view inside the bar; the view loads its engine on entry. */
  | 'view'
  /** Leave the bar and navigate the dashboard to a route. */
  | 'navigate'
  /** Run a callback and close; never navigates. */
  | 'invoke'

/** The groups the root is allowed to show, in display order. */
export const ROOT_GROUPS = ['commands', 'apps', 'settings'] as const

export type RootGroup = (typeof ROOT_GROUPS)[number]

export interface RootRow {
  /** Stable id; also the frecency key, so it must not encode the query. */
  id: string
  title: string
  subtitle?: string
  group: RootGroup
  kind: RootRowKind
  /** `navigate` rows: the dashboard route. */
  route?: string
  /** `view` rows: which scoped view to enter. */
  view?: string
  /** For `invoke` rows: the work to run. A rejection is surfaced, never swallowed. */
  run?: () => Promise<unknown>
  /** Extra strings that should match but are not displayed (aliases, keywords). */
  keywords?: string[]
}

export interface RankedRow extends RootRow {
  /** Fuzzy score of the query against the title, plus the frecency boost. */
  score: number
  /** Matched character positions in `title`, for highlight rendering. */
  indices: number[]
}

/**
 * Weight of one frecency point relative to fuzzy score.
 *
 * Sized so habit outranks a marginally better string match but cannot outrank a
 * clearly better one: an exact-prefix hit on a never-used row still beats a
 * scattered subsequence on a daily one.
 */
const FRECENCY_WEIGHT = 6

/** Cap per group so no single group can push the others off the first page. */
const PER_GROUP_LIMIT = 6

/**
 * Tighter cap for the settings group on an EMPTY query.
 *
 * Settings are a long searchable tail, not what a launcher opens on: the codegen
 * registry contributes hundreds of rows, and at the normal cap six alphabetical
 * toggles fill the first page before the user has typed anything. They still rank
 * normally the moment a query narrows them.
 */
const SETTINGS_IDLE_LIMIT = 2

function bestFieldMatch(query: string, row: RootRow): { score: number; indices: number[] } | null {
  const direct = fuzzyMatch(query, row.title)
  if (direct) return direct
  // Aliases and subtitles match but never highlight: the indices would point
  // into a string the row does not render.
  for (const alt of [row.subtitle ?? '', ...(row.keywords ?? [])]) {
    if (!alt) continue
    const hit = fuzzyMatch(query, alt)
    if (hit) return { score: hit.score * 0.6, indices: [] }
  }
  return null
}

/**
 * Filter and rank the root rows for a query.
 *
 * An empty query keeps every row and orders by frecency alone, so the bar opens
 * on "what you actually use" instead of an alphabetical inventory.
 *
 * Rows come back in GROUP order — commands, apps, quicklinks, settings — with
 * match quality ordering rows inside a group. Group order is a product decision
 * about what a launcher leads with, so it must not be at the mercy of whichever
 * row happens to score highest: ranking alone put six settings toggles above the
 * commands on an empty query, because every score was 0 and the tie broke
 * alphabetically.
 */
export function rankRootRows(
  rows: readonly RootRow[],
  query: string,
  usage: UsageMap,
  now = Date.now(),
): RankedRow[] {
  const q = query.trim()
  const ranked: RankedRow[] = []
  for (const row of rows) {
    const boost = frecencyScore(usage[row.id], now) * FRECENCY_WEIGHT
    if (!q) {
      ranked.push({ ...row, score: boost, indices: [] })
      continue
    }
    const hit = bestFieldMatch(q, row)
    if (!hit) continue
    ranked.push({ ...row, score: hit.score + boost, indices: hit.indices })
  }
  // Titles are display copy, so the tiebreak orders them in the APP's language
  // rather than the browser's.
  ranked.sort((a, b) => b.score - a.score || compareText(a.title, b.title))

  // Group caps are applied AFTER ranking so a row only loses its place to a
  // better row in its own group, never to the order the sources were listed in.
  const perGroup = new Map<RootGroup, number>()
  const capped: RankedRow[] = []
  for (const row of ranked) {
    const limit = !q && row.group === 'settings' ? SETTINGS_IDLE_LIMIT : PER_GROUP_LIMIT
    const seen = perGroup.get(row.group) ?? 0
    if (seen >= limit) continue
    perGroup.set(row.group, seen + 1)
    capped.push(row)
  }
  // Stable within-group order is already established above, so a stable sort by
  // group alone yields group blocks with their ranking intact.
  return capped.sort((a, b) => groupOrder(a.group) - groupOrder(b.group))
}

/** Group order for section headers, stable regardless of ranking. */
function groupOrder(group: RootGroup): number {
  return ROOT_GROUPS.indexOf(group)
}
