/** Every `/api/memory/*` query-key PREFIX the Memory tab owns.
 *
 *  The page's global refresh invalidates the family through these rather than
 *  naming each store, because a prefix match also reaches the stores a user
 *  looked at earlier in the session and whose cached rows would otherwise
 *  survive the refresh. */
export const MEMORY_QUERY_PREFIXES: readonly string[][] = [
  ['memory-stores'],
  ['memory-doc'],
  ['memory-retired'],
  ['memory-backups'],
  ['memory-carve'],
  ['member-memory'],
  ['memory-records'],
]
