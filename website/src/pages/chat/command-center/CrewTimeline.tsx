import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { CrewTimeline as CrewTimelineData, CrewTimelineLane, CrewTimelineSegment } from '../../../types'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtCredits, fmtDuration, fmtElapsed } from '../../../i18n/format'
import { useReducedMotion } from '../../../hooks/useReducedMotion'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'
import InfoTip from '../../../components/InfoTip'
import { parseTime, scaledBox, scaledTicks, timelineScale, timelineWindow, type TimeScale, type TimelineWindow } from '../../../lib/crewTimeline'

/** The query key of one root's timeline. */
export const crewTimelineQueryKey = (root: string) => ['crew-timeline', root] as const

/** Bar and mark labels, keyed by the wire `kind`. */
const KIND_KEY: Record<CrewTimelineSegment['kind'] | 'handoff' | 'failed', string> = {
  turn: 'commandCenter.timeline_legend_turn',
  tool: 'commandCenter.timeline_legend_tool',
  waiting: 'commandCenter.timeline_legend_waiting',
  handoff: 'commandCenter.timeline_legend_handoff',
  failed: 'commandCenter.timeline_legend_failed',
}

/** What the transcript does not record, keyed by the server's gap code. A code
 *  this build does not know is left out rather than shown raw. */
const GAP_KEY: Record<string, string> = {
  thinking_vs_output: 'commandCenter.timeline_gap_thinking_vs_output',
  tool_end_time: 'commandCenter.timeline_gap_tool_end_time',
  tool_failure_status: 'commandCenter.timeline_gap_tool_failure_status',
  tokens: 'commandCenter.timeline_gap_tokens',
  closed_worker_rows: 'commandCenter.timeline_gap_closed_worker_rows',
  rows_before_window: 'commandCenter.timeline_gap_rows_before_window',
  served_model_unreported: 'commandCenter.timeline_gap_served_model_unreported',
}

/** Each kind's colour, from the theme's own tokens so light, dark and every
 *  palette follow. A turn carries the crew gradient; a wait is the warning tone. */
const BAR_CLASS: Record<CrewTimelineSegment['kind'], string> = {
  turn: 'crew-grad opacity-80',
  tool: 'bg-info',
  waiting: 'bg-warn',
}

/** Re-read cadence: brisk while the run moves, slow once it has stopped. */
const LIVE_REFETCH_MS = 2000
const DONE_REFETCH_MS = 15000
/** How often a live playhead advances between reads, when motion is allowed. */
const PLAYHEAD_TICK_MS = 250

/**
 * One Crew Mode root's timeline: a lane for the conductor and one per worker,
 * each with its model under its name, and bars for its turns, tool calls and
 * waits on the person, all read off recorded transcript rows. A hand-off shows on
 * the worker's lane from the sender's `session_send` to the worker's first row; an
 * error or a refused tool is a mark. The header is the run's real elapsed time
 * and summed credits. While the run is live a playhead marks now and advances
 * between reads; under reduced motion there is no playhead, and while the person
 * types in the composer the view holds still. Once the run is done it is static. Clicking a lane opens that session in the main pane.
 *
 * Time is drawn true up to a minute per stretch and shorter past it
 * (`timelineScale`), so a long wait cannot squash the short bars; every bar is
 * at least `MIN_BAR_PX` wide. A hand-off is an arrow from the sender's lane at
 * its `session_send` down to the worker's lane at its first row.
 *
 * What the transcript does not record is listed behind the (i) beside the
 * legend, never drawn.
 */
