import type { Skill } from '../../types'

/** How the Skills list orders its rows. `default` keeps the server's order. */
export type SkillSort = 'default' | 'used' | 'recent' | 'created'

/** The sort select's options, in the order it lists them. */
export const SKILL_SORTS: SkillSort[] = ['default', 'used', 'recent', 'created']

const uses = (s: Skill) => s.deliveries ?? 0
const lastUsed = (s: Skill) => s.last_used_at ?? 0

/** Order rows newest first by a RECORDED creation time.
 *
 *  A row whose time is missing or unreadable is undated, not "oldest": it
 *  goes after every dated row, and undated rows keep their incoming order
 *  (`Array.prototype.sort` is stable). */
export function sortByCreated<T>(rows: T[], createdAt: (row: T) => number | null | undefined): T[] {
  const at = (row: T) => {
    const t = createdAt(row)
    return typeof t === 'number' && Number.isFinite(t) ? t : null
  }
  return [...rows].sort((a, b) => {
    const ta = at(a)
    const tb = at(b)
    if (ta === null || tb === null) return ta === tb ? 0 : ta === null ? 1 : -1
    return tb - ta
  })
}

/** A pending candidate's `.meta.json` `created_at` (ISO 8601) as epoch ms. */
export const pendingCreatedAt = (p: { created_at?: string }) => (p.created_at ? Date.parse(p.created_at) : null)

/** Order one group of Skills-list rows by usage or creation time.
 *
 *  `Array.prototype.sort` is stable, so ties (every never-used skill, for one)
 *  keep the server's order instead of reshuffling on each refetch. */
export function sortSkills(skills: Skill[], mode: SkillSort): Skill[] {
  if (mode === 'default') return skills
  if (mode === 'created') return sortByCreated(skills, s => s.created_at)
  const byUses = (a: Skill, b: Skill) => uses(b) - uses(a)
  const byRecent = (a: Skill, b: Skill) => lastUsed(b) - lastUsed(a)
  return [...skills].sort(mode === 'used'
    ? (a, b) => byUses(a, b) || byRecent(a, b)
    : (a, b) => byRecent(a, b) || byUses(a, b))
}
