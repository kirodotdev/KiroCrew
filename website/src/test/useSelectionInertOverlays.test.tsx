import { describe, it, expect, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSelectionInertOverlays, SELECTION_INERT_ATTR } from '../pages/chat/useSelectionInertOverlays'

/** Parse fixture markup into nodes (the suite's no-innerHTML rule). */
const nodes = (markup: string) => document.createRange().createContextualFragment(markup)

/**
 * On touch, the transcript's overlays turn inert while a selection is held in
 * the transcript, so a handle dragged under them hit-tests the transcript
 * instead of the composer's draft. Desktop is untouched.
 */
const realMatchMedia = window.matchMedia
const stubTouch = (touch: boolean) => {
  window.matchMedia = ((q: string) => ({ matches: touch && (q === '(pointer: coarse)' || q === '(hover: none)'), media: q, addEventListener: () => {}, removeEventListener: () => {}, onchange: null, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false })) as typeof window.matchMedia
}

function setup() {
  document.body.replaceChildren(nodes(`<div id="sc"><p id="row">some transcript text</p></div><div id="dock" ${SELECTION_INERT_ATTR}><span id="draft">draft</span></div>`))
  return { scroller: document.getElementById('sc')!, dock: document.getElementById('dock')! }
}
function selectIn(id: string, from: number, to: number) {
  const node = document.getElementById(id)!.firstChild!
  const r = document.createRange(); r.setStart(node, from); r.setEnd(node, to)
  const s = document.getSelection()!; s.removeAllRanges(); s.addRange(r)
  document.dispatchEvent(new Event('selectionchange'))
}

describe('useSelectionInertOverlays', () => {
  afterEach(() => { window.matchMedia = realMatchMedia; document.getSelection()?.removeAllRanges(); document.body.replaceChildren() })

  it('makes overlays inert while a transcript selection is held on touch, and restores them', () => {
    stubTouch(true)
    const { scroller, dock } = setup()
    renderHook(() => useSelectionInertOverlays({ current: scroller }))
    act(() => selectIn('row', 0, 4))
    expect(dock.inert).toBe(true)
    act(() => { document.getSelection()!.removeAllRanges(); document.dispatchEvent(new Event('selectionchange')) })
    expect(dock.inert).toBe(false)
  })

  it('stays inert while only the focus is left in the transcript', () => {
    stubTouch(true)
    const { scroller, dock } = setup()
    renderHook(() => useSelectionInertOverlays({ current: scroller }))
    act(() => {
      const s = document.getSelection()!
      s.setBaseAndExtent(document.getElementById('draft')!.firstChild!, 2, document.getElementById('row')!.firstChild!, 4)
      document.dispatchEvent(new Event('selectionchange'))
    })
    expect(dock.inert).toBe(true)
  })

  it('releases the overlays on a finger touch, so the first tap reaches them', () => {
    stubTouch(true)
    const { scroller, dock } = setup()
    renderHook(() => useSelectionInertOverlays({ current: scroller }))
    act(() => selectIn('row', 0, 4))
    expect(dock.inert).toBe(true)
    act(() => { document.dispatchEvent(new Event('touchstart')) })
    expect(dock.inert).toBe(false)
    // A handle drag (no touch events) moves the selection and re-applies it.
    act(() => selectIn('row', 0, 6))
    expect(dock.inert).toBe(true)
  })

  it('leaves overlays alone for a selection outside the transcript', () => {
    stubTouch(true)
    const { scroller, dock } = setup()
    renderHook(() => useSelectionInertOverlays({ current: scroller }))
    act(() => selectIn('draft', 0, 3))
    expect(dock.inert).toBe(false)
  })

  it('does nothing on desktop', () => {
    stubTouch(false)
    const { scroller, dock } = setup()
    renderHook(() => useSelectionInertOverlays({ current: scroller }))
    act(() => selectIn('row', 0, 4))
    expect(dock.inert).toBe(false)
  })
})
