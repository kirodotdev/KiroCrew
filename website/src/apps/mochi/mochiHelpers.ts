// Small helpers layered over the raw api client.
import {
  updateWatchlist,
  type UpdateWatchlistResult,
  type WatchItem,
  type WatchKind,
} from './api'

export type { WatchItem }

export function addToWatchlist(params: {
  label: string
  kind: WatchKind
  target: string
}): Promise<UpdateWatchlistResult> {
  return updateWatchlist({ add: [params] })
}
