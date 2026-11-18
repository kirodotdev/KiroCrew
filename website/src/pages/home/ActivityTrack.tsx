/**
 * One row's activity over the board's window: a bar per turn, a brighter bar for
 * the run in progress, and a dot where the row asked for a decision or failed.
 * Theme tokens only; positions are percentages of the track, which is the one
 * place an inline style is the right tool.
 */
import { useTranslation } from 'react-i18next'
import type { TrackSpan } from './homeTimeline'

export interface TrackMarker { atPct: number; kind: 'ask' | 'error' }

export default function ActivityTrack({ spans, live, markers, thin = false, label }: {
  /** Finished work, oldest first. */
  spans: readonly TrackSpan[]
  /** The run in progress, drawn brighter and pulsing; absent when idle. */
  live?: TrackSpan | null
  markers?: readonly TrackMarker[]
  /** A child row (subagent, workflow) draws a slimmer track. */
  thin?: boolean
  /** What the track belongs to, for assistive technology. */
  label: string
}) {
  const { t } = useTranslation()
  return (
    <div role="img" aria-label={t('pages.homePage.track_label', { name: label })} className={`relative w-full ${thin ? 'h-1.5' : 'h-2.5'} rounded-full bg-bg-elevated overflow-visible`} data-testid="home-track">
      {spans.map((s, i) => (
        <span key={i} className="absolute inset-y-0 rounded-full bg-accent/50" style={{ left: `${s.leftPct}%`, width: `${s.widthPct}%` }} data-testid="home-track-span" />
      ))}
      {live && (
        <span className="absolute inset-y-0 rounded-full bg-accent animate-pulse" style={{ left: `${live.leftPct}%`, width: `${live.widthPct}%` }} data-testid="home-track-live" />
      )}
      {markers?.map((m, i) => (
        <span
          key={`m${i}`}
          className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 rounded-full border-2 border-bg ${thin ? 'w-2.5 h-2.5' : 'w-3.5 h-3.5'} ${m.kind === 'ask' ? 'bg-warn' : 'bg-danger'}`}
          style={{ left: `${m.atPct}%` }}
          data-testid={`home-track-${m.kind}`}
        />
      ))}
    </div>
  )
}
