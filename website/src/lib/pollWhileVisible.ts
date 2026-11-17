/**
 * Repeat `tick` every `ms` while the page is on screen.
 *
 * A plain `setInterval` keeps reading the gateway from a tab nobody is looking
 * at: the Task Runner page asked for its runs every 3 s and a Worlds window
 * sent three reads every few seconds for as long as the tab stayed open. Here a
 * hidden page stops ticking. When it is shown again `tick` runs once at once,
 * so what the reader sees is current, and the cadence starts over from there.
 *
 * The first tick is the caller's: this only repeats. Returns the stop function,
 * which an effect can return as its cleanup.
 */
export function pollWhileVisible(tick: () => void, ms: number): () => void {
  let timer: ReturnType<typeof setInterval> | null = null
  const hidden = () => document.visibilityState === 'hidden'
  const stop = () => {
    if (timer === null) return
    clearInterval(timer)
    timer = null
  }
  const sync = () => {
    if (hidden()) {
      stop()
      return
    }
    if (timer !== null) return
    tick()
    timer = setInterval(tick, ms)
  }
  if (!hidden()) timer = setInterval(tick, ms)
  document.addEventListener('visibilitychange', sync)
  return () => {
    stop()
    document.removeEventListener('visibilitychange', sync)
  }
}
