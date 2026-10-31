/**
 * Two outline rules from the login survey (GAP-1):
 *
 * - A 0 x 0 box that hides nothing is walked through. Reddit's login form sits
 *   under custom-element hosts (`auth-flow-manager`, `faceplate-partial`) whose
 *   own box is 0 x 0 while their shadow content is drawn; the walker cut the
 *   whole subtree and the outline held one skip link.
 * - A text box something lies over (ScienceDirect's OneTrust filter, Springer
 *   Nature's consent dialog) keeps its ref and is marked covered, so the agent
 *   closes the dialog before an ask that would be refused "covered".
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
const snapshot = () =>
  (new Function(`return (${walkerSource()})`)() as { snapshot: () => { nodes: Node[] } }).snapshot().nodes
const named = (nodes: Node[], name: string) => nodes.find(n => n.name === name)

/** A login form inside a 0 x 0 host, its fields drawn in the host's open shadow root. */
function zeroHost(style = '') {
  setHtml(document.body, `<main><auth-flow style="${style}"></auth-flow></main>`)
  const host = $('auth-flow')
  const root = host.attachShadow({ mode: 'open' })
  setHtml(root, '<input id="u" type="text" aria-label="Email or username"><button>Log In</button>')
  boxes.set($('main'), [0, 0, 800, 600])
  // The host itself is NOT boxed: 0 x 0, no offsetParent, like Reddit's.
  boxes.set(root.querySelector('#u') as Element, [20, 20, 300, 30])
  boxes.set(root.querySelector('button') as Element, [20, 60, 100, 30])
  hitAt = () => root.querySelector('#u')
}

describe('a 0 x 0 box in the snapshot', () => {
  it('is walked through when it hides nothing: its drawn children get refs', () => {
    zeroHost()
    const nodes = snapshot()
    expect(named(nodes, 'Email or username')?.ref).toBeTruthy()
    expect(named(nodes, 'Log In')?.ref).toBeTruthy()
  })

  it.each([
    ['display: none', 'display: none'],
    ['visibility: hidden', 'visibility: hidden'],
    ['overflow: hidden', 'overflow: hidden'],
    ['overflow: auto', 'overflow: auto'],
    ['contain: paint', 'contain: paint'],
    ['clip-path', 'clip-path: inset(50%)'],
    ['opacity: 0', 'opacity: 0'],
    ['an opacity(0) filter', 'filter: opacity(0)'],
  ])('still cuts the subtree when it is %s', (_name, style) => {
    zeroHost(style)
    expect(named(snapshot(), 'Email or username')).toBeUndefined()
  })

  it('never lists the 0 x 0 box itself, even when it is a control', () => {
    setHtml(document.body, '<main><a href="/x" id="z"><span id="in">Inner</span></a></main>')
    boxes.set($('main'), [0, 0, 800, 600])
    boxes.set($('#in'), [0, 0, 50, 20])
    const nodes = snapshot()
    expect(nodes.find(n => n.role === 'link')).toBeUndefined()
  })
})

describe('a covered text box in the snapshot', () => {
  function form() {
    setHtml(document.body, `<main><input id="u" type="email" aria-label="Email"><button>Continue</button>
      <div id="consent" role="dialog"><button>Reject all</button></div></main>`)
    boxes.set($('main'), [0, 0, 800, 600]); boxes.set($('#u'), [20, 20, 300, 30])
    boxes.set($('main > button'), [20, 60, 100, 30]); boxes.set($('#consent'), [0, 0, 800, 600])
    boxes.set($('#consent button'), [300, 500, 100, 30])
  }

  it('keeps its ref and says it is covered', () => {
    form()
    hitAt = () => $('#consent')
    const email = named(snapshot(), 'Email')
    expect(email?.ref).toBeTruthy()
    expect(email?.state?.covered).toBe(true)
    // What covers it is in the outline too, so the agent can close it.
    expect(named(snapshot(), 'Reject all')?.ref).toBeTruthy()
  })

  it('is not marked when nothing lies over it', () => {
    form()
    hitAt = () => $('#u')
    expect(named(snapshot(), 'Email')?.state?.covered).toBeUndefined()
  })
})
