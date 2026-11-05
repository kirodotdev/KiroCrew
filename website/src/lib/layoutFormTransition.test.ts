import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { COMPOSER_ATTR, leadingEdge, runLayoutFormChange, TRANSCRIPT_ATTR } from './layoutFormTransition'

type Transition = { finished: Promise<void>; resolve: () => void }
let started: Array<() => void> = []
let transitions: Transition[] = []

function installViewTransitions() {
  started = []
  transitions = []
  ;(document as unknown as { startViewTransition: unknown }).startViewTransition = vi.fn((update: () => void) => {
    started.push(update)
    let resolve!: () => void
    const finished = new Promise<void>(r => { resolve = r })
    const t = { finished, resolve }
    transitions.push(t)
    return t
  })
}

function setReducedMotion(on: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn((q: string) => ({ matches: on && q.includes('prefers-reduced-motion'), media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  })
}

function transcript() {
  const pane = document.createElement('div')
  const scroller = document.createElement('div')
  scroller.className = 'chat-container'
  const dock = document.createElement('div')
  dock.setAttribute('data-testid', 'composer-dock-root')
  pane.append(scroller, dock)
  document.body.appendChild(pane)
  return { scroller, dock }
}

const root = document.documentElement

beforeEach(() => {
  setReducedMotion(false)
  installViewTransitions()
  root.removeAttribute('dir')
})
afterEach(async () => {
  for (const t of transitions) t.resolve()
  await Promise.resolve()
  delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
  document.body.replaceChildren()
  root.removeAttribute('dir')
  delete root.dataset.layoutMorph
  delete root.dataset.layoutMorphEdge
})

describe('runLayoutFormChange', () => {
  it('runs a live-resize form change inside one view transition and cleans up after it', async () => {
    const { scroller, dock } = transcript()
    const apply = vi.fn()
    runLayoutFormChange(apply, { toNarrow: false, cause: 'resize' })
    expect(dock.hasAttribute(COMPOSER_ATTR)).toBe(true)
    // The old form is captured first: nothing has switched yet.
    expect(apply).not.toHaveBeenCalled()
    expect(started).toHaveLength(1)
    expect(root.dataset.layoutMorph).toBe('unfold')
    expect(scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(true)
    started[0]()
    expect(apply).toHaveBeenCalledTimes(1)
    transitions[0].resolve()
    await transitions[0].finished
    await Promise.resolve()
    expect(root.dataset.layoutMorph).toBeUndefined()
    expect(root.dataset.layoutMorphEdge).toBeUndefined()
    expect(scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(false)
    expect(dock.hasAttribute(COMPOSER_ATTR)).toBe(false)
  })

  it('keeps the markers for a second switch that skips the first one mid-animation', async () => {
    const { scroller, dock } = transcript()
    runLayoutFormChange(vi.fn(), { toNarrow: false, cause: 'resize' })
    started[0]()
    // The viewport crosses back before the first animation ends. The browser
    // skips the first transition, so its `finished` settles now.
    runLayoutFormChange(vi.fn(), { toNarrow: true, cause: 'resize' })
    transitions[0].resolve()
    await transitions[0].finished
    await Promise.resolve()
    expect(root.dataset.layoutMorph).toBe('fold')
    expect(root.dataset.layoutMorphEdge).toBe('left')
    expect(scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(true)
    expect(dock.hasAttribute(COMPOSER_ATTR)).toBe(true)
    // The newest transition's own end still cleans up.
    transitions[1].resolve()
    await transitions[1].finished
    await Promise.resolve()
    expect(root.dataset.layoutMorph).toBeUndefined()
    expect(scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(false)
    expect(dock.hasAttribute(COMPOSER_ATTR)).toBe(false)
  })

  it('takes the markers off the first transition\'s nodes when a second switch names others', () => {
    const first = transcript()
    runLayoutFormChange(vi.fn(), { toNarrow: false, cause: 'resize' })
    first.scroller.className = ''
    const second = transcript()
    runLayoutFormChange(vi.fn(), { toNarrow: true, cause: 'resize' })
    expect(first.scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(false)
    expect(first.dock.hasAttribute(COMPOSER_ATTR)).toBe(false)
    expect(second.scroller.hasAttribute(TRANSCRIPT_ATTR)).toBe(true)
  })

  it('marks a fold as "fold" so the sidebar slides out', () => {
    runLayoutFormChange(vi.fn(), { toNarrow: true, cause: 'change' })
    expect(root.dataset.layoutMorph).toBe('fold')
  })

  it('switches instantly without the View Transitions API', () => {
    delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
    const apply = vi.fn()
    runLayoutFormChange(apply, { toNarrow: false, cause: 'resize' })
    expect(apply).toHaveBeenCalledTimes(1)
    expect(root.dataset.layoutMorph).toBeUndefined()
  })

  it('switches instantly under prefers-reduced-motion: reduce', () => {
    setReducedMotion(true)
    const apply = vi.fn()
    runLayoutFormChange(apply, { toNarrow: false, cause: 'resize' })
    expect(apply).toHaveBeenCalledTimes(1)
    expect(document.startViewTransition).not.toHaveBeenCalled()
  })

  it.each([
    ['an ARIA modal dialog', { role: 'dialog', 'aria-modal': 'true' }],
    ['an alert dialog', { role: 'alertdialog', 'aria-modal': 'true' }],
    ['an open Radix dialog', { role: 'dialog', 'data-state': 'open' }],
  ])('switches instantly while %s covers the page, so no snapshot paints over it', (_label, attrs) => {
    transcript()
    const dialog = document.createElement('div')
    for (const [k, v] of Object.entries(attrs)) dialog.setAttribute(k, v)
    document.body.appendChild(dialog)
    const apply = vi.fn()
    runLayoutFormChange(apply, { toNarrow: false, cause: 'resize' })
    expect(apply).toHaveBeenCalledTimes(1)
    expect(document.startViewTransition).not.toHaveBeenCalled()
    expect(root.dataset.layoutMorph).toBeUndefined()
  })

  it('still animates with an open Radix popover, which is not modal', () => {
    transcript()
    const wrapper = document.createElement('div')
    wrapper.setAttribute('data-radix-popper-content-wrapper', '')
    const popover = document.createElement('div')
    popover.setAttribute('role', 'dialog')
    popover.setAttribute('data-state', 'open')
    wrapper.appendChild(popover)
    document.body.appendChild(wrapper)
    runLayoutFormChange(vi.fn(), { toNarrow: false, cause: 'resize' })
    expect(document.startViewTransition).toHaveBeenCalledTimes(1)
  })

  it.each(['pageshow', 'visibilitychange'])('switches instantly for a change noticed on %s, which no reader watched happen', cause => {
    const apply = vi.fn()
    runLayoutFormChange(apply, { toNarrow: true, cause })
    expect(apply).toHaveBeenCalledTimes(1)
    expect(document.startViewTransition).not.toHaveBeenCalled()
  })

  it('gives keyboard focus back when the switch dropped it', () => {
    const input = document.createElement('textarea')
    document.body.appendChild(input)
    input.focus()
    runLayoutFormChange(() => { input.blur() }, { toNarrow: false, cause: 'resize' })
    started[0]()
    expect(document.activeElement).toBe(input)
  })
})

describe('leading edge', () => {
  it('is the left edge in a left-to-right document', () => {
    expect(leadingEdge(root)).toBe('left')
    runLayoutFormChange(vi.fn(), { toNarrow: false, cause: 'resize' })
    expect(root.dataset.layoutMorphEdge).toBe('left')
  })

  it('is the right edge in a right-to-left document', () => {
    root.setAttribute('dir', 'rtl')
    expect(leadingEdge(root)).toBe('right')
    runLayoutFormChange(vi.fn(), { toNarrow: false, cause: 'resize' })
    expect(root.dataset.layoutMorphEdge).toBe('right')
  })
})

describe('layout-morph.css', () => {
  const css = readFileSync(join(__dirname, '..', 'styles', 'layout-morph.css'), 'utf8')
  const index = readFileSync(join(__dirname, '..', 'index.css'), 'utf8')

  it('is imported by the app stylesheet', () => {
    expect(index).toContain('@import "./styles/layout-morph.css";')
  })

  it('scopes every rule to the transition-time root attribute', () => {
    const selectors = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/^([^@{}\s][^{}]*)\{/gm)].map(m => m[1].trim())
      .filter(sel => !/^(from|to)$/.test(sel))
    expect(selectors.length).toBeGreaterThan(0)
    for (const sel of selectors) for (const part of sel.split(',')) expect(part.trim()).toMatch(/^:root\[data-layout-morph/)
  })

  it('animates transform and opacity only', () => {
    for (const m of css.matchAll(/@keyframes [\w-]+ \{([^@]*?)\}\s*\}/g)) {
      const props = [...m[1].matchAll(/([a-z-]+)\s*:/g)].map(p => p[1])
      expect(props.every(p => p === 'transform' || p === 'opacity')).toBe(true)
    }
  })

  it('slides the sidebar from the right edge in a right-to-left document', () => {
    expect(css).toMatch(/\[data-layout-morph="unfold"\]\[data-layout-morph-edge="right"\]::view-transition-new\(kc-sessions\)\s*\{\s*animation-name:\s*kc-sessions-in-from-right/)
    expect(css).toMatch(/@keyframes kc-sessions-in-from-right \{ from \{ transform: translateX\(100%\)/)
    expect(css).toMatch(/@keyframes kc-sessions-in-from-left \{ from \{ transform: translateX\(-100%\)/)
  })

  it('stops all movement under reduced motion and keeps the duration short', () => {
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*animation: none !important/)
    expect(css).toMatch(/animation-duration: 300ms;\s*animation-timing-function: ease-out;/)
  })
})
