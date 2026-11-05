import { memo, useCallback, useEffect, useRef, useState } from 'react'
import {
  ChevronsDown, Dot, Keyboard, MousePointerClick, Move, Navigation, Pause, Play, RotateCcw,
  SkipBack, SkipForward, Type, UserRound,
} from 'lucide-react'

import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import {
  SPEEDS, currentStep, durationOf, nextOffset, offsetOf, prevOffset,
  type Recording, type ReplayEvent, type ReplayOp, type ReplaySpeed,
} from '../../hooks/useBrowserLive.replay'
import { eventLabel } from './BrowserLiveReplayStage'
import { FoldedSteps, foldLoopRuns, useOpenRuns } from './BrowserLiveLoop'

/**
 * The replay's controls: the player clock (`useReplayPlayer`), the scrubber with
 * one marker per action, the transport buttons and the step list.
 *
 * Keyboard. The scrubber is a native range input (arrow keys move it by a
 * second); the markers are pointer shortcuts out of the tab order, because the
 * step list below carries the same jumps as real buttons. The view that hosts
 * this binds Space and the arrow keys (`replayKeyAction`).
 */

/** One player tick. The clock advances by a fixed step per tick, times the speed. */
export const TICK_MS = 50

export interface ReplayPlayer {
  t: number
  duration: number
  playing: boolean
  speed: ReplaySpeed
  play: () => void
  pause: () => void
  toggle: () => void
  seek: (t: number) => void
  setSpeed: (s: ReplaySpeed) => void
  next: () => void
  prev: () => void
}

/**
 * The play head for a recording. `restartKey` changing seeks to the start and
 * plays (a fresh Replay click); `startAt: 'end'` parks the head at the end (a
 * live turn the person opened to look back over).
 */
export function useReplayPlayer(rec: Recording | null, restartKey: string, startAt: 'start' | 'end'): ReplayPlayer {
  const duration = rec ? durationOf(rec) : 0
  const [t, setT] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<ReplaySpeed>(1)
  const durationRef = useRef(duration)
  durationRef.current = duration
  const hasRec = !!rec

  // Only a new `restartKey` restarts; `startAt` is read at that moment, so a mode
  // flip that keeps the key (a scrub out of live) keeps the head where it was put.
  const startAtRef = useRef(startAt)
  startAtRef.current = startAt
  useEffect(() => {
    if (!hasRec) return
    if (startAtRef.current === 'end') { setT(durationRef.current); setPlaying(false) } else { setT(0); setPlaying(true) }
  }, [restartKey, hasRec])

  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      setT(prev => {
        const next = prev + TICK_MS * speed
        if (next >= durationRef.current) { setPlaying(false); return durationRef.current }
        return next
      })
    }, TICK_MS)
    return () => clearInterval(id)
  }, [playing, speed])

  const seek = useCallback((to: number) => setT(Math.min(durationRef.current, Math.max(0, to))), [])
  const play = useCallback(() => {
    // Play at the end starts over, the way a finished video does.
    setT(prev => (prev >= durationRef.current ? 0 : prev))
    setPlaying(true)
  }, [])
  const pause = useCallback(() => setPlaying(false), [])
  const toggle = useCallback(() => { if (playing) pause(); else play() }, [playing, play, pause])
  const next = useCallback(() => { if (rec) setT(prev => nextOffset(rec, prev)) }, [rec])
  const prev = useCallback(() => { if (rec) setT(p => prevOffset(rec, p)) }, [rec])
  return { t: Math.min(t, duration), duration, playing, speed, play, pause, toggle, seek, setSpeed, next, prev }
}

/** What a key does to the replay, or null for a key it leaves alone. */
export function replayKeyAction(key: string): 'toggle' | 'next' | 'prev' | 'start' | 'end' | null {
  switch (key) {
    case ' ': case 'Spacebar': case 'k': return 'toggle'
    case 'ArrowRight': return 'next'
    case 'ArrowLeft': return 'prev'
    case 'Home': return 'start'
    case 'End': return 'end'
    default: return null
  }
}

