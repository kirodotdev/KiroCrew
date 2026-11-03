// @vitest-environment jsdom
/**
 * The conductor's status overlay: its HTML is sanitized, each item is pinned to
 * the words it names and follows them through edits, items whose words are gone
 * wait in the tray, and the session convention opens a session.
 *
 * jsdom, like the dashboard page tests: DOMPurify does not sanitize under
 * happy-dom, so a sanitizer test there would pass or fail for the wrong reason.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { findAnchor, overlayExtension, overlaySlug, parseOverlay, setOverlay } from '../pages/chat/page/docOverlay'

/** Let CodeMirror run its measure phase, where the overlay is positioned. */
const nextFrame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(() => resolve(undefined))) })

describe('parseOverlay', () => {
  it('keeps the conductor\'s markup and drops anything that could run, reach out or submit', () => {
    const items = parseOverlay(`
      <div data-anchor="## Pricing" data-place="margin" class="ov-card" onclick="alert(1)">
        <b>Pricing</b> · 2 of 3 drafts <img src="https://x.test/t.png"><script>alert(2)</script>
        <form><input></form><a href="https://evil.test">link</a>
      </div>
      <span class="ov-pill ov-busy" data-place="over" data-anchor="launch date">Checking</span>
      <style>body{display:none}</style>
      <div class="ov-pill">Whole doc: 3 workers</div>`)
    expect(items.map(i => [i.anchor, i.place])).toEqual([['## Pricing', 'margin'], ['launch date', 'over'], [null, 'margin']])
    const html = items.map(i => i.html).join('')
    expect(items[0].html).toContain('<b>Pricing</b>')
    for (const gone of ['onclick', '<script', '<img', '<form', '<input', 'href=', '<style', 'display:none']) expect(html).not.toContain(gone)
  })

  it('names the overlay artifact after its doc', () => {
    expect(overlaySlug('0123abcd')).toBe('doc-overlay-0123abcd')
  })
})

describe('findAnchor', () => {
  it('finds exact text, then forgives case and spacing', () => {
    expect(findAnchor('a Launch  Date here', 'Launch  Date')).toBe(2)
    expect(findAnchor('a Launch  Date here', 'launch date')).toBe(2)
    expect(findAnchor('nothing', 'launch')).toBeNull()
  })
})

describe('the overlay in the editor', () => {
  let view: EditorView | null = null
  afterEach(() => { act(() => { view?.destroy() }); view = null; document.body.replaceChildren() })

  function make(doc: string) {
    const host = { current: { openSession: vi.fn() } }
    act(() => {
      view = new EditorView({ parent: document.body, state: EditorState.create({ doc, extensions: [overlayExtension(host)] }) })
    })
    return { v: view!, host: host.current }
  }
  const items = (v: EditorView) => [...v.dom.querySelectorAll('[data-testid="overlay-item"]')] as HTMLElement[]
  const show = async (v: EditorView, html: string) => {
    act(() => { v.dispatch({ effects: setOverlay.of(parseOverlay(html)) }) })
    await nextFrame()
  }

  it('draws items outside the text and keeps them on their words through edits', async () => {
    const { v } = make('# Doc\nintro\n## Pricing\nsome text')
    await show(v, '<div data-anchor="## Pricing" class="ov-card">Pricing · 2 of 3</div><span data-place="over" data-anchor="intro" class="ov-pill">Busy</span>')
    const [card, pill] = items(v)
    expect(card.className).toContain('cm-overlay-margin')
    expect(pill.className).toContain('cm-overlay-over')
    expect(v.contentDOM.contains(card)).toBe(false)
    expect([...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent)).toEqual(['# Doc', 'intro', '## Pricing', 'some text'])
    // Typing above the words moves them; the item is placed where they are now.
    const before = parseFloat(items(v)[0].style.top)
    act(() => { v.dispatch({ changes: { from: 0, insert: 'one\ntwo\nthree\n' } }) })
    await nextFrame()
    expect(items(v)).toHaveLength(2)
    expect(parseFloat(items(v)[0].style.top)).toBeGreaterThanOrEqual(before)
  })

  it('an item whose words are gone, or that names none, waits in the tray', async () => {
    const { v } = make('only this')
    await show(v, '<div data-anchor="deleted words">Gone</div><div>Doc-wide</div>')
    const tray = v.dom.querySelector('[data-testid="doc-overlay-tray"]') as HTMLElement
    expect(tray.hidden).toBe(false)
    expect(tray.textContent).toContain('Gone')
    expect(tray.textContent).toContain('Doc-wide')
  })

  it('data-open-session opens that session', async () => {
    const { v, host } = make('the plan')
    await show(v, '<div data-anchor="plan"><span data-open-session="w-7">Draft · working</span></div>')
    fireEvent.click(v.dom.querySelector('[data-open-session="w-7"]')!)
    expect(host.openSession).toHaveBeenCalledWith('w-7')
  })
})

describe('in-progress overlay items move', () => {
  it('every animation the overlay classes use has its keyframes in the one shared copy', async () => {
    const { OVERLAY_CLASS_STYLE, OVERLAY_KEYFRAMES } = await import('../pages/chat/page/docOverlay')
    const used = new Set<string>()
    for (const rules of Object.values(OVERLAY_CLASS_STYLE)) {
      const m = /^(ov-[a-z]+)/.exec(rules.animation ?? '')
      if (m) used.add(m[1])
    }
    expect([...used].sort()).toEqual(['ov-breathe', 'ov-ping', 'ov-pulse', 'ov-slide', 'ov-spin', 'ov-stripes', 'ov-sweep'])
    for (const name of used) expect(OVERLAY_KEYFRAMES).toContain(`@keyframes ${name}{`)
    // Reduced motion keeps the state and drops the movement.
    expect(OVERLAY_KEYFRAMES).toMatch(/prefers-reduced-motion: reduce\)\{\[class\*="ov-"\][^}]*animation:none!important/)
  })

  // The doc conductor's copy of this rule is in its standing brief (doc_session_brief.py).
  it('the board conductor is told that work in progress must move', async () => {
    const { buildBoardTurn } = await import('../pages/chat/page/board/boardTurn')
    const { emptyBoard } = await import('../pages/chat/page/board/boardModel')
    const board = buildBoardTurn({ id: 'q', request: 'r', board: emptyBoard(), selected: [], followUp: false, docId: 'b' })
    for (const turn of [board]) {
      expect(turn).toContain('Anything still in progress MUST move')
      expect(turn).toContain('ov-indeterminate')
      expect(turn).toContain('ov-spinner')
    }
  })
})
