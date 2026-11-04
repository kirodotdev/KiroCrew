import { useState } from 'react'
import { ChevronDown, Zap } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import type { BrowserLiveLoop } from '../../hooks/useBrowserLive'

/**
 * A loop op (`reflex`) as the card and the big view show it: one line of
 * counters while it runs, a thin bar for how much of its budget it has used,
 * and, once it stops, why, in one short sentence. Stopping it early is Take
 * over: the loop checks for the person at every step. Rendered from the
 * `browser_live` event's `loop` field alone, so any loop op that reports the
 * same counters shows the same way.
 *
 * Also the step folding: a loop reports a step per move, so a run of loop steps
 * shows only its last few, behind one "N earlier steps folded" control.
 */

/** Loop-step runs show this many of their newest steps; the rest fold. */
export const LOOP_STEPS_SHOWN = 3

/** The stop tokens with a sentence of their own; any other says only that it stopped. */
const STOP_KEYS = {
  done: 'pages.chat.browserLiveLoop.stop_done',
  until: 'pages.chat.browserLiveLoop.stop_until',
  stuck: 'pages.chat.browserLiveLoop.stop_stuck',
  risk: 'pages.chat.browserLiveLoop.stop_risk',
  budget: 'pages.chat.browserLiveLoop.stop_budget',
  user_in_control: 'pages.chat.browserLiveLoop.stop_user_in_control',
  off_path: 'pages.chat.browserLiveLoop.stop_off_path',
  no_actions_left: 'pages.chat.browserLiveLoop.stop_no_actions_left',
  decision_unavailable: 'pages.chat.browserLiveLoop.stop_decision_unavailable',
  redirect_refused: 'pages.chat.browserLiveLoop.stop_redirect_refused',
} as const

/** The sentence for a loop's stop token. */
export function stopSentence(stop: string): string {
  const key = Object.prototype.hasOwnProperty.call(STOP_KEYS, stop) ? STOP_KEYS[stop as keyof typeof STOP_KEYS] : 'pages.chat.browserLiveLoop.stop_other'
  return i18nT(key)
}

/** How much of its budget the loop has used, 0..1: the larger of steps and time; null when neither is known. */
export function loopProgress(loop: BrowserLiveLoop): number | null {
  const parts: number[] = []
  if (loop.steps !== null && loop.maxSteps) parts.push(loop.steps / loop.maxSteps)
  if (loop.seconds !== null && loop.maxSeconds) parts.push(loop.seconds / loop.maxSeconds)
  if (parts.length === 0) return null
  return Math.min(1, Math.max(0, ...parts))
}

/** "Fast mode: 37 moves · 6.2/s", with the parts the loop reported. */
export function loopLabel(loop: BrowserLiveLoop): string {
  const parts: string[] = []
  if (loop.moves !== null) parts.push(i18nT('pages.chat.browserLiveLoop.moves', { count: loop.moves }))
  if (loop.rate !== null) {
    parts.push(i18nT('pages.chat.browserLiveLoop.rate', { rate: fmtNumber(loop.rate, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) }))
  }
  if (parts.length === 0) return i18nT('pages.chat.browserLiveLoop.fast_mode')
  return i18nT('pages.chat.browserLiveLoop.line', { counts: parts.join(' · ') })
}

export function BrowserLiveLoopLine({ loop, className }: { loop: BrowserLiveLoop; className?: string }) {
  const running = loop.stop === null
  const progress = running ? loopProgress(loop) : null
  return (
    <div className={`flex flex-col gap-1 ${className ?? ''}`} data-testid="browser-live-loop" data-stop={loop.stop ?? ''} role="status">
      <div className="flex items-center gap-1.5 text-[12px] text-text-strong">
        <Zap size={12} aria-hidden="true" className={running ? 'text-accent shrink-0' : 'text-muted shrink-0'} />
        <span className="font-semibold tabular-nums truncate" data-testid="browser-live-loop-line">{loopLabel(loop)}</span>
      </div>
      {progress !== null && (
        <div
          className="h-1 w-full overflow-hidden rounded-full bg-border"
          role="progressbar"
          aria-label={i18nT('pages.chat.browserLiveLoop.progress')}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
          data-testid="browser-live-loop-progress"
        >
          <div className="h-full rounded-full bg-accent transition-[width] duration-200 motion-reduce:transition-none" style={{ width: `${progress * 100}%` }} />
        </div>
      )}
      {!running && loop.stop && (
        <span className="text-[12px] text-muted" data-testid="browser-live-loop-stop">{stopSentence(loop.stop)}</span>
      )}
    </div>
  )
}

export type Folded<T> = { kind: 'item'; item: T; index: number } | { kind: 'folded'; count: number; run: number }

/**
 * Items with each run of loop items cut to its newest `keep`: the earlier ones
 * become one `folded` entry (keyed by the run's first index), unless that run is
 * `open` or holds the `current` index. A run only folds when that hides two or more.
 */
export function foldLoopRuns<T>(
  items: readonly T[],
  isLoop: (item: T) => boolean,
  open: ReadonlySet<number>,
  current = -1,
  keep = LOOP_STEPS_SHOWN,
): Folded<T>[] {
  const out: Folded<T>[] = []
  let i = 0
  while (i < items.length) {
    if (!isLoop(items[i])) {
      out.push({ kind: 'item', item: items[i], index: i })
      i += 1
      continue
    }
    let end = i
    while (end < items.length && isLoop(items[end])) end += 1
    const hidden = end - i - keep
    const fold = hidden >= 2 && !open.has(i) && !(current >= i && current < i + hidden)
    if (fold) out.push({ kind: 'folded', count: hidden, run: i })
    for (let k = fold ? i + hidden : i; k < end; k++) out.push({ kind: 'item', item: items[k], index: k })
    i = end
  }
  return out
}

/** The open/closed state of a list's folded runs. */
export function useOpenRuns(): [ReadonlySet<number>, (run: number) => void] {
  const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set())
  const toggle = (run: number) => setOpen(prev => {
    const next = new Set(prev)
    if (next.has(run)) next.delete(run)
    else next.add(run)
    return next
  })
  return [open, toggle]
}

/** The "N earlier steps folded" control; clicking it shows the run's steps. */
export function FoldedSteps({ count, onOpen, className }: { count: number; onOpen: () => void; className?: string }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={`inline-flex items-center gap-1 cursor-pointer border-0 bg-transparent text-[12px] text-muted hover:text-text ${className ?? ''}`}
      data-testid="browser-live-folded"
      data-count={count}
    >
      <ChevronDown size={12} aria-hidden="true" />
      {i18nT('pages.chat.browserLiveLoop.folded', { count })}
    </button>
  )
}
