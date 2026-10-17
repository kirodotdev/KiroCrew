import type { Skill } from '../../types'

/** How the Skills list orders its rows. `default` keeps the server's order. */
export type SkillSort = 'default' | 'used' | 'recent'

/** The sort select's options, in the order it lists them. */
export const SKILL_SORTS: SkillSort[] = ['default', 'used', 'recent']

const uses = (s: Skill) => s.deliveries ?? 0
const lastUsed = (s: Skill) => s.last_used_at ?? 0

/** Order one group of Skills-list rows by usage.
 *
 *  `Array.prototype.sort` is stable, so ties (every never-used skill, for one)
 *  keep the server's order instead of reshuffling on each refetch. */
export function sortSkills(skills: Skill[], mode: SkillSort): Skill[] {
  if (mode === 'default') return skills
  const byUses = (a: Skill, b: Skill) => uses(b) - uses(a)
  const byRecent = (a: Skill, b: Skill) => lastUsed(b) - lastUsed(a)
  return [...skills].sort(mode === 'used'
    ? (a, b) => byUses(a, b) || byRecent(a, b)
    : (a, b) => byRecent(a, b) || byUses(a, b))
}
