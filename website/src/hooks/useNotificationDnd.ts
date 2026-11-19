/**
 * The gateway's timed Do Not Disturb (`GET`/`PUT /api/notifications/dnd`).
 *
 * The gateway owns the end time and checks it on every delivery, so it ends on
 * its own there. The timer below only re-renders the reader when it runs out,
 * so the header badge and the Settings select read Off again without a refetch.
 * Another tab's change arrives as the `notification_dnd` frame
 * (`useWebSocket`), which writes the same query; a reconnect re-reads it
 * (`refreshServerStateAfterReconnect`). The socket also observes this query
 * without dashboard chrome. `turnCompletion.ts` reads the same cache to quiet
 * the turn-done chime and background-finished toast.
 */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export const NOTIFICATION_DND_KEY = ['notification-dnd'] as const

export const notificationDndQueryOptions = {
  queryKey: NOTIFICATION_DND_KEY,
  queryFn: () => api.notificationDnd(),
  staleTime: 60_000,
}

export interface NotificationDnd {
  /** Epoch seconds the gateway ends Do Not Disturb at; null when it is off. */
  until: number | null
}

/** The running end time in ms, or null when Do Not Disturb is off or has run out. */
export function dndEndMs(data: NotificationDnd | undefined, now: number): number | null {
  const until = data?.until
  if (typeof until !== 'number' || !Number.isFinite(until)) return null
  const ms = until * 1000
  return ms > now ? ms : null
}

export interface NotificationDndView {
  /** The running end time in ms, or null when off or not known. */
  endMs: number | null
  /** The latest read failed, so `endMs` is unconfirmed even if older data is cached. */
  loadFailed: boolean
}

export function useNotificationDnd(): NotificationDndView {
  const { data, isError } = useQuery<NotificationDnd>(notificationDndQueryOptions)
  const [, setExpired] = useState(0)
  const endMs = dndEndMs(data, Date.now())
  useEffect(() => {
    if (endMs === null) return
    const timer = window.setTimeout(() => setExpired(n => n + 1), endMs - Date.now())
    return () => window.clearTimeout(timer)
  }, [endMs])
  return { endMs, loadFailed: isError }
}
