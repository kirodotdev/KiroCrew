/**
 * The walker's `pick` and `locate`: the two read-only methods an app's PagePort
 * runs (`ops.APP_OPS`). `pick` names the element drawn at a point and gives it a
 * ref `resolve` takes; a password field gets no ref. `locate` answers the boxes a
 * text is drawn in, and none for hidden text.
 *
 * happy-dom has no layout: boxes and `elementFromPoint` are stubbed.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

type Box = { x: number; y: number; w: number; h: number }
type Walker = {
  pick: (x: number, y: number) => { ok: boolean; code?: string; ref?: string; text?: string; box?: Box }
  locate: (texts: string[], cap: number) => { ok: boolean; found: { text: string; boxes: Box[] }[] }
  resolve: (ref: string, scroll?: boolean) => { ok: boolean; x?: number; y?: number }
}

function walker(): Walker {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return new Function(`return (${lines.join('\n').trim()})`)()
}

const rects = new Map<Element, [number, number, number, number]>()
let atPoint: Element | null = null
const realRect = Element.prototype.getBoundingClientRect
const realRangeRect = Range.prototype.getBoundingClientRect
const realRangeRects = Range.prototype.getClientRects
const realComputedStyle = window.getComputedStyle.bind(window)
// Text drawn by an element with no box of its own (display:contents): its range's box.
const textBoxes = new Map<Element, [number, number, number, number]>()
const realFromPoint = Object.getOwnPropertyDescriptor(document, 'elementFromPoint')
const realOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

function domRect([x, y, w, h]: [number, number, number, number]): DOMRect {
  return { left: x, top: y, width: w, height: h, right: x + w, bottom: y + h, x, y, toJSON() {} } as DOMRect
}

beforeEach(() => {
  Element.prototype.getBoundingClientRect = function (this: Element) {
    return domRect(rects.get(this) ?? [0, 0, 0, 0])
  }
  // happy-dom omits the initial visibility value, which `textNodeShown` checks.
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el) => new Proxy(realComputedStyle(el), {
    get(style, key) {
      const value = Reflect.get(style, key)
      return key === 'visibility' ? value || 'visible' : value
    },
  }))
  // A text range measures as its parent element's box, or the box its text draws.
  const textBox = (range: Range) => {
    const node = range.startContainer
    const parent = node.nodeType === 1
      ? (node as Element)
      : node.parentElement ?? ((node.parentNode as ShadowRoot | null)?.host ?? null)
    return (parent && (rects.get(parent) ?? textBoxes.get(parent))) ?? null
  }
  Range.prototype.getBoundingClientRect = function (this: Range) {
    return domRect(textBox(this) ?? [0, 0, 0, 0])
  }
  Range.prototype.getClientRects = function (this: Range) {
    const box = textBox(this)
    return (box ? [domRect(box)] : []) as unknown as DOMRectList
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get(this: HTMLElement) { return rects.has(this) ? document.body : null },
  })
  ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = () => atPoint
})
afterEach(() => {
  vi.restoreAllMocks()
  Range.prototype.getClientRects = realRangeRects
  textBoxes.clear()
  Element.prototype.getBoundingClientRect = realRect
  Range.prototype.getBoundingClientRect = realRangeRect
  if (realFromPoint) Object.defineProperty(document, 'elementFromPoint', realFromPoint)
  else delete (document as { elementFromPoint?: unknown }).elementFromPoint
  if (realOffsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', realOffsetParent)
  else delete (HTMLElement.prototype as { offsetParent?: unknown }).offsetParent
  rects.clear()
  atPoint = null
  document.body.replaceChildren()
})

describe('pick', () => {
  it('names the element at the point with a ref resolve takes', () => {
    setHtml(document.body, '<main><button id="buy">Buy now</button></main>')
    const button = document.querySelector('#buy') as Element
    rects.set(button, [100, 200, 80, 30])
    atPoint = button
    const w = walker()
    const out = w.pick(120, 210)
    expect(out).toMatchObject({ ok: true, text: 'Buy now', box: { x: 100, y: 200, w: 80, h: 30 } })
    expect(w.resolve(out.ref as string, false)).toMatchObject({ ok: true, x: 140, y: 215 })
  })

  it('gives a password field no ref', () => {
    setHtml(document.body, '<input id="pw" type="password" value="hunter2">')
    atPoint = document.querySelector('#pw')
    expect(walker().pick(1, 1)).toEqual({ ok: false, code: 'password_field' })
  })

  it('answers nothing_there off the page', () => {
    expect(walker().pick(1, 1)).toEqual({ ok: false, code: 'nothing_there' })
  })

  it('refuses a point inside a one-time-code editable, and a quick-ask field', () => {
    setHtml(
      document.body,
      '<div contenteditable="true" name="otp"><span id="digit">482913</span></div>' +
        '<input id="asked" data-kc-asked value="mine">',
    )
    atPoint = document.querySelector('#digit')
    expect(walker().pick(1, 1)).toEqual({ ok: false, code: 'password_field' })
    atPoint = document.querySelector('#asked')
    expect(walker().pick(1, 1)).toEqual({ ok: false, code: 'password_field' })
  })

  it('never names an element with a protected field, a form value or another element', () => {
    setHtml(
      document.body,
      '<section id="box">Code <div contenteditable="true" id="otpCode">482913</div></section>' +
        '<div id="wrap"><label for="t">Token</label><textarea id="t" name="token">sk-live-77</textarea></div>' +
        '<button id="lb" aria-labelledby="t">x</button>' +
        '<label id="lab">Code <span contenteditable="true" name="otp">1234</span><b id="in">go</b></label>' +
        '<div id="scr">Hi<script>var k = "s3cret"</script></div>',
    )
    for (const id of ['box', 'otpCode', 'wrap', 't', 'lb', 'lab', 'in', 'scr']) {
      rects.set(document.querySelector(`#${id}`) as Element, [0, 0, 50, 10])
    }
    const w = walker()
    const name = (id: string) => {
      atPoint = document.querySelector(`#${id}`)
      return w.pick(1, 1)
    }
    expect(name('box')).toMatchObject({ ok: true, text: 'Code' })
    expect(name('otpCode')).toEqual({ ok: false, code: 'password_field' })
    expect(name('t')).toEqual({ ok: false, code: 'password_field' })
    expect(name('wrap')).toMatchObject({ ok: true, text: 'Token' })
    // Its own text only: never the labelledby target.
    expect(name('lb')).toMatchObject({ ok: true, text: 'x' })
    expect(name('in')).toMatchObject({ ok: true, text: 'go' })
    expect(name('lab').text).not.toContain('1234')
    expect(name('scr')).toMatchObject({ ok: true, text: 'Hi' })
  })

  it('withholds a name over its cap whole, never cutting it', () => {
    setHtml(document.body, `<p id="long">${'a'.repeat(1990)} sk-ant-api03-abcdefghij</p>`)
    atPoint = document.querySelector('#long')
    rects.set(atPoint as Element, [0, 0, 50, 10])
    expect(walker().pick(1, 1)).toMatchObject({ ok: true, text: '' })
  })

  it('names a field by its own attributes, never its value', () => {
    setHtml(document.body, '<input id="q" aria-label="Search" value="my query">')
    atPoint = document.querySelector('#q')
    rects.set(atPoint as Element, [0, 0, 50, 10])
    expect(walker().pick(1, 1)).toMatchObject({ ok: true, text: 'Search' })
  })
})

describe('locate, protected text', () => {
  it('never searches a protected editable, a form field or a script', () => {
    setHtml(
      document.body,
      '<div contenteditable="true" name="otp" id="otp"><span id="s">482913</span></div>' +
        '<textarea id="t">482913</textarea><script>var c = "482913"</script><p id="p">482913</p>',
    )
    for (const id of ['otp', 's', 't', 'p']) rects.set(document.querySelector(`#${id}`) as Element, [0, 0, 9, 9])
    expect(walker().locate(['482913'], 20).found[0].boxes).toHaveLength(1)
  })

  it('searches open shadow roots, with one cap across every root', () => {
    setHtml(document.body, '<p id="p">$5</p><x-price id="host"></x-price>')
    const host = document.querySelector('#host') as Element
    const shadow = host.attachShadow({ mode: 'open' })
    setHtml(shadow, '<span id="s">$5</span><textarea name="otp">$5</textarea>')
    rects.set(document.querySelector('#p') as Element, [0, 0, 20, 10])
    rects.set(shadow.querySelector('#s') as Element, [0, 40, 20, 10])
    rects.set(shadow.querySelector('textarea') as Element, [0, 80, 20, 10])
    const w = walker()
    expect(w.locate(['$5'], 20).found[0].boxes).toEqual([
      { x: 0, y: 0, w: 20, h: 10 },
      { x: 0, y: 40, w: 20, h: 10 },
    ])
    expect(w.locate(['$5'], 1).found[0].boxes).toHaveLength(1)
  })

  it("finds a shadow root's own text child, which has no parent element", () => {
    setHtml(document.body, '<x-price id="host"></x-price>')
    const host = document.querySelector('#host') as Element
    const shadow = host.attachShadow({ mode: 'open' })
    shadow.append(document.createTextNode('$7'))
    rects.set(host, [3, 4, 20, 10])
    expect(walker().locate(['$7'], 20).found[0].boxes).toEqual([{ x: 3, y: 4, w: 20, h: 10 }])
  })

  it('finds text a display:contents element draws, though that element has no box', () => {
    setHtml(document.body, '<span id="c" style="display:contents">$19.99</span>')
    textBoxes.set(document.querySelector('#c') as Element, [5, 6, 30, 10])
    expect(walker().locate(['$19.99'], 20).found[0].boxes).toEqual([{ x: 5, y: 6, w: 30, h: 10 }])
  })

  it('answers every occurrence in one text node, up to cap', () => {
    setHtml(document.body, '<p id="p">$9 or $9 or $9</p>')
    rects.set(document.querySelector('#p') as Element, [0, 0, 100, 10])
    const w = walker()
    expect(w.locate(['$9'], 20).found[0].boxes).toHaveLength(3)
    expect(w.locate(['$9'], 2).found[0].boxes).toHaveLength(2)
  })
})

describe('locate', () => {
  it('answers the boxes of visible text only, in order', () => {
    setHtml(
      document.body,
      '<p id="a">Price $19.99 today</p><p id="b" style="display:none">$19.99</p><p id="c">$19.99</p>',
    )
    rects.set(document.querySelector('#a') as Element, [0, 10, 200, 20])
    rects.set(document.querySelector('#c') as Element, [0, 50, 60, 20])
    const out = walker().locate(['$19.99', 'missing'], 20)
    expect(out.ok).toBe(true)
    expect(out.found).toEqual([
      { text: '$19.99', boxes: [{ x: 0, y: 10, w: 200, h: 20 }, { x: 0, y: 50, w: 60, h: 20 }] },
      { text: 'missing', boxes: [] },
    ])
  })

  it('stops at cap boxes per text', () => {
    setHtml(document.body, '<p id="a">x</p><p id="b">x</p><p id="c">x</p>')
    for (const id of ['a', 'b', 'c']) rects.set(document.querySelector(`#${id}`) as Element, [0, 0, 5, 5])
    expect(walker().locate(['x'], 2).found[0].boxes).toHaveLength(2)
  })
})
