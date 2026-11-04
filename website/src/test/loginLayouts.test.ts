/**
 * Two login layouts from the top-100 login survey, run against the real sources:
 *
 * - A `display: contents` wrapper (Adobe, ok.ru, Notion) draws no box, so the
 *   snapshot walker's visibility test failed it and dropped the whole form. The
 *   walker now walks through it and lets each child judge itself.
 * - The field's own floating label over the field's centre (Xiaomi:
 *   `label.mi-floating-label for="account"`, the input has no id) made the quick
 *   ask read the field as covered. The ask's inspector (`quick_ask.py`,
 *   `_INSPECT_JS`) now lets that label through, and still refuses a real overlay.
 *
 * happy-dom has no layout: boxes and `elementFromPoint` are stubbed per element.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

/** `_INSPECT_JS` as Python holds it: a triple-quoted string, `\\` read as `\`. */
function inspectSource(): string {
  const py = readFileSync(join(HARNESS_DIR, 'quick_ask.py'), 'utf8')
  const start = py.indexOf('_INSPECT_JS = """') + '_INSPECT_JS = """'.length
  return py.slice(start, py.indexOf('"""', start)).replace(/\\\\/g, '\\')
}

type Box = [number, number, number, number]
const boxes = new Map<Element, Box>()
let hitAt: (x: number, y: number) => Element | null = () => null
const realRect = Element.prototype.getBoundingClientRect
const realOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

beforeEach(() => {
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const [x, y, w, h] = boxes.get(this) ?? [0, 0, 0, 0]
    return { left: x, top: y, width: w, height: h, right: x + w, bottom: y + h, x, y, toJSON() {} } as DOMRect
  }
  // A box-less element (display: contents, display: none) has no offsetParent.
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get(this: HTMLElement) { return boxes.has(this) ? document.body : null },
  })
  ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = (x: number, y: number) => hitAt(x, y)
})
afterEach(() => {
  Element.prototype.getBoundingClientRect = realRect
  if (realOffsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', realOffsetParent)
  boxes.clear()
  hitAt = () => null
  document.body.replaceChildren()
})

const html = (s: string) => { setHtml(document.body, s) }
const $ = (sel: string) => document.querySelector(sel) as HTMLElement
const box = (sel: string, b: Box) => boxes.set($(sel), b)

describe('a display: contents wrapper', () => {
  it('is walked through: the form inside gets refs, the wrapper emits nothing', () => {
    // A <div> stands in for the <form>: happy-dom's form element answers its own
    // offsetParent. The real-Chromium check runs the same shape with a <form>.
    html(`<main><div id="wrap" style="display: contents"><div class="form">
      <input id="u" type="email" aria-label="Email">
      <input id="p" type="password" aria-label="Password">
      <button>Continue</button></div></div></main>`)
    box('main', [0, 0, 800, 600]); box('.form', [10, 10, 400, 200])
    box('#u', [20, 20, 300, 30]); box('#p', [20, 60, 300, 30]); box('button', [20, 100, 100, 30])
    // #wrap has no box, as Chrome reports a display: contents element.
    const snap = (new Function(`return (${walkerSource()})`)() as { snapshot: () => { nodes: Array<{ role: string; name: string; ref?: string }> } }).snapshot()
    const named = snap.nodes.map(n => `${n.role}:${n.name}:${n.ref ? 'ref' : ''}`)
    expect(named).toEqual(['textbox:Email:ref', 'textbox:Password:ref', 'button:Continue:ref'])
  })

  it('still drops a display: none subtree and a hidden child of a contents wrapper', () => {
    html(`<main><div style="display: contents"><input id="a" aria-label="Shown"><input id="b" aria-label="Gone"></div>
      <div id="none" style="display: none"><input id="c" aria-label="Hidden"></div></main>`)
    box('main', [0, 0, 800, 600]); box('#a', [0, 0, 100, 20])
    const snap = (new Function(`return (${walkerSource()})`)() as { snapshot: () => { nodes: Array<{ name: string }> } }).snapshot()
    expect(snap.nodes.map(n => n.name)).toEqual(['Shown'])
  })
})

interface Inspect { covered: boolean; labelCovered: boolean; visible: boolean }
const inspect = (el: Element): Inspect => (new Function(`return (${inspectSource()})`)() as (this: Element) => Inspect).call(el)

describe('a field under its own floating label', () => {
  // The Xiaomi shape: the label names for="account", the input has no id.
  const xiaomi = (extra = '') => {
    html(`<form><div class="mi-input"><input name="account" type="text">
      <label class="mi-floating-label" for="account">Email/Phone/Xiaomi Account</label>${extra}</div></form>`)
    box('form', [0, 0, 400, 300]); box('.mi-input', [10, 10, 320, 56])
    box('input', [10, 10, 320, 56]); box('label', [22, 26, 240, 24])
  }

  it('is not covered by that label', () => {
    xiaomi()
    hitAt = () => $('label')
    // Covered, but only by its own label: quick_ask.target_problem lets that through
    // for a field it fills, and never for a button it clicks.
    expect(inspect($('input'))).toMatchObject({ visible: true, covered: true, labelCovered: true })
  })

  it('is not covered by a label it names through el.labels', () => {
    html(`<div><input id="em" type="email"><label for="em"><span id="s">Email</span></label></div>`)
    box('div', [0, 0, 300, 40]); box('input', [0, 0, 300, 40]); box('label', [8, 10, 100, 20])
    hitAt = () => $('#s')
    expect(inspect($('input')).labelCovered).toBe(true)
  })

  it.each([
    ['a button laid over it', '<button id="x">Win</button>', '#x'],
    ['another control in the same wrapper', '<input id="x" type="text">', '#x'],
    ['a container', '<div id="x"></div>', '#x'],
  ])('is still covered by %s', (_name, extra, sel) => {
    xiaomi(extra)
    box(sel, [10, 10, 320, 56])
    hitAt = () => $(sel)
    expect(inspect($('input')).labelCovered).toBe(false)
  })

  it('is still covered by its own floating label once the wrapper holds another control', () => {
    xiaomi('<button id="eye">show</button>')
    box('#eye', [300, 20, 20, 20])
    hitAt = () => $('label')
    expect(inspect($('input')).labelCovered).toBe(false)
  })

  it('is still covered by the label of another field', () => {
    html(`<div><input id="a"><input id="b"><label for="b">B</label></div>`)
    box('div', [0, 0, 300, 40]); box('#a', [0, 0, 300, 40]); box('label', [8, 10, 100, 20])
    hitAt = () => $('label')
    expect(inspect($('#a')).labelCovered).toBe(false)
  })

  it('is still covered by a label that reaches outside the field', () => {
    html(`<div><input id="em" type="email"><label for="em">Email</label></div>`)
    box('div', [0, 0, 300, 40]); box('input', [0, 0, 300, 40]); box('label', [0, 0, 300, 400])
    hitAt = () => $('label')
    expect(inspect($('input')).labelCovered).toBe(false)
  })

  it('is still covered by a label in a page-sized wrapper', () => {
    html(`<section><input name="account"><label class="big">Sign in</label></section>`)
    box('section', [0, 0, 1280, 800]); box('input', [10, 10, 320, 56]); box('label', [0, 0, 1280, 800])
    hitAt = () => $('label')
    expect(inspect($('input')).labelCovered).toBe(false)
  })
})
