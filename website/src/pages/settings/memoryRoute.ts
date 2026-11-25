/** Settings > Memory's address, and the query it reads: `store` (a bot's store;
 *  absent is Global memory), `focus` (`lessons` or `failures`) and `lesson` (the
 *  saved lesson a chat's "Review lessons" link opens). */
export const MEMORY_PATH = '/settings/memory'
export const MEMORY_PARAMS = ['store', 'focus', 'lesson'] as const

export function memoryRoute(query: { store?: string; focus?: string; lesson?: string } = {}): string {
  const params = new URLSearchParams()
  for (const name of MEMORY_PARAMS) if (query[name]) params.set(name, query[name]!)
  const rest = params.toString()
  return rest ? `${MEMORY_PATH}?${rest}` : MEMORY_PATH
}

/** Where an old `/settings/overview?view=memory…` link now lands: the same query
 *  on Settings > Memory, without `view`. Bare `/settings` opened Overview on a
 *  wide screen, so its `?view=memory` counts too, as does the legacy
 *  `?tab=overview&view=memory`. Null for any other address. */
export function legacyMemoryRedirect(pathname: string, search: string): string | null {
  const path = pathname.replace(/\/$/, '')
  // The memory graph was Settings > Developer > Memory; it is the Graph tab now.
  if (path === '/settings/developer/memory') return memoryRoute({ focus: 'graph' })
  const params = new URLSearchParams(search)
  const tab = params.get('tab')
  const overview = path === '/settings/overview' || (path === '/settings' && (tab === null || tab === 'overview'))
  if (!overview || params.get('view') !== 'memory') return null
  params.delete('view')
  params.delete('tab')
  const rest = params.toString()
  return rest ? `${MEMORY_PATH}?${rest}` : MEMORY_PATH
}
