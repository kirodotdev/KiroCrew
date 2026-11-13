import { useSyncExternalStore } from 'react'

/**
 * One shared 1 Hz wall clock for live elapsed and countdown labels.
 *
 * Each running-turn label used to own a `setInterval` that set state in its
 * host component, so a whole progress bar, tool row or subagent card
 * re-rendered every second to move one number. Hosts now hand a selector to
 * `useSecondClock`: React compares the selected value after each tick and
 * re-renders only the component whose selected value changed. A leaf that
 * selects whole elapsed seconds repaints once a second; a host that selects a
 * boolean ("past the threshold yet?") repaints once, when it flips.
 *
 * The interval runs only while something is subscribed and the document is
 * visible. A hidden tab shows no label, so the clock stops there, and the
 * first visible frame reads the wall clock again so labels catch up at once.
 */

const TICK_MS = 1000

const listeners = new Set<() => void>()
let now = Date.now()
let timer: ReturnType<typeof setInterval> | null = null
let visibilityBound = false

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

function emit(): void {
  now = Date.now()
  for (const l of [...listeners]) l()
}

function stopTimer(): void {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

function syncTimer(): void {
  if (listeners.size === 0 || isHidden()) {
    stopTimer()
    return
  }
  if (timer === null) timer = setInterval(emit, TICK_MS)
}

function onVisibilityChange(): void {
  if (!isHidden() && listeners.size > 0) emit()
  syncTimer()
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) now = Date.now()
  listeners.add(listener)
  if (!visibilityBound && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
    visibilityBound = true
  }
  syncTimer()
  return () => {
    listeners.delete(listener)
    syncTimer()
    if (listeners.size === 0 && visibilityBound) {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      visibilityBound = false
    }
  }
}

const noopSubscribe = () => () => {}

/** The clock's current reading. With no subscriber the stored reading is
 *  refreshed once it is a tick old, so a first render is not stale while two
 *  reads inside one render still agree (React requires a stable snapshot). */
function read(): number {
  if (listeners.size === 0 || isHidden()) {
    const wall = Date.now()
    if (Math.abs(wall - now) >= TICK_MS) now = wall
  }
  return now
}

/**
 * Select a value from the shared 1 Hz clock. `select` must return a primitive
 * (or a stable reference): the component re-renders only when it changes.
 * With `enabled` false nothing subscribes and the value is read once per
 * render, so a host can keep the hook call unconditional.
 */
export function useSecondClock<T>(select: (nowMs: number) => T, enabled = true): T {
  const getSnapshot = () => select(read())
  return useSyncExternalStore(enabled ? subscribe : noopSubscribe, getSnapshot, getSnapshot)
}

/** Test-only: drop every subscriber and the timer. */
export function __resetSecondClockForTests(): void {
  listeners.clear()
  stopTimer()
  if (visibilityBound && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', onVisibilityChange)
  }
  visibilityBound = false
  now = Date.now()
}
