/**
 * The change cards of one chat slot.
 *
 * The gateway is the authority: this caches `GET /api/cards/pending?slot=` in
 * React Query and the socket folds every owner `card_update` frame into the
 * same cache (`applyCardUpdate`). A reconnect re-reads it, since frames are
 * one-shot.
 */
import { useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { cardsApi, cardsQueryKey, mergeCard, type Card } from '../api/cards'

const LIVE: ReadonlySet<string> = new Set(['pending', 'applying'])

export function useChangeCards(slot: string | null) {
  const queryClient = useQueryClient()
  const key = cardsQueryKey(slot)
  const { data: cards = [], error, isFetched } = useQuery({
    queryKey: key,
    queryFn: () => cardsApi.pending(slot as string),
    enabled: !!slot,
    refetchOnWindowFocus: true,
    // A card that expires server-side sends no frame to a tab that was asleep.
    refetchInterval: q => (q.state.data ?? []).some(c => LIVE.has(c.status)) ? 60_000 : false,
    retry: false,
    staleTime: 5_000,
  })

  /** Fold a card the gateway just returned into the cache. */
  const store = useCallback((c: Card | null) => {
    if (c) queryClient.setQueryData<Card[]>(cardsQueryKey(c.slot_key), prev => mergeCard(prev, c))
  }, [queryClient])

  /** Re-read the slot's cards: the result of an apply or undo is the gateway's. */
  const refresh = useCallback(() => queryClient.invalidateQueries({ queryKey: cardsQueryKey(slot) }), [queryClient, slot])

  const remove = useCallback((id: string) => {
    queryClient.setQueryData<Card[]>(cardsQueryKey(slot), prev => (prev ?? []).filter(c => c.id !== id))
  }, [queryClient, slot])

  return { cards, error, isFetched, store, refresh, remove }
}

export { CHANGE_CARDS_QUERY_PREFIX } from '../api/cards'
