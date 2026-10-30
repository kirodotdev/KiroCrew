/**
 * A password field the quick ask would refuse gets no ref in the snapshot.
 *
 * Login survey (twitter, netflix, epicgames, nytimes, shopify, avast): an
 * identifier-first page keeps the next step's password input in the DOM, hidden,
 * disabled or covered. The walker listed it with a ref, the agent asked
 * credentials on it, and `quick_ask.target_problem` refused. The walker now
 * judges a password input by the ask's own rule (`_INSPECT_JS`), and each case
 * here is checked against that inspector run on the same DOM.
 *
 * happy-dom has no layout: boxes and `elementFromPoint` are stubbed per element.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setHtml, appendHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

function inspectSource(): string {
  const py = readFileSync(join(HARNESS_DIR, 'quick_ask.py'), 'utf8')
  const start = py.indexOf('_INSPECT_JS = """') + '_INSPECT_JS = """'.length
  return py.slice(start, py.indexOf('"""', start)).replace(/\\\\/g, '\\')
}

type Box = [number, number, number, number]
type Node = { role: string; name: string; ref?: string; state?: Record<string, unknown> }
const boxes = new Map<Element, Box>()
let hitAt: (x: number, y: number) => Element | null = () => null
const realRect = Element.prototype.getBoundingClientRect
const realOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

beforeEach(() => {
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const [x, y, w, h] = boxes.get(this) ?? [0, 0, 0, 0]
    return { left: x, top: y, width: w, height: h, right: x + w, bottom: y + h, x, y, toJSON() {} } as DOMRect
  }
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

const $ = (sel: string) => document.querySelector(sel) as HTMLElement
const box = (sel: string, b: Box) => boxes.set($(sel), b)
const snapshot = () =>
  (new Function(`return (${walkerSource()})`)() as { snapshot: () => { nodes: Node[] } }).snapshot().nodes

interface Inspect { visible: boolean; covered: boolean; labelCovered: boolean; disabled: boolean; readOnly: boolean }
const inspect = (el: Element): Inspect =>
  (new Function(`return (${inspectSource()})`)() as (this: Element) => Inspect).call(el)
/** `target_problem("password", info) == ""` for a same-document input[type=password]. */
const askOk = (i: Inspect) => i.visible && (!i.covered || i.labelCovered) && !i.disabled && !i.readOnly

/** An identifier-first page: the email field, then the password field set up by *pw*. */
function page(pw: string, extra = '') {
  setHtml(document.body, `<main><div class="f"><input id="u" type="email" aria-label="Email">
    <div class="pw"><input id="p" type="password" aria-label="Password" ${pw}>${extra}</div>
    <button>Next</button></div></main>`)
  box('main', [0, 0, 800, 600]); box('.f', [10, 10, 400, 300]); box('#u', [20, 20, 300, 30])
  box('.pw', [20, 60, 300, 30]); box('#p', [20, 60, 300, 30]); box('button', [20, 120, 100, 30])
  hitAt = (x, y) => (y >= 60 && y < 90 ? $('#p') : null)
}

const password = (nodes: Node[]) => nodes.find(n => n.name === 'Password')

