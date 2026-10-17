import type { ChatMessage } from '../../types'
import type { CrewTeam, MemberRosterRow } from '../../api/client'
import { parseOptions } from '../../app-sdk/protocol/options'
import { isStopEvent } from '../../lib/stopEvent'
import { isSystemNoticeKind } from '../../lib/systemNotice'

/** URL parameter naming the open team (`?team=<id>`), the sibling of `?member=`. */
export const TEAM_PARAM = 'team'

/** Group id of the trailing "No team" group. Not a stored team: it is every
 *  roster row no team lists, and it exists only in the rendered roster. */
export const NO_TEAM_ID = 'no-team'

/** localStorage key holding the ids of the collapsed roster groups. */
export const TEAM_COLLAPSED_KEY = 'mc-members-teams-collapsed'

export interface RosterGroup {
  /** The stored team, or `null` for the trailing "No team" group. */
  team: CrewTeam | null
  id: string
  /** Rows in the ROSTER's order (the user's sort), not the team's stored order:
   *  the team list says who is on the team, the roster says how rows are sorted. */
  members: MemberRosterRow[]
}

/**
 * Group the displayed roster by team, in the teams' stored order, with every
 * row no team lists in a trailing "No team" group.
 *
 * `rows` is the roster AFTER search / filter / sort, so a narrowed roster
 * narrows every group the same way; a team whose rows are all filtered out
 * is dropped rather than shown empty, because an empty group under an active
 * filter would read as a team with nobody on it. A team that is GENUINELY
 * empty -- none of the names it lists is in `allRows`, the whole roster
 * before any filter (a team made with no crewmates, or whose crewmates were
 * all deleted) -- keeps its header, at "0 crewmates": it is still the user's
 * team, and the header is the only way back to its view and its Edit team
 * dialog. With no teams at all the result is one anonymous "No team" group
 * -- callers render that as the flat list the page had before teams existed,
 * with no header.
 *
 * A name listed by a team but absent from the roster (a deleted crewmate the
 * team list still names) contributes nothing: the roster is the source of who
 * exists.
 */
export function groupRosterByTeam(
  teams: readonly CrewTeam[],
  rows: readonly MemberRosterRow[],
  allRows: readonly MemberRosterRow[] = rows,
): RosterGroup[] {
  const known = new Set(allRows.map((r) => r.name))
  const teamOf = new Map<string, CrewTeam>()
  for (const team of teams) {
    for (const name of team.members) {
      // The store keeps a crewmate on one team; a foreign document that lists
      // one twice resolves to the FIRST team, the store's own reading rule.
      if (!teamOf.has(name)) teamOf.set(name, team)
    }
  }
  const byTeam = new Map<string, MemberRosterRow[]>()
  const loose: MemberRosterRow[] = []
  for (const row of rows) {
    const team = teamOf.get(row.name)
    if (!team) {
      loose.push(row)
      continue
    }
    const list = byTeam.get(team.id)
    if (list) list.push(row)
    else byTeam.set(team.id, [row])
  }
  const groups: RosterGroup[] = []
  for (const team of teams) {
    const members = byTeam.get(team.id)
    if (members && members.length > 0) groups.push({ team, id: team.id, members })
    else if (!team.members.some((name) => known.has(name))) groups.push({ team, id: team.id, members: [] })
  }
  // The trailing group exists when a row is loose, or when there are no teams
  // at all (the flat list). Not when every row is merely hidden by a filter:
  // an empty "No team" header there would read as a team with nobody on it.
  if (loose.length > 0 || teams.length === 0) groups.push({ team: null, id: NO_TEAM_ID, members: loose })
  return groups
}

/** The team listing `name`, or `undefined` when it is on none. */
export function teamOfMember(teams: readonly CrewTeam[], name: string): CrewTeam | undefined {
  return teams.find((team) => team.members.includes(name))
}

/** Parse the persisted collapsed-group set. Storage is hand-editable: anything
 *  that is not a JSON array of strings reads as "nothing collapsed". */
export function parseCollapsedTeams(raw: string | null): Set<string> {
  if (!raw) return new Set()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((v): v is string => typeof v === 'string'))
  } catch {
    return new Set()
  }
}

export function serializeCollapsedTeams(collapsed: ReadonlySet<string>): string {
  return JSON.stringify([...collapsed])
}

/** One question a crewmate asked the user that has no later reply in its chat. */
export interface OpenQuestion {
  /** The bubble's prose with option markers stripped. */
  text: string
  /** The choices the crewmate offered, in its order (empty for a bare question). */
  options: string[]
  /** ISO timestamp of the message, '' when the row carries none. */
  ts: string
}

/** Roles that end a turn from either side. Tool rows, notices and streaming
 *  chunks are neither a question nor an answer. */
const CONVERSATIONAL_ROLES = new Set(['user', 'assistant'])

/**
 * The crewmate's unanswered question at the tail of a chat, if any.
 *
 * Reads the NEWEST conversational row: a user row means whatever was asked has
 * been answered (or superseded), an assistant row ending in a question mark or
 * carrying an `[OPTIONS:]` marker is a question still waiting. A trailing Stop
 * press does not count as an answer -- the question stands, the user only
 * halted the turn -- so stop events are skipped when looking for the tail.
 * So is a system notice the backend appends in the assistant role (compaction,
 * session reload): it is a status report, not the crewmate's last word, and
 * reading it as one would hide the question before it -- the same skip every
 * other backward scan for the assistant's last word runs (`lib/systemNotice`).
 */
export function unansweredQuestion(messages: readonly ChatMessage[]): OpenQuestion | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (isStopEvent(m)) continue
    if (isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined))) continue
    if (!CONVERSATIONAL_ROLES.has(m.role)) continue
    if (m.role === 'user') return null
    const content = typeof m.content === 'string' ? m.content : ''
    if (!content.trim()) continue
    const parsed = parseOptions(content)
    const text = parsed.text.trim()
    const asks = parsed.options.length > 0 || /[?？]\s*$/.test(text)
    if (!asks) return null
    return { text, options: parsed.options, ts: m.ts ?? '' }
  }
  return null
}

/** How a crewmate reads in the team view's status strip. */
export type TeamMemberState = 'running' | 'waiting' | 'idle' | 'paused'

export function teamMemberState(input: {
  running: boolean
  waiting: boolean
  stopped: boolean
}): TeamMemberState {
  if (input.running) return 'running'
  if (input.waiting) return 'waiting'
  if (input.stopped) return 'paused'
  return 'idle'
}

/** Seconds in the "This week" window. */
export const WEEK_SECONDS = 7 * 86_400

/** The most lines the "This week" block lists. */
export const WEEK_MAX_ITEMS = 20

/**
 * Order of the "Needs you" inbox: newest question first, and a row whose
 * question carries no parseable timestamp (the plain waiting card, a bubble
 * with no `ts`) sinks to the end. `Date.parse('')` is `NaN`, and a `NaN`
 * difference read as "equal" left such a row wherever the input order put it
 * -- above newer questions -- behind a comparator no sort can trust (a `NaN`
 * answer is not transitive). Each side is resolved once to a finite time or
 * `-Infinity`, so every pair answers with a real number and two
 * timestamp-less rows compare equal.
 */
export function newestQuestionFirst(a: { question: OpenQuestion | null }, b: { question: OpenQuestion | null }): number {
  const ta = questionTime(a.question)
  const tb = questionTime(b.question)
  if (ta === tb) return 0
  return tb - ta
}

function questionTime(q: OpenQuestion | null): number {
  const t = q?.ts ? Date.parse(q.ts) : NaN
  return Number.isFinite(t) ? t : -Infinity
}
