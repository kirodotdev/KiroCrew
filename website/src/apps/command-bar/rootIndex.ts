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
import type { ReactNode } from 'react'

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
  /**
   * Collect ONE argument inside the bar, then act on it.
   *
   * Distinct from `invoke` because activating it does no work: a row whose whole
   * operation is defined by a value the user has not supplied yet cannot run on
   * Enter, so activation enters an argument state and a second Enter is what acts.
   * That second step is not friction to be optimized away — for a row that writes
   * to somewhere shared, the argument IS the blast radius, and it is worth its own
   * keystroke on a field the user is looking at.
   */
  | 'prompt'

/**
 * The groups the root is allowed to show, in display order.
 *
 * `attention` leads and is usually EMPTY. It holds the sessions that owe the user
 * something — one waiting on an approval, one holding a question — which is the
 * most time-sensitive object this product has and the only kind of row a launcher
 * over a static app catalogue cannot produce. It is built from the store the
 * dashboard already keeps live, so leading with it costs the root no request.
 *
 * `recent` follows it: the handful of sessions the reader was last in. The most
 * common reason this surface is opened at all is "get me back to what I was
 * doing", and that answer is already in the same live store — so it is a row to
 * press, not a search to run. It is capped small on purpose (see the caller): a
 * switch list stops being one the moment it becomes an index, and the full corpus
 * is one `view` row below it.
 *
 * There is deliberately no `folders` group: the sidebar's folders are a CORPUS, and
 * a corpus is reached here the way sessions are — one `view` row under `commands`
 * that the reader enters. Flattening the folder list into this index made the same
 * collection behave unlike every other corpus the surface holds, and cost it two
 * mechanisms (an idle demotion and a group cap) to keep tens of the user's own
 * folders from filling a page they had not typed into.
 */
export const ROOT_GROUPS = ['attention', 'recent', 'commands', 'apps', 'settings'] as const

export type RootGroup = (typeof ROOT_GROUPS)[number]

/**
 * Live state a row can carry in place of a static label.
 *
 * Narrow and typed rather than a free-form node: a row's right-hand column is a
 * layout contract shared by every row type, and the one thing worth spending it on
 * is state that is CHANGING. `pill` marks the state as something the user owes the
 * session (an approval, an answer) rather than something the session is doing.
 */
export interface RowStatus {
  /** CSS custom property name for the state's colour, e.g. `--warn`. */
  colorVar: string
  label: string
  detail?: string
  pulse?: boolean
  pill?: boolean
}

export interface RootRow {
  /** Stable id; also the frecency key, so it must not encode the query. */
  id: string
  title: string
  subtitle?: string
  group: RootGroup
  kind: RootRowKind
  /**
   * The row's OWN icon, when it has one — an app's manifest art, or the glyph
   * that names this particular command.
   *
   * A per-group glyph is the fallback, not the design: filed by group alone every
   * app row renders the same package outline, so a list of seven apps carries no
   * more information in its icon column than a list of one. The column only earns
   * its width when the icon identifies the row.
   */
  icon?: ReactNode
  /**
   * Live state for the row's right-hand column, replacing the static kind label.
   *
   * A row whose state is changing is worth more of that column than a word naming
   * what the row IS: "Approve" on an amber pill answers "which of these needs me"
   * in one glance, where "Command" answers a question nobody asked twice.
   */
  status?: RowStatus
  /** `navigate` rows: the dashboard route. */
  route?: string
  /** `view` rows: which scoped view to enter. */
  view?: string
  /** For `invoke` rows: the work to run. A rejection is surfaced, never swallowed. */
  run?: () => Promise<unknown>
  /** Extra strings that should match but are not displayed (aliases, keywords). */
  keywords?: string[]
  /**
   * Sort this row to the END of its group while the query is EMPTY.
   *
   * The empty-query order is frecency, and an unused row scores zero — so the tie
   * breaks alphabetically, which is a fine rule for rows that are equals and a bad
   * one for rows that are not. A launcher opens with its first row selected, and
   * "Approve all PRs" sorting above "New Session" on the letter A would make a bulk
   * write the default thing Cmd+K offers.
   *
   * Only the IDLE order is affected: a row the user has actually typed toward ranks
   * on its match like any other, which is the whole point of naming a command.
   */
  idleDemote?: boolean
  /**
   * Display name of the app that contributed this row, when one did.
   *
   * Rendered in the meta column beside the row's kind. A contributed row otherwise
   * looks exactly like a builtin -- same badge, same shape -- while its prompt goes to
   * an agent with tools, and the subtitle cannot carry the attribution because the app
   * may write its own. "Which app put this in my launcher" is the first question a
   * reader asks of a row they do not recognise, and this is the only place in the root
   * list that can answer it.
   */
  appLabel?: string
}

/**
 * Which of the row's fields the query actually matched.
 *
 * Carried out of ranking because the row has to be able to SHOW it. A match on a
 * field the row does not render — a keyword, or a subtitle whose offsets were
 * discarded — produces a row with no highlight anywhere, so the list answers
 * "these matched" with rows that visibly did not: typing `theme` returned
 * settings rows whose title and subtitle both lack the word, and nothing on the
 * row said which of its hidden aliases put it there. The renderer uses this to
 * put the evidence on screen instead.
 */
export type MatchField = 'title' | 'subtitle' | 'keyword'

