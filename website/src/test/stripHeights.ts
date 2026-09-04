import { vi } from 'vitest'

/**
 * Give the composer's strips a real measured height in jsdom.
 *
 * jsdom does no layout: every `getBoundingClientRect()` is zero and there is no
 * `ResizeObserver`. The composer measures its strips rather than predicting
 * their height from Tailwind classes, so without this a test renders a strip
 * that measures 0 and the reserved floor looks like no strip at all.
 *
 * The heights are the TEST's, not the component's, and that is the point: the
 * assertions that used to read `INPUT_DRAG_MIN_H + FILE_PREVIEW_H` — a source
 * constant checked against itself — now say "the floor is the drag minimum plus
 * whatever the strip actually measured". A regression in the wiring fails; a
 * change to the strip's padding does not.
 */
export const PREVIEW_STRIP_H = 81
const SESSION_REF_STRIP_H = 43

let byTestid: Record<string, number> = {}
let observed: { cb: ResizeObserverCallback; target: Element; observer: ResizeObserver }[] = []

/**
 * Change what a strip measures mid-test, e.g. when a resize pill lands, and
 * notify the observers watching it.
 *
 * The notification is the point. A strip that grows while staying MOUNTED is
 * exactly the case the ref callback cannot catch, so it is the case the
 * ResizeObserver exists for. A no-op observer stub would let the test pass on
 * mount-driven measurement alone and never exercise that path.
 */
export function setStripHeight(testid: string, height: number): void {
  byTestid[testid] = height
  for (const { cb, target, observer } of observed) {
    if (target.firstElementChild?.getAttribute('data-testid') === testid) {
      cb([{ target } as unknown as ResizeObserverEntry], observer)
    }
  }
}

function rect(height: number): DOMRect {
  return {
    height, width: 0, top: 0, left: 0, right: 0, bottom: height, x: 0, y: 0,
    toJSON: () => ({}),
  } as DOMRect
}

/**
 * Stub layout for the strip wrappers and install a ResizeObserver.
 *
 * Matches on the wrapper's OWN first child rather than a descendant query, so
 * an ancestor that merely contains a strip (the input area, the wrapper) still
 * measures 0 and cannot be mistaken for the strip itself.
 */
export function stubStripHeights(): void {
  byTestid = {
    'preview-strip': PREVIEW_STRIP_H,
    'session-ref-strip': SESSION_REF_STRIP_H,
  }
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    const testid = this.firstElementChild?.getAttribute('data-testid') ?? ''
    return rect(byTestid[testid] ?? 0)
  })
  observed = []
  globalThis.ResizeObserver = class {
    constructor(private cb: ResizeObserverCallback) {}
    observe(target: Element) {
      observed.push({ cb: this.cb, target, observer: this as unknown as ResizeObserver })
    }
    unobserve(target: Element) {
      observed = observed.filter(o => o.target !== target)
    }
    disconnect() {
      observed = observed.filter(o => o.cb !== this.cb)
    }
  } as unknown as typeof ResizeObserver
}
