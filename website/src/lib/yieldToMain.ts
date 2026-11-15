/**
 * Give the browser a turn: resolves in a later task, so input and a paint that
 * are waiting can run before the caller's next piece of work.
 *
 * `scheduler.yield()` where the browser has it: the continuation keeps its place
 * ahead of other queued tasks. Elsewhere a zero-delay timer.
 *
 * shortcut: a hidden tab throttles the timer to about once a second, so the
 * caller's next piece of work waits that long there. Move the fallback to a
 * `MessageChannel` when a caller must not wait while hidden.
 */
export function yieldToMain(): Promise<void> {
  const scheduler = (globalThis as { scheduler?: { yield?: () => Promise<void> } }).scheduler
  if (typeof scheduler?.yield === 'function') return scheduler.yield()
  return new Promise(resolve => { setTimeout(resolve, 0) })
}