export interface RankedRow extends RootRow {
  /** Fuzzy score of the query against the title, plus the frecency boost. */
  score: number
  /** Matched character positions in `title`, for highlight rendering. */
  indices: number[]
  /** Which field produced the match. `title` for an empty query (nothing matched). */
  matchField: MatchField
  /** Matched positions in `subtitle`, set only when `matchField` is `subtitle`. */
  subtitleIndices?: number[]
  /** The hidden alias that matched, set only when `matchField` is `keyword`. */
  matchedKeyword?: string
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
 * Penalty applied to an `idleDemote` row while the query is empty.
 *
 * Sized to lose to a single real use, not to hide the row for good: one use scores
 * `1 * FRECENCY_WEIGHT`, so a row the user actually reaches for climbs back out
 * immediately. What this fixes is the COLD state, where every score is zero and the
 * alphabet alone decides what a launcher opens on.
 */
const IDLE_DEMOTION = 1

/**
 * Tighter cap for the settings group on an EMPTY query.
 *
 * Settings are a long searchable tail, not what a launcher opens on: the codegen
 * registry contributes hundreds of rows, and at the normal cap six alphabetical
 * toggles fill the first page before the user has typed anything. They still rank
 * normally the moment a query narrows them.
 */
const SETTINGS_IDLE_LIMIT = 2

/**
 * Score of a match on a field the row does not lead with, relative to a title hit.
 *
 * A subtitle or alias hit is weaker evidence of intent than the name the user is
 * looking at, so it ranks below one — but it still has to rank, because the alias
 * is frequently the word the user knows the row by.
 */
const ALT_FIELD_PENALTY = 0.6

interface FieldMatch {
  score: number
  indices: number[]
  field: MatchField
  subtitleIndices?: number[]
  matchedKeyword?: string
}

/**
 * Best match of `query` against the row, and WHICH field produced it.
 *
 * Subtitle offsets are kept rather than dropped, and the matching alias is named,
 * so the caller can render the evidence. The previous form returned
 * `indices: []` for both cases, which is why an alias hit rendered as a row with
 * no highlight at all: correct by score, unexplainable on screen.
 */
function bestFieldMatch(query: string, row: RootRow): FieldMatch | null {
  const direct = fuzzyMatch(query, row.title)
  if (direct) return { score: direct.score, indices: direct.indices, field: 'title' }
  if (row.subtitle) {
    const hit = fuzzyMatch(query, row.subtitle)
    if (hit) {
      return {
        score: hit.score * ALT_FIELD_PENALTY,
        indices: [],
        field: 'subtitle',
        subtitleIndices: hit.indices,
      }
    }
  }
  for (const keyword of row.keywords ?? []) {
    const hit = fuzzyMatch(query, keyword)
    if (hit) {
      return {
        score: hit.score * ALT_FIELD_PENALTY,
        indices: [],
        field: 'keyword',
        matchedKeyword: keyword,
      }
    }
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
  // Source position of each row, captured before ranking. An idle-ordered group
  // (recent) keeps the order its caller supplied, and a stable sort needs a real
  // key to hold that order in place of the alphabet.
  const sourceOrder = new Map<string, number>()
  rows.forEach((row, index) => sourceOrder.set(row.id, index))
  for (const row of rows) {
    const boost = frecencyScore(usage[row.id], now) * FRECENCY_WEIGHT
    if (!q) {
      // A group whose idle order is a FACT (recency) declines the frecency boost, so
      // neither the boost nor the alphabet can reshuffle an order these rows already
      // carry; source order stands in the tiebreak below.
      if (row.group === 'recent') {
        ranked.push({ ...row, score: 0, indices: [], matchField: 'title' })
        continue
      }
      // Demotion is subtracted rather than applied as a sort key so a row the user
      // DOES use can still climb: frecency is a positive boost, so habit eventually
      // outweighs the penalty instead of being permanently capped under it.
      const idle = row.idleDemote ? boost - IDLE_DEMOTION : boost
      ranked.push({ ...row, score: idle, indices: [], matchField: 'title' })
      continue
    }
    const hit = bestFieldMatch(q, row)
    if (!hit) continue
    ranked.push({
      ...row,
      score: hit.score + boost,
      indices: hit.indices,
      matchField: hit.field,
      subtitleIndices: hit.subtitleIndices,
      matchedKeyword: hit.matchedKeyword,
    })
  }
  // Group order sits between score and the tiebreak so the tiebreak only ever
  // compares rows already in the SAME group. That is required, not cosmetic: the two
  // tiebreak keys differ by group — an idle-ordered group orders by source position,
  // every other by title — and mixing them across groups is not a consistent order.
  // Score still leads, so a used row outranks an unused one regardless of group; the
  // final regroup below restores block order for the display.
  ranked.sort((a, b) => {
    if (a.score !== b.score) return b.score - a.score
    const byGroup = groupOrder(a.group) - groupOrder(b.group)
    if (byGroup !== 0) return byGroup
    // Same group. An idle-ordered group keeps the order its caller supplied, but only
    // while the query is empty: a query ranks every row on its match, so the idle
    // order must not decide there.
    if (!q && a.group === 'recent') {
      return (sourceOrder.get(a.id) ?? 0) - (sourceOrder.get(b.id) ?? 0)
    }
    return compareText(a.title, b.title)
  })

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
