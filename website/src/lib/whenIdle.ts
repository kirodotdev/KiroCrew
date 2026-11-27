/**
 * Run `fn` at an idle moment, at most `timeoutMs` away; returns the cancel.
 *
 * `requestIdleCallback` where it exists; Safari has none, so there it is a
 * plain `timeoutMs` timer.
 */
export function whenIdle(fn: () => void, timeoutMs: number): () => void {
  const w = window as Window & { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number; cancelIdleCallback?: (h: number) => void }
  if (w.requestIdleCallback && w.cancelIdleCallback) {
    const h = w.requestIdleCallback(fn, { timeout: timeoutMs })
    const cancel = w.cancelIdleCallback
    return () => cancel(h)
  }
  const t = window.setTimeout(fn, timeoutMs)
  return () => window.clearTimeout(t)
}
