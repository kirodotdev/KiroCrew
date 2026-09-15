const SKILLS_CACHE_STALE_MS = 5 * 60 * 1000

export function skillsCacheStaleTime(project: string | undefined): number {
  // Without project identity the query key cannot observe a same-slot project
  // switch. Make every reopen revalidate instead of serving another project's
  // still-fresh catalog.
  return project === undefined ? 0 : SKILLS_CACHE_STALE_MS
}
