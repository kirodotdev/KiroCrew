import { vi } from 'vitest'

/**
 * Replaces `requestAnimationFrame` with a queue the test drains, so a frame runs
 * only when the test says so. Undo with `vi.unstubAllGlobals()`.
 */
export function stubManualFrames() {
  const pending = new Map<number, FrameRequestCallback>()
  let lastId = 0
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { pending.set(++lastId, cb); return lastId })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { pending.delete(id) })
  return {
    pending,
    runFrame() {
      const due = [...pending.values()]
      pending.clear()
      for (const cb of due) cb(0)
    },
  }
}
