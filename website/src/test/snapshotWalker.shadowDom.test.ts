/**
 * The page-side walker (`src/kiro_crew/browser_harness/snapshot_walker.js`) and
 * the screencast's password-mask expression (`screencast.py`, `_MASK_JS`), run in
 * a DOM with an OPEN SHADOW ROOT holding a focused password input.
 *
 * `document.activeElement` stops at the shadow host, a <div>. The agent's two
 * input gates (`type` with no ref, a printable `press_key`) ask the walker's
 * `activeField`, and the live-frame mask asks `_MASK_JS` for password boxes;
 * both looked only at the top document, so a web-component login form's
 * password field was typeable by the agent and unmasked in the live view.
 * Pinned here against the real source, evaluated the way the browser does.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

/** `_MASK_JS` exactly as Python concatenates it: the adjacent string literals joined. */
function maskSource(): string {
  const py = readFileSync(join(HARNESS_DIR, 'screencast.py'), 'utf8')
  const start = py.indexOf('_MASK_JS = (')
  const end = py.indexOf('\n)\n', start)
  const body = py.slice(start, end)
  const parts = [...body.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1])
  return parts.join('')
}

interface Walker {
  activeField: () => { focused: boolean; password: boolean; asked: boolean; tag: string }
  focus: (ref: string) => { ok: boolean; focused?: boolean; code?: string }
  snapshot: () => { nodes: Array<{ ref?: string; role: string; state?: { password?: boolean } }> }
}

// Evaluated as the page evaluates it: one global expression, in this window's scope.
const evaluate = (source: string): unknown => new Function(`return (${source})`)()
const evalWalker = (): Walker => evaluate(walkerSource()) as Walker
// The mask expression answers `{boxes, frames, partial, sx, sy}`: the password
// boxes, the opaque embedded documents, whether the walk was cut short, and the
// scroll it read the page at (the turn replay keeps a frame only at that scroll).
type MaskReply = { boxes: number[][]; frames: number[][]; partial: boolean; sx: number; sy: number }
const evalMaskReply = (): MaskReply => evaluate(maskSource()) as MaskReply
const evalMask = (): number[][] => evalMaskReply().boxes

function shadowLogin(): { host: HTMLElement; user: HTMLInputElement; pass: HTMLInputElement } {
  const host = document.createElement('div')
  host.id = 'login'
  document.body.appendChild(host)
  const root = host.attachShadow({ mode: 'open' })
  const user = document.createElement('input')
  user.type = 'text'
  user.name = 'user'
  const pass = document.createElement('input')
  pass.type = 'password'
  pass.name = 'pw'
  root.appendChild(user)
  root.appendChild(pass)
  return { host, user, pass }
}

/** The test DOM lays nothing out: give every element a box so `visible()` and the mask see it. */
function withBoxes(): void {
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const tag = this.tagName.toLowerCase()
    const w = tag === 'input' ? 200 : 800
    const h = tag === 'input' ? 24 : 600
    return { x: 10, y: 20, left: 10, top: 20, width: w, height: h, right: 10 + w, bottom: 20 + h, toJSON: () => ({}) } as DOMRect
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get: () => document.body })
}

afterEach(() => {
  document.body.replaceChildren()
  const w = window as unknown as Record<string, unknown>
  delete w.__kcRefs
  delete w.__kcRefDocObj
  delete w.__kcRefSeq
})

