/**
 * The walker's `resolve` scrolls a ref into view and measures it. The harness
 * re-measures with `scroll` false after the page has had time to react to the
 * scroll, so that read must leave the scroll position alone: a second
 * `scrollIntoView` would re-centre the element and hide the shift.
 *
 * happy-dom has no layout: boxes are stubbed per element.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walker(): {
  snapshot: () => { nodes: { name: string; ref?: string }[] }
  resolve: (ref: string, scroll?: boolean) => { ok: boolean; x?: number; y?: number }
} {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return new Function(`return (${lines.join('\n').trim()})`)()
}

type Box = [number, number, number, number]
const boxes = new Map<Element, Box>()
let scrolls = 0
const realRect = Element.prototype.getBoundingClientRect
const realScroll = Element.prototype.scrollIntoView
const realHtmlScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
const realOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')
const realFromPoint = Object.getOwnPropertyDescriptor(document, 'elementFromPoint')

beforeEach(() => {
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const [x, y, w, h] = boxes.get(this) ?? [0, 0, 0, 0]
    return { left: x, top: y, width: w, height: h, right: x + w, bottom: y + h, x, y, toJSON() {} } as DOMRect
  }
  Element.prototype.scrollIntoView = function () { scrolls += 1 }
  HTMLElement.prototype.scrollIntoView = Element.prototype.scrollIntoView
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get(this: HTMLElement) { return boxes.has(this) ? document.body : null },
  })
  ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = () => null
})
afterEach(() => {
  Element.prototype.getBoundingClientRect = realRect
  Element.prototype.scrollIntoView = realScroll
  if (realHtmlScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', realHtmlScroll)
  else delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
  if (realOffsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', realOffsetParent)
  else delete (HTMLElement.prototype as { offsetParent?: unknown }).offsetParent
  if (realFromPoint) Object.defineProperty(document, 'elementFromPoint', realFromPoint)
  else delete (document as { elementFromPoint?: unknown }).elementFromPoint
  boxes.clear()
  scrolls = 0
  document.body.replaceChildren()
})

describe('resolve', () => {
  it('scrolls by default and measures without scrolling when scroll is false', () => {
    setHtml(document.body, '<main><a href="/item" id="t">Demo kettle</a></main>')
    const link = document.querySelector('#t') as Element
    boxes.set(document.querySelector('main') as Element, [0, 0, 800, 2000])
    boxes.set(link, [100, 400, 200, 44])
    const w = walker()
    const ref = w.snapshot().nodes.find(n => n.name === 'Demo kettle')?.ref as string
    expect(ref).toBeTruthy()

    expect(w.resolve(ref)).toMatchObject({ ok: true, y: 422 })
    expect(scrolls).toBe(1)

    // The page collapses a 60 px header after the scroll: the link moves up.
    boxes.set(link, [100, 340, 200, 44])
    expect(w.resolve(ref, false)).toMatchObject({ ok: true, y: 362 })
    expect(scrolls).toBe(1)
  })
})
