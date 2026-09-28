import { api, type CrewTeam } from './client'

/**
 * The Crewmates page's team list (`GET /api/teams`).
 *
 * One module so the page, its dialog and any later consumer spell the key
 * exactly once. The list is small and changes only through this page's own
 * create / edit / delete mutations, which invalidate it; the finite staleTime
 * is the floor for a return to the page, matching the roster's.
 */
export const TEAMS_QUERY_KEY = ['crew-teams'] as const

const TEAMS_STALE_MS = 30_000

export const teamsQuery = {
  queryKey: TEAMS_QUERY_KEY,
  queryFn: async (): Promise<CrewTeam[]> => (await api.teams.list()).teams,
  staleTime: TEAMS_STALE_MS,
}

/**
 * The cached list after a create / edit answered with `saved`.
 *
 * The store keeps a crewmate on ONE team: a write that adds it to `saved`
 * detached it from whichever team held it before, in the same document
 * write. The cache has to say the same thing, so `saved`'s members are
 * dropped from every OTHER cached team before `saved` is inserted or
 * replaced -- otherwise a move shows the crewmate on both teams until a
 * refetch, and when that refetch fails the roster keeps showing the old
 * team. No cache (`undefined`) stays no cache: there is no list on screen
 * for a stale entry to hide in, and the refetch's failure is said on the
 * roster as usual.
 */
export function withSavedTeam(prev: CrewTeam[] | undefined, saved: CrewTeam): CrewTeam[] | undefined {
  if (prev === undefined) return prev
  const moved = new Set(saved.members)
  const others = prev
    .filter((tm) => tm.id !== saved.id)
    .map((tm) => {
      const members = tm.members.filter((m) => !moved.has(m))
      return members.length === tm.members.length ? tm : { ...tm, members }
    })
  const at = prev.findIndex((tm) => tm.id === saved.id)
  if (at === -1) return [...others, saved]
  others.splice(at, 0, saved)
  return others
}