export default function CrewTimeline({ root, viewing, active, onOpen }: {
  root: string
  viewing: string | null
  active: boolean
  onOpen: (key: string) => void
}) {
  const { t } = useTranslation()
  const reduced = useReducedMotion()
  const query = useQuery({
    queryKey: crewTimelineQueryKey(root),
    queryFn: () => api.crewTimeline(root),
    enabled: active && !!root,
    refetchInterval: q => (!active ? false : q.state.data?.live ? LIVE_REFETCH_MS : DONE_REFETCH_MS),
  })
  // While the person types in the composer the view holds still: the last read
  // stays on screen and the clock stops, until a moment after the last keystroke.
  const composing = useComposing()
  const data = useHeld(query.data, composing)
  const ticking = !!data?.live && !reduced && !composing
  // Derived once per read, not per playhead tick: the tick re-renders only the
  // playhead and the elapsed label, which own the ticking clock.
  const window = useMemo(() => (data ? timelineWindow(data) : null), [data])
  const scale = useMemo(() => (data && window ? timelineScale(data, window) : null), [data, window])
  if (query.isError) return <div className="p-3"><ErrorNotice message={t('commandCenter.timeline_error')} /></div>
  if (!data) return null
  const lanes = data.lanes
  return <div className="flex flex-col gap-3 p-3 min-w-0" data-testid="crew-timeline" data-live={data.live ? 'true' : 'false'}>
    <header className="flex items-center gap-2 text-[12px] text-muted" data-testid="crew-timeline-header">
      <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${data.live ? 'crew-grad' : 'bg-bg-hover text-text'}`}>
        {data.live ? t('commandCenter.timeline_live') : t('commandCenter.timeline_done')}
      </span>
      <Elapsed data={data} start={window?.start ?? null} receivedAt={query.dataUpdatedAt} ticking={ticking} />
      <span aria-hidden="true">·</span>
      <span data-testid="crew-timeline-credits">{t('commandCenter.timeline_credits', { value: fmtCredits(data.credits) })}</span>
    </header>
    {!window || !scale || lanes.every(lane => lane.segments.length === 0 && lane.marks.length === 0)
      ? <p className="text-sm text-muted">{t('commandCenter.timeline_empty')}</p>
      : <Lanes data={data} window={window} scale={scale} viewing={viewing} onOpen={onOpen} receivedAt={query.dataUpdatedAt} ticking={ticking} reduced={reduced} />}
    <Legend gaps={data.gaps} compressed={!!scale?.compressed} />
  </div>
}

/** The header's elapsed time: the server's figure once done, the ticking clock
 *  while live. Its own component, so the tick re-renders only this label. */
function Elapsed({ data, start, receivedAt, ticking }: { data: CrewTimelineData; start: number | null; receivedAt: number; ticking: boolean }) {
  const { t } = useTranslation()
  const now = useServerNow(data, receivedAt, ticking)
  const elapsed = data.live && start !== null ? Math.max(0, now - start) : data.elapsed_ms
  return <span data-testid="crew-timeline-elapsed">{t('commandCenter.timeline_elapsed', { value: fmtElapsed(elapsed, { wholeSeconds: data.live }) })}</span>
}

/** The axis, the lanes, the hand-off arrows over them and the playhead. The
 *  arrows join two lanes, so they are drawn once over the whole list from each
 *  track's measured box. */
function Lanes({ data, window, scale, viewing, onOpen, receivedAt, ticking, reduced }: {
  data: CrewTimelineData
  window: TimelineWindow
  scale: TimeScale
  viewing: string | null
  onOpen: (key: string) => void
  receivedAt: number
  ticking: boolean
  reduced: boolean
}) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  // Keyed on WHICH lanes there are, not how many: a worker closing as another
  // opens between two reads keeps the count and changes the boxes.
  const tracks = useTrackBoxes(boxRef, data.lanes.map(lane => lane.key).join('|'))
  return <div ref={boxRef} className="relative flex flex-col gap-1" role="list">
    <Axis window={window} scale={scale} />
    {data.lanes.map(lane => (
      <Lane key={lane.key} lane={lane} scale={scale} current={lane.key === viewing} onOpen={onOpen} />
    ))}
    <HandoffArrows links={data.handoffs} scale={scale} tracks={tracks} />
    {/* Under reduced motion there is no playhead at all: a line that jumps at
        every read is motion too. The header still says Live and the elapsed time. */}
    {data.live && !reduced && <Playhead data={data} scale={scale} receivedAt={receivedAt} ticking={ticking} />}
  </div>
}

interface TrackBox { left: number; top: number; width: number; height: number }

/** Each lane track's box inside *box*, by lane key, re-measured on resize. */
function useTrackBoxes(box: React.RefObject<HTMLDivElement | null>, laneKeys: string): Record<string, TrackBox> {
  const [boxes, setBoxes] = useState<Record<string, TrackBox>>({})
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const measure = () => {
      const origin = el.getBoundingClientRect()
      const next: Record<string, TrackBox> = {}
      el.querySelectorAll<HTMLElement>('[data-track-of]').forEach(track => {
        const r = track.getBoundingClientRect()
        next[track.dataset.trackOf || ''] = { left: r.left - origin.left, top: r.top - origin.top, width: r.width, height: r.height }
      })
      setBoxes(next)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [box, laneKeys])
  return boxes
}

/** One arrow per hand-off: from the sender's lane at its send down to the
 *  worker's lane at its first row. Drawn in the crew purple; decoration only,
 *  the lanes carry the names. */
function HandoffArrows({ links, scale, tracks }: { links: CrewTimelineData['handoffs']; scale: TimeScale; tracks: Record<string, TrackBox> }) {
  const { t } = useTranslation()
  const drawn = links.flatMap(link => {
    const from = tracks[link.from]
    const to = tracks[link.to]
    const start = parseTime(link.start)
    const end = parseTime(link.end)
    if (!from || !to || start === null || end === null) return []
    const x1 = from.left + (from.width * scale.toPct(start)) / 100
    const x2 = to.left + (to.width * scale.toPct(end)) / 100
    const down = to.top >= from.top
    const y1 = down ? from.top + from.height : from.top
    const y2 = down ? to.top + 3 : to.top + to.height - 3
    return [{ link, x1, x2, y1, y2 }]
  })
  if (drawn.length === 0) return null
  return <svg aria-hidden="true" className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
    <defs>
      <marker id="crew-handoff-head" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="var(--aim)" />
      </marker>
    </defs>
    {drawn.map(({ link, x1, x2, y1, y2 }) => (
      <path
        key={`${link.from}-${link.to}-${link.start}`}
        data-testid="crew-timeline-handoff"
        data-from={link.from}
        data-to={link.to}
        d={`M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`}
        fill="none"
        stroke="var(--aim)"
        strokeWidth="1.5"
        markerEnd="url(#crew-handoff-head)"
      >
        <title>{t('commandCenter.timeline_legend_handoff')}</title>
      </path>
    ))}
  </svg>
}

/** How long after the last keystroke the view stays held. */
const COMPOSING_HOLD_MS = 1500

/** Whether the person is typing in the chat composer right now: an `input` in the
 *  composer (`.input-area`) within the last `COMPOSING_HOLD_MS`. Changes state
 *  only on the edges, so a keystroke does not re-render the timeline. */
function useComposing(): boolean {
  const [composing, setComposing] = useState(false)
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const onInput = (event: Event) => {
      const target = event.target as Element | null
      if (!target?.closest?.('.input-area')) return
      setComposing(true)
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => setComposing(false), COMPOSING_HOLD_MS)
    }
    document.addEventListener('input', onInput, true)
    return () => {
      document.removeEventListener('input', onInput, true)
      if (timer) clearTimeout(timer)
    }
  }, [])
  return composing
}

/** *value*, except while *hold* is true: then the value it had when the hold
 *  began, so a read that lands mid-typing is shown once the typing stops. */
function useHeld<T>(value: T, hold: boolean): T {
  const held = useRef(value)
  if (!hold) held.current = value
  return hold ? held.current : value
}

/** The server's clock, advanced locally between reads only while `ticking`. */
function useServerNow(data: CrewTimelineData | undefined, receivedAt: number, ticking: boolean): number {
  const base = parseTime(data?.now) ?? 0
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    setOffset(0)
    if (!ticking) return
    const id = setInterval(() => setOffset(Date.now() - receivedAt), PLAYHEAD_TICK_MS)
    return () => clearInterval(id)
  }, [base, receivedAt, ticking])
  return base + offset
}

const LABEL_COL = 'w-40 shrink-0'
/** Where a track starts and ends inside a lane row: `px-1` + `w-40` + `gap-2`. */
const TRACK_INSET = '10.75rem'
const TRACK_END = '0.25rem'
/** The narrowest a bar is drawn, so a short turn or tool call stays readable. */
const MIN_BAR_PX = 6

function Axis({ window, scale }: { window: TimelineWindow; scale: TimeScale }) {
  const ticks = scaledTicks(window, scale)
  return <div className="flex items-end gap-2 px-1 text-[10px] text-muted" aria-hidden="true">
    <span className={LABEL_COL} />
    <span className="relative flex-1 h-4 border-b border-border">
      {ticks.map(({ at, pct }, i) => (
        // The first label hangs right of its tick and one near the end left of
        // it, so neither leaves the track; the rest are centred.
        <span key={at} className={`absolute bottom-0.5 whitespace-nowrap tabular-nums ${i === 0 ? '' : pct > 90 ? '-translate-x-full' : '-translate-x-1/2'}`} style={{ left: `${pct}%` }}>
          {fmtTick(at)}
        </span>
      ))}
    </span>
  </div>
}

/** An axis label: whole minutes past a minute ("10m", not "10m 0s"). */
function fmtTick(ms: number): string {
  return ms >= 60_000 && ms % 60_000 === 0 ? fmtDuration([[ms / 60_000, 'minute']]) : fmtElapsed(ms, { wholeSeconds: true })
}

/** The name under a lane: the model the backend served or the session picked,
 *  else the gateway default it runs on. */
function laneModel(t: ReturnType<typeof useTranslation>['t'], lane: CrewTimelineLane): string {
  if (lane.model_source !== 'default') return lane.model || t('commandCenter.timeline_model_default')
  return lane.model
    ? t('commandCenter.timeline_model_default_named', { name: lane.model })
    : t('commandCenter.timeline_model_default')
}

/** One lane. Memoised: its props (the lane object from the read, the memoised
 *  scale, the stable open callback) change only when a read does. */
const Lane = memo(function Lane({ lane, scale, current, onOpen }: {
  lane: CrewTimelineLane
  scale: TimeScale
  current: boolean
  onOpen: (key: string) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const { t } = useTranslation()
  const closed = lane.status === 'closed'
  const title = lane.title || t('commandCenter.worker_untitled')
  const model = laneModel(t, lane)
  // Two lines for the name before it is cut; the whole name and model are the tooltip.
  const head = <span className={`${LABEL_COL} min-w-0 text-left`} title={`${title} · ${model}`}>
    {lane.role === 'conductor' && <span className="block text-[9px] font-bold uppercase tracking-wider crew-grad-text">{t('commandCenter.timeline_conductor')}</span>}
    <span className="block line-clamp-2 break-words text-[12px] font-medium leading-tight text-text" data-testid="crew-timeline-title">{title}</span>
    <span className="block truncate text-[10px] text-muted" data-testid="crew-timeline-model">{model}</span>
  </span>
  const track = <span className="relative flex-1 h-7 rounded bg-bg-hover/50" aria-hidden="true" data-track-of={lane.key}>
    {lane.segments.map((bar, i) => {
      const box = scaledBox(scale, bar.start, bar.end)
      if (!box) return null
      return <span
        key={`${bar.kind}-${bar.start}-${i}`}
        data-testid="crew-timeline-bar"
        data-kind={bar.kind}
        data-end-bound={bar.end_bound}
        data-width-pct={box.width.toFixed(2)}
        title={barTitle(t, bar)}
        className={`absolute rounded-sm ${BAR_CLASS[bar.kind]} ${bar.kind === 'turn' ? 'top-1 bottom-1' : 'top-2 bottom-2'} ${bar.end_bound === 'next_row' ? 'opacity-70' : ''}`}
        style={{ left: `${box.left}%`, width: `max(${box.width}%, ${MIN_BAR_PX}px)` }}
      />
    })}
    {lane.marks.map((mark, i) => {
      const at = parseTime(mark.ts)
      if (at === null) return null
      return <span
        key={`${mark.kind}-${mark.ts}-${i}`}
        data-testid="crew-timeline-mark"
        data-kind={mark.kind}
        title={mark.label}
        className={`absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45 ${mark.kind === 'failed' ? 'bg-danger' : 'border-2 border-danger bg-bg'}`}
        style={{ left: `${scale.toPct(at)}%` }}
      />
    })}
  </span>
  const cls = `flex items-center gap-2 w-full rounded-md px-1 py-1 ${current ? 'crew-card' : ''}`
  return <div role="listitem" data-testid="crew-timeline-lane" data-lane={lane.key} data-role={lane.role}>
    {closed
      ? <div className={`${cls} opacity-60`} title={t('commandCenter.timeline_closed_lane')}>{head}{track}</div>
      : <button type="button" className={`${cls} cursor-pointer hover:bg-bg-hover min-h-11`} aria-current={current ? 'true' : undefined} aria-label={t('commandCenter.timeline_open_lane', { title })} onClick={() => onOpen(lane.key)}>{head}{track}</button>}
  </div>
})

function barTitle(t: ReturnType<typeof useTranslation>['t'], bar: CrewTimelineSegment): string {
  const from = parseTime(bar.start)
  const to = parseTime(bar.end)
  const duration = from !== null && to !== null ? fmtElapsed(Math.max(0, to - from)) : ''
  return [t(KIND_KEY[bar.kind]), bar.label, duration].filter(Boolean).join(' · ')
}

/** Now, one line down every lane's track. A track starts after the lane's
 *  padding, label column and gap (`TRACK_INSET`) and ends before its padding, so
 *  the line is placed on the same scale as the bars. */
function Playhead({ data, scale, receivedAt, ticking }: { data: CrewTimelineData; scale: TimeScale; receivedAt: number; ticking: boolean }) {
  const now = useServerNow(data, receivedAt, ticking)
  return <span aria-hidden="true" className="pointer-events-none absolute top-0 bottom-0" style={{ left: TRACK_INSET, right: TRACK_END }}>
    <span
      data-testid="crew-timeline-playhead"
      className="absolute top-0 bottom-0 w-0.5 -translate-x-1/2 bg-aim"
      style={{ left: `${scale.toPct(now)}%` }}
    />
  </span>
}

function Legend({ gaps, compressed }: { gaps: string[]; compressed: boolean }) {
  const { t } = useTranslation()
  const known = gaps.filter(gap => GAP_KEY[gap]).map(gap => t(GAP_KEY[gap]))
  if (compressed) known.unshift(t('commandCenter.timeline_scale_compressed'))
  const swatch: [keyof typeof KIND_KEY, string][] = [
    ['turn', 'crew-grad'],
    ['tool', 'bg-info'],
    ['waiting', 'bg-warn'],
    ['handoff', 'border-t-2 border-aim h-0 mt-1'],
    ['failed', 'bg-danger rotate-45 w-2 h-2'],
  ]
  return <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted" data-testid="crew-timeline-legend">
    <ul className="contents">
      {swatch.map(([kind, cls]) => (
        <li key={kind} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className={`inline-block w-3 h-2 rounded-sm ${cls}`} />
          {t(KIND_KEY[kind])}
        </li>
      ))}
    </ul>
    {/* What is not recorded (and how the scale bends) sits behind one (i), so the
        panel shows lanes and legend only. */}
    {known.length > 0 && <span data-testid="crew-timeline-gaps" className="inline-flex items-center">
      <InfoTip text={`${t('commandCenter.timeline_gaps_title')}: ${known.join('; ')}.`} />
    </span>}
  </div>
}
