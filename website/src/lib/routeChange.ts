/**
 * The router's pathname changes, for code outside React that must notice
 * them. `useFocusRouteRelay` reports each change; the focus relay listens.
 */
const listeners = new Set<() => void>()

export function noteRouteChange(): void {
  for (const listener of listeners) listener()
}

export function subscribeRouteChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
