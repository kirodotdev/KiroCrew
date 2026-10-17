/** Which crew *Send a copy to* lists first.
 *
 *  Two values feed it: the folder's `default_destination_crew` and the global
 *  `instances.default_destination`. A value only counts when it names a crew that
 *  is registered right now; a crew removed since marks nothing, so a stale folder
 *  value falls through to the global one instead of hiding it. Nothing here
 *  sends anything: the result only orders and marks the menu. */

/** The default destination's instance id, or '' when no registered crew is named. */
export function resolveDefaultDestination(
  registeredIds: readonly string[],
  globalDefault: string | undefined,
  folderOverride: string | undefined,
): string {
  const known = new Set(registeredIds)
  if (folderOverride && known.has(folderOverride)) return folderOverride
  if (globalDefault && known.has(globalDefault)) return globalDefault
  return ''
}

/** `items` with the default crew moved to the front; the rest keep their order. */
export function withDefaultFirst<T extends { id: string }>(items: readonly T[], defaultId: string): T[] {
  if (!defaultId) return [...items]
  const first = items.filter(i => i.id === defaultId)
  return first.length === 0 ? [...items] : [...first, ...items.filter(i => i.id !== defaultId)]
}