describe('a focused password input inside an open shadow root', () => {
  it('is what activeField reports, so the no-ref type and printable press_key refusals fire', () => {
    const { host, pass } = shadowLogin()
    pass.focus()
    expect(document.activeElement).toBe(host) // the host, which is the bug's shape
    const active = evalWalker().activeField()
    expect(active).toEqual({ focused: true, password: true, asked: false, tag: 'input', hasFocus: true })
  })

  it('is masked in the live frame: the mask expression walks open shadow roots', () => {
    withBoxes()
    shadowLogin()
    const boxes = evalMask()
    expect(boxes).toEqual([[10, 20, 200, 24]])
  })

  it('focus() on a shadow text field by ref sees the focus land (not the host)', () => {
    withBoxes()
    const { user } = shadowLogin()
    const w = evalWalker()
    const nodes = w.snapshot().nodes
    const userRef = nodes.find((n) => n.role === 'textbox' && !n.state?.password)?.ref
    expect(userRef).toBeTruthy()
    const result = w.focus(userRef as string)
    expect(result.ok).toBe(true)
    expect(result.focused).toBe(true)
    expect(document.activeElement).not.toBe(user) // the host still, as far as the document can see
  })

  it('a password field inside the root carries the password flag in the outline, and its ref is refused', () => {
    withBoxes()
    shadowLogin()
    const w = evalWalker()
    const pw = w.snapshot().nodes.find((n) => n.state?.password)
    expect(pw?.ref).toBeTruthy()
    expect(w.focus(pw!.ref as string)).toEqual({ ok: false, code: 'password_field' })
  })
})

describe('a quick-ask field the person filled inside an open shadow root', () => {
  it('is counted by askedValues, so the agent screenshot is refused', () => {
    const { user } = shadowLogin()
    user.setAttribute('data-kc-asked', '')
    const w = evalWalker() as unknown as { askedValues: () => number }
    expect(w.askedValues()).toBe(0) // marked but still empty
    user.value = 'me@example.com'
    expect(w.askedValues()).toBe(1)
  })

  it('is never focused, typed into or counted for the agent', () => {
    withBoxes()
    const { user } = shadowLogin()
    user.setAttribute('data-kc-asked', '')
    user.value = '123456'
    user.setAttribute('value', '123456') // a framework that mirrors the value attribute
    const w = evalWalker() as unknown as Walker & { read: (probes: unknown) => unknown }
    const ref = w.snapshot().nodes.find((n) => n.role === 'textbox' && !n.state?.password)?.ref
    expect(w.focus(ref as string)).toEqual({ ok: false, code: 'asked_field' })
    user.focus()
    expect(w.activeField()).toEqual({ focused: true, password: false, asked: true, tag: 'input', hasFocus: true })
  })

  it('blocks every value-attribute probe, so the value cannot be spelled out', () => {
    const code = document.createElement('input')
    code.setAttribute('data-kc-asked', '')
    code.setAttribute('value', '123456')
    document.body.appendChild(code)
    const plain = document.createElement('input')
    plain.setAttribute('value', '1')
    document.body.appendChild(plain)
    // Counters already installed for this document, so observe starts no endless
    // requestAnimationFrame loop the test DOM would wait on at teardown.
    const g = window as unknown as Record<string, unknown>
    g.__kcTickDoc = document
    g.__kcTick = { m: 0, f: 0 }
    const w = evalWalker() as unknown as { observe: (o: unknown) => { read: Record<string, unknown> } }
    // Not on the field, a parent, a sibling, an escaped attribute name or a region.
    const probe = (sel: string) => w.observe({ read: [{ name: 'a', sel, prop: 'count' }] }).read.a
    expect(probe('input[value^="1"]')).toBeNull()
    expect(probe('body:has(input[data-kc-asked][value^="1"])')).toBeNull()
    expect(probe('input[data-kc-asked][value^="1"] ~ *')).toBeNull()
    expect(probe('input[\\76 alue^="1"]')).toBeNull()
    expect(probe('input')).toBe(2) // a selector that tests no value still works
    expect((w.observe({ region: 'body:has([value^="1"])' }) as unknown as { ok: boolean }).ok).toBe(false)
    delete g.__kcTickDoc
    delete g.__kcTick
  })
})

describe('the top-document case still works the same', () => {
  it('a plain focused password input is reported and masked', () => {
    withBoxes()
    const pass = document.createElement('input')
    pass.type = 'password'
    document.body.appendChild(pass)
    pass.focus()
    expect(evalWalker().activeField()).toEqual({ focused: true, password: true, asked: false, tag: 'input', hasFocus: true })
    expect(evalMask()).toEqual([[10, 20, 200, 24]])
    expect(evalMaskReply()).toEqual({ boxes: [[10, 20, 200, 24]], frames: [], partial: false, sx: 0, sy: 0 })
  })

  it('nothing focused reads as no field', () => {
    expect(evalWalker().activeField().focused).toBe(false)
  })
})
