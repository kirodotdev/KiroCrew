/**
 * A session's turns over the board's widest window, from the usage row store the
 * Spend tab's drill-down reads. The query key and the cached shape are the ones
 * `TelemetryPanelConversations` uses for the same route, so the two surfaces
 * share one cache entry instead of racing to fill it with different shapes.
 *
 * Always the 24-hour superset: `spanOn` clamps a span to whatever window the
 * board shows, so switching 6/12/24 costs no request. Off for rows the board is
 * not drawing, and below `md`, where the track is not drawn at all.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useIsMobile } from '../../hooks/useIsMobile'
import { TIMELINE_HOURS, timelineWindow, turnsRange, type TurnRow } from './homeTimeline'

const WIDEST_HOURS = TIMELINE_HOURS[TIMELINE_HOURS.length - 1]

export function useTurnHistory(slot: string, nowMs: number, enabled: boolean) {
  const range = turnsRange(timelineWindow(WIDEST_HOURS, nowMs))
  const mobile = useIsMobile()
  return useQuery({
    queryKey: ['usage-turns', slot, range],
    queryFn: () => api.usageTurns(slot, range) as Promise<{ turns?: TurnRow[] }>,
    enabled: enabled && !mobile,
    staleTime: 60_000,
    // `nowMs` is read to the minute, so the key moves once a minute; keep the
    // entries from earlier minutes from piling up in the cache.
    gcTime: 5 * 60_000,
    retry: false,
  })
}
