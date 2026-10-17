/**
 * The cold greeting shown when a confirmed crewmate thread opens.
 *
 * The board read remains only to prove no goal is in flight. Active work now
 * gets a return-triggered durable task brief from the gateway, never a static
 * card merely because this component mounted.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type MemberRecap } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import { crewBoardQueryKey } from '../../api/crewBoard'
import { retryPolicy } from '../../api/queryClient'
import { partitionBoardRows } from '../crewBoardRows'

export type MateGreeting = { kind: 'cold'; slot: string; recap: MemberRecap }

/** Which crewmate a cold welcome is for, and when its thread was last active. */
export interface ColdOpen {
  slug: string
  member: string
  lastActiveTs: number
}

/** Idle time after which an open is a cold start. */
export const COLD_AFTER_MS = 6 * 60 * 60_000

export function isColdOpen(lastActiveTs: number, now: number, welcomedTs: string | null): boolean {
  if (welcomedTs === String(lastActiveTs)) return false
  return lastActiveTs <= 0 || now - lastActiveTs * 1000 >= COLD_AFTER_MS
}

const WELCOMED_PREFIX = 'kc-mate-welcomed-'

function readWelcomed(slot: string): string | null {
  try {
    return localStorage.getItem(WELCOMED_PREFIX + slot)
  } catch {
    return null
  }
}

function markWelcomed(slot: string, lastActiveTs: number): void {
  try {
    localStorage.setItem(WELCOMED_PREFIX + slot, String(lastActiveTs))
  } catch {
    // Blocked storage may repeat the unchanged cold welcome on the next open.
  }
}

export interface MateGreetingFailure {
  slot: string
  kind: 'board' | 'cold'
  error: unknown
}

const retryUnlessNoLedger = (failureCount: number, error: unknown): boolean =>
  !isNotFoundError(error) && retryPolicy(failureCount, error)

/**
 * The cold greeting for the confirmed crewmate thread.
 *
 * Fires once per open: when `slotKey` becomes a confirmed thread, not on every
 * render. A slot going blank and back is a new open. Nothing is read while the
 * crewmate is mid-turn; a turn starting takes a shown greeting down.
 *
 * The board query uses `staleTime: 0`, so a cached board is refreshed and never
 * decides whether this open is cold. The recap query is enabled only after that
 * fresh board says no goal is in flight. A recap with nothing draws no card.
 */
export function useMateGreeting(slotKey: string, idle: boolean, cold: ColdOpen | null): {
  greeting: MateGreeting | null
  failure: MateGreetingFailure | null
  dismiss: () => void
} {
  const [greeting, setGreeting] = useState<MateGreeting | null>(null)
  const [failure, setFailure] = useState<MateGreetingFailure | null>(null)
  const [armed, setArmed] = useState('')
  const handled = useRef('')
  const coldRef = useRef(cold)
  coldRef.current = cold
  const [coldArmed, setColdArmed] = useState<(ColdOpen & { slot: string }) | null>(null)

  useEffect(() => {
    if (!slotKey) {
      // Leaving ends this open. A later reopen starts from fresh board state,
      // never from a card retained by this hook.
      handled.current = ''
      setGreeting(null)
      return
    }
    if (handled.current === slotKey) return
    handled.current = slotKey
    setGreeting(null)
    setFailure(null)
    setColdArmed(null)
    setArmed(idle ? slotKey : '')
  }, [slotKey, idle])

  useEffect(() => {
    if (idle) return
    setGreeting(null)
    setArmed('')
    setColdArmed(null)
  }, [idle])

  const armedSlot = armed === slotKey ? armed : ''
  const read = useQuery({
    queryKey: crewBoardQueryKey(armedSlot),
    queryFn: () => api.crewBoard(armedSlot),
    enabled: !!armedSlot,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryUnlessNoLedger,
  })
  const { data, error, fetchStatus } = read

  useEffect(() => {
    if (!armedSlot || fetchStatus !== 'idle') return
    const welcome = () => {
      const candidate = coldRef.current
      if (!candidate || !isColdOpen(candidate.lastActiveTs, Date.now(), readWelcomed(armedSlot))) return
      setColdArmed({ ...candidate, slot: armedSlot })
    }
    if (error) {
      setArmed('')
      if (isNotFoundError(error)) welcome()
      else setFailure({ slot: armedSlot, kind: 'board', error })
      return
    }
    if (!data) return
    setArmed('')
    const { ruling, working } = partitionBoardRows(data.items)
    if (ruling.length === 0 && working.length === 0) welcome()
  }, [armedSlot, data, error, fetchStatus])

  const recapFor = coldArmed && coldArmed.slot === slotKey && idle ? coldArmed : null
  const recapRead = useQuery({
    queryKey: ['memberRecap', recapFor?.slug ?? '', recapFor?.member ?? ''],
    queryFn: () => api.memberRecap(recapFor!.slug, recapFor!.member),
    enabled: !!recapFor,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryPolicy,
  })

  useEffect(() => {
    if (!recapFor || recapRead.fetchStatus !== 'idle') return
    setColdArmed(null)
    if (recapRead.error) {
      setFailure({ slot: recapFor.slot, kind: 'cold', error: recapRead.error })
      return
    }
    const recap = recapRead.data
    if (!recap || (recap.paused.length === 0 && recap.recent.length === 0)) return
    markWelcomed(recapFor.slot, recapFor.lastActiveTs)
    setGreeting({ kind: 'cold', slot: recapFor.slot, recap })
  }, [recapFor, recapRead.data, recapRead.error, recapRead.fetchStatus])

  const dismiss = useCallback(() => {
    setGreeting(null)
    setFailure(null)
  }, [])
  return {
    greeting: greeting && greeting.slot === slotKey ? greeting : null,
    failure: failure && failure.slot === slotKey ? failure : null,
    dismiss,
  }
}
