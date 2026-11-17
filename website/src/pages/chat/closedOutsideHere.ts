import type { MutableRefObject } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import type { ChatSlot } from '../../types'

/**
 * Whether the chat a page last showed was closed outside this page: no longer
 * listed, confirmed gone by the server's `removed` patch (`durablyRemoved`),
 * and not a close this page started. A close this page holds in
 * `closingSlots` latches `closedHereRef`, so a later frame cannot relabel it
 * once the hold is released. Closing a chat this one was dispatched from
 * (`lineage`: its conductor, crew root or parent) closes it on the server too,
 * so that counts as this page's close as well.
 */
export function closedOutsideHere(
  last: string | null,
  listed: ReadonlySet<string>,
  closingSlots: Readonly<Record<string, unknown>> | undefined,
  closedHereRef: MutableRefObject<string | null>,
  durablyRemoved: Readonly<Record<string, true>> | undefined,
  lineage: readonly string[] = [],
): boolean {
  if (!last || listed.has(last)) return false
  if (closingSlots?.[last] || lineage.some(k => closingSlots?.[k])) closedHereRef.current = last
  return closedHereRef.current !== last && !!durablyRemoved?.[last]
}

/** The chats `slot` was dispatched from, nearest first, without blanks or repeats. */
export function slotLineage(slot: Pick<ChatSlot, 'key' | 'created_by' | 'crew_root' | 'parent'> | undefined): string[] {
  if (!slot) return []
  const keys = [slot.parent?.slot, slot.created_by, slot.crew_root].filter((k): k is string => !!k && k !== slot.key)
  return [...new Set(keys)]
}

/**
 * Whether this Crew is one other people use, as the cached Crew mode says
 * (`network`: a sharing path is set up or another person was seen). Read from
 * the cache only; with no mode known it is false, so a personal Crew keeps
 * the usual landing when the server closes a chat (an idle sweep, a Bot).
 */
export function sharedCrewInUse(queryClient: QueryClient): boolean {
  return queryClient.getQueryData<CrewModeResponse>(sharedCrewKeys.mode)?.mode === 'network'
}