describe('a password input in the snapshot', () => {
  it.each([
    ['shown and free', '', () => {}, ''],
    ['faded to opacity 0', 'style="opacity: 0"', () => {}, 'hidden'],
    ['a 1 x 1 box', '', () => box('#p', [20, 60, 1, 1]), 'hidden'],
    ['inside a faded wrapper', '', () => { $('.pw').style.opacity = '0' }, 'hidden'],
    ['disabled', 'disabled', () => {}, 'disabled'],
    ['read-only', 'readonly', () => {}, 'disabled'],
    ['aria-disabled', 'aria-disabled="true"', () => {}, 'disabled'],
    ['under another element', '', () => {
      appendHtml($('.pw'), '<div id="cover"></div>')
      box('#cover', [20, 60, 300, 30])
      hitAt = () => $('#cover')
    }, 'covered'],
    ['under its own label', '', () => {
      appendHtml($('.pw'), '<label for="p" id="lab">Password</label>')
      box('#lab', [24, 64, 100, 20])
      hitAt = () => $('#lab')
    }, ''],
  ])('%s: has a ref exactly when the quick ask would bind it', (_name, attrs, more, why) => {
    page(attrs)
    more()
    const node = password(snapshot())
    const usable = why === ''
    expect(node).toBeDefined()
    expect(askOk(inspect($('#p')))).toBe(usable)
    expect(Boolean(node?.ref)).toBe(usable)
    expect(node?.state?.unusable ?? '').toBe(why)
    expect(node?.state?.password).toBe(true)
  })

  it('drops the ref of a field whose shadow host is hidden', () => {
    page('')
    const host = document.createElement('div')
    host.style.opacity = '0'
    $('.pw').appendChild(host)
    const inner = document.createElement('input')
    inner.type = 'password'
    inner.setAttribute('aria-label', 'Shadow password')
    host.attachShadow({ mode: 'open' }).appendChild(inner)
    boxes.set(host, [20, 60, 300, 30]); boxes.set(inner, [20, 60, 300, 30])
    hitAt = () => inner
    const node = snapshot().find(n => n.name === 'Shadow password')
    expect(node?.ref).toBeUndefined()
    expect(node?.state?.unusable).toBe('hidden')
  })

  it('keeps its ref outside the viewport: the agent can scroll to it', () => {
    page('')
    box('#p', [20, 5000, 300, 30])
    hitAt = () => null
    expect(password(snapshot())?.ref).toBeTruthy()
  })

  it('drops the ref of a decoy fixed past the window edge: scrolling never reaches it', () => {
    // Shopify: div.next-input-wrapper (position: fixed, height 0) holds the
    // password input at y=824 in an 800 px window.
    page('')
    $('.pw').style.position = 'fixed'
    box('#p', [1060, window.innerHeight + 24, 220, 37])
    hitAt = () => null
    const node = password(snapshot())
    expect(node?.ref).toBeUndefined()
    expect(node?.state?.unusable).toBe('hidden')
  })

  it('keeps the ref of a field below the fold of a scrolling fixed dialog', () => {
    page('')
    $('.f').style.position = 'fixed'
    $('.f').style.overflowY = 'auto'
    box('#p', [20, window.innerHeight + 200, 300, 30])
    hitAt = () => null
    expect(password(snapshot())?.ref).toBeTruthy()
  })

  it('keeps the ref of a field clipped below a scroll box, still inside the window', () => {
    page('')
    $('.f').style.overflowY = 'auto'
    box('.f', [10, 10, 400, 100])
    box('#p', [20, 300, 300, 30])
    hitAt = () => $('main')
    expect(password(snapshot())?.ref).toBeTruthy()
  })

  it('still drops a fixed decoy under a page that has a scroll box above it', () => {
    page('')
    $('main').style.overflowY = 'auto'
    $('.pw').style.position = 'fixed'
    box('#p', [1060, window.innerHeight + 24, 220, 37])
    hitAt = () => null
    expect(password(snapshot())?.ref).toBeUndefined()
  })

  it('keeps the ref of a field scrolled up out of a scroll box', () => {
    page('')
    $('.f').style.overflowY = 'auto'
    box('#p', [20, -200, 300, 30])
    hitAt = () => null
    expect(password(snapshot())?.ref).toBeTruthy()
  })

  it('drops the ref of a field placed before the start of the page', () => {
    page('')
    box('#p', [-9999, 60, 300, 30])
    expect(password(snapshot())?.ref).toBeUndefined()
  })

  it('leaves the account field and other controls untouched', () => {
    page('style="opacity: 0"')
    const named = snapshot().map(n => `${n.name}:${n.ref ? 'ref' : 'none'}`)
    expect(named).toEqual(['Email:ref', 'Password:none', 'Next:ref'])
  })

  it('judges only password inputs: a faded text input keeps its ref', () => {
    page('')
    $('#u').style.opacity = '0'
    expect(snapshot().find(n => n.name === 'Email')?.ref).toBeTruthy()
  })
})