/** `m:ss` for a play-head offset. Digits through the locale seam. */
export function clockLabel(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${fmtNumber(Math.floor(s / 60))}:${fmtNumber(s % 60, { minimumIntegerDigits: 2 })}`
}

const OP_ICON: Record<ReplayOp, typeof Dot> = {
  click: MousePointerClick, type: Type, key: Keyboard, drag: Move, scroll: ChevronsDown, navigate: Navigation, person: UserRound, other: Dot,
}
/** Marker colours: tokens only, one per op family. */
const OP_TONE: Record<ReplayOp, string> = {
  click: 'var(--accent)', type: 'var(--aim)', key: 'var(--muted-strong)', drag: 'var(--danger)',
  scroll: 'var(--warn)', navigate: 'var(--info)', person: 'var(--ok)', other: 'var(--border-strong)',
}

export function OpIcon({ op, size = 12 }: { op: ReplayOp; size?: number }) {
  const Icon = OP_ICON[op]
  return (
    <span className="grid place-items-center shrink-0 rounded-[5px] size-[18px]" style={{ background: OP_TONE[op] }} aria-hidden="true">
      <Icon size={size} className="text-bg" />
    </span>
  )
}

/** A marker's width plus a hair: markers closer than this, in px, share one. */
export const MARKER_GAP_PX = 20

/**
 * Markers that would cover each other, grouped: each group is the indices of
 * consecutive actions whose centres sit within `gap` px of the group's first.
 * A group draws as ONE marker at its first action's exact time (with a count),
 * so a dense run (a fast loop's moves) stays readable and every marker still
 * seeks to a real action's moment.
 */
export function clusterMarkers(xs: readonly number[], gap = MARKER_GAP_PX): number[][] {
  const out: number[][] = []
  for (let i = 0; i < xs.length; i++) {
    const last = out[out.length - 1]
    if (last && xs[i] - xs[last[0]] < gap) last.push(i)
    else out.push([i])
  }
  return out
}

function speedLabel(s: ReplaySpeed): string {
  return i18nT('pages.chat.browserLiveStage.speed_x', { speed: fmtNumber(s) })
}

export const ReplayTimeline = memo(function ReplayTimeline({ rec, player, live, onLive, onSeekStep }: {
  rec: Recording
  player: ReplayPlayer
  /** The turn is still running: offer Live. */
  live: boolean
  /** True while the view shows the live picture (the head sits at "now"). */
  onLive?: () => void
  /** A marker was clicked: the host leaves live mode before seeking. */
  onSeekStep: (offset: number) => void
}) {
  // Re-render on a language switch: memo() would otherwise keep the old copy.
  useLanguageGeneration()
  const { duration } = player
  const pct = (ms: number) => (duration > 0 ? (ms / duration) * 100 : 0)
  const trackRef = useRef<HTMLDivElement>(null)
  const [trackW, setTrackW] = useState(0)
  useEffect(() => {
    const el = trackRef.current
    if (!el) return
    const measure = () => setTrackW(el.clientWidth)
    measure()
    if (typeof ResizeObserver !== 'function') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const groups = clusterMarkers(rec.events.map((_, i) => (pct(offsetOf(rec, i)) / 100) * Math.max(trackW, 1)), trackW > 0 ? MARKER_GAP_PX : 0)
  const step = currentStep(rec, player.t)
  const ended = !player.playing && duration > 0 && player.t >= duration
  const playLabel = player.playing
    ? i18nT('pages.chat.browserLiveStage.pause')
    : ended ? i18nT('pages.chat.browserLiveStage.replay_from_start') : i18nT('pages.chat.browserLiveStage.play')
  return (
    <div className="px-3 pt-1.5 pb-2 border-t border-border" data-testid="browser-replay-timeline">
      <div ref={trackRef} className="relative h-8">
        {groups.map(group => {
          const i = group[0]
          const e = rec.events[i]
          const more = group.length - 1
          const on = group.includes(step)
          return (
            <button
              key={e.id}
              type="button"
              tabIndex={-1}
              className={`absolute top-0 -translate-x-1/2 grid place-items-center rounded-[5px] cursor-pointer border-0 p-0 ${on ? 'ring-2 ring-text-strong' : ''}`}
              style={{ left: `${pct(offsetOf(rec, i))}%` }}
              onClick={() => onSeekStep(offsetOf(rec, i))}
              title={`${clockLabel(offsetOf(rec, i))} · ${eventLabel(e)}`}
              aria-label={more > 0
                ? i18nT('pages.chat.browserLiveStage.jump_to_steps', { n: i + 1, label: eventLabel(e), count: more })
                : i18nT('pages.chat.browserLiveStage.jump_to_step', { n: i + 1, label: eventLabel(e) })}
              data-testid="browser-replay-marker"
              data-op={e.op}
              data-count={group.length}
            >
              <OpIcon op={e.op} size={11} />
              {more > 0 && (
                <span aria-hidden="true" className="absolute -top-1.5 -right-2 rounded-full bg-text-strong px-1 text-[9px] leading-[13px] text-bg tabular-nums">
                  {fmtNumber(group.length)}
                </span>
              )}
            </button>
          )
        })}
        <input
          type="range"
          min={0}
          max={Math.max(1, Math.round(duration))}
          step={1000}
          value={Math.round(player.t)}
          onChange={e => onSeekStep(Number(e.target.value))}
          aria-label={i18nT('pages.chat.browserLiveStage.timeline')}
          aria-valuetext={i18nT('pages.chat.browserLiveStage.time_of', { at: clockLabel(player.t), total: clockLabel(duration) })}
          className="absolute left-0 right-0 bottom-0 w-full accent-[var(--accent)] cursor-pointer"
          data-testid="browser-replay-scrubber"
        />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <Btn className="!px-1.5" onClick={player.prev} aria-label={i18nT('pages.chat.browserLiveStage.previous_step')} title={i18nT('pages.chat.browserLiveStage.previous_step')} data-testid="browser-replay-prev">
          <SkipBack size={14} aria-hidden="true" />
        </Btn>
        <Btn className="!px-1.5" onClick={player.toggle} aria-label={playLabel} title={playLabel} aria-pressed={player.playing} data-testid="browser-replay-play">
          {player.playing ? <Pause size={14} aria-hidden="true" /> : ended ? <RotateCcw size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
        </Btn>
        <Btn className="!px-1.5" onClick={player.next} aria-label={i18nT('pages.chat.browserLiveStage.next_step')} title={i18nT('pages.chat.browserLiveStage.next_step')} data-testid="browser-replay-next">
          <SkipForward size={14} aria-hidden="true" />
        </Btn>
        <span className="ml-1 text-[12px] text-muted tabular-nums" data-testid="browser-replay-clock">
          {i18nT('pages.chat.browserLiveStage.time_of', { at: clockLabel(player.t), total: clockLabel(duration) })}
        </span>
        <span className="flex-1" />
        <div role="group" aria-label={i18nT('pages.chat.browserLiveStage.speed')} className="flex items-center gap-1">
          {SPEEDS.map(s => (
            <Btn
              key={s}
              aria-pressed={player.speed === s}
              onClick={() => player.setSpeed(s)}
              className={`!px-2 ${player.speed === s ? '!border-accent text-accent' : ''}`}
              data-testid={`browser-replay-speed-${s}`}
            >
              {speedLabel(s)}
            </Btn>
          ))}
        </div>
        {live && onLive && (
          <Btn primary onClick={onLive} data-testid="browser-replay-go-live">
            {i18nT('pages.chat.browserLiveStage.go_live')}
          </Btn>
        )}
      </div>
    </div>
  )
})

export const ReplayStepList = memo(function ReplayStepList({ rec, current, onSeekStep, className }: {
  rec: Recording
  current: number
  onSeekStep: (offset: number) => void
  className?: string
}) {
  useLanguageGeneration()
  const listRef = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>('[aria-current="step"]')
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [current])
  const [openRuns, toggleRun] = useOpenRuns()
  if (rec.events.length === 0) {
    return <p className={`px-4 py-3 text-[12px] text-muted ${className ?? ''}`}>{i18nT('pages.chat.browserLiveStage.no_steps')}</p>
  }
  return (
    <ol ref={listRef} className={`overflow-y-auto px-2 py-1 ${className ?? ''}`} aria-label={i18nT('pages.chat.browserLiveStage.steps')} data-testid="browser-replay-steps">
      {foldLoopRuns(rec.events, e => e.loop, openRuns, current).map(entry => entry.kind === 'folded' ? (
        <li key={`f${entry.run}`} className="px-2 py-1"><FoldedSteps count={entry.count} onOpen={() => toggleRun(entry.run)} /></li>
      ) : (
        <StepRow key={entry.item.id} rec={rec} e={entry.item} i={entry.index} current={current} onSeekStep={onSeekStep} />
      ))}
    </ol>
  )
})

function StepRow({ rec, e, i, current, onSeekStep }: {
  rec: Recording
  e: ReplayEvent
  i: number
  current: number
  onSeekStep: (offset: number) => void
}) {
  return (
    // The highlight sits on the row: the button reset clears a button's own background.
    <li className={`rounded-md ${i === current ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}>
      <button
        type="button"
        onClick={() => onSeekStep(offsetOf(rec, i))}
        aria-current={i === current ? 'step' : undefined}
        className={`w-full flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] cursor-pointer border-0 bg-transparent ${
          i === current ? 'text-text-strong font-medium' : 'text-text'
        }`}
        data-testid="browser-replay-step"
      >
        <OpIcon op={e.op} />
        <span className="min-w-0 flex-1 truncate">{eventLabel(e)}</span>
        {e.op === 'person' && <span className="shrink-0 rounded-full border border-border px-1.5 text-[10px] leading-4 text-muted" data-testid="browser-replay-person">{i18nT('pages.chat.quickAsk.replay_badge')}</span>}
        {e.actor === 'user' && e.op !== 'person' && <span className="shrink-0 text-[11px] text-muted">{i18nT('pages.chat.browserLiveStage.by_you')}</span>}
        {e.status === 'error' && <span className="shrink-0 text-[11px] text-danger">{i18nT('pages.chat.browserLiveStage.failed')}</span>}
        <span className="shrink-0 text-[12px] text-muted tabular-nums">{clockLabel(offsetOf(rec, i))}</span>
      </button>
    </li>
  )
}
