import { vi } from 'vitest'

/**
 * Install a ResizeObserver that delivers once for each target it starts to
 * watch, as a browser's does at the next rendering step. Here there is no
 * rendering step, so it delivers synchronously from `observe()`.
 *
 * The DOM in tests has an observer that never delivers. A hook that takes its
 * first measurement from the observer's first delivery, rather than reading
 * layout inside a commit, measures nothing under it. Undo with
 * `vi.unstubAllGlobals()`.
 */
export function installInitialResizeObserver(): void {
  class InitialResizeObserver {
    constructor(private cb: ResizeObserverCallback) {}
    observe(target: Element) {
      this.cb([{ target, contentRect: target.getBoundingClientRect() } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', InitialResizeObserver)
}
