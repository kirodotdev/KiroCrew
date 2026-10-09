import { describe, expect, it } from 'vitest'
import {
  nextRetainedRange,
  restoreEndpointToTranscript,
  rowEndpoints,
  selectedRowRange,
  selectionTouchesContainer,
} from '../utils/selectionRetention'

/** Parse fixture markup into nodes (the suite's no-innerHTML rule). */
const nodes = (markup: string) => document.createRange().createContextualFragment(markup)

function selection(anchorNode: Node, focusNode: Node): Selection {
  // DOM Ranges normalize start/end and so cannot model a backward mobile-handle
  // selection. The helper only reads these public Selection fields.
  return {
    anchorNode,
    anchorOffset: 0,
    focusNode,
    focusOffset: 0,
    isCollapsed: false,
    setBaseAndExtent(anchor, anchorOffset, focus, focusOffset) {
      this.anchorNode = anchor
      this.anchorOffset = anchorOffset
      this.focusNode = focus
      this.focusOffset = focusOffset
    },
  } as Selection
}

describe('selectionRetention', () => {
  it('keeps every transcript row between reverse selection endpoints', () => {
    const container = document.createElement('div')
    container.replaceChildren(nodes('<div data-display-index="4">first</div><div data-display-index="5">second</div><div data-display-index="6">third</div>'))
    document.body.append(container)
    const rows = container.querySelectorAll('[data-display-index]')

    const selected = selection(rows[2].firstChild!, rows[0].firstChild!)

    expect(selectedRowRange(container, selected)).toEqual({ start: 4, end: 7 })
    expect(selectionTouchesContainer(container, selected)).toBe(true)
  })

  it('does not treat a document-spanning selection as a transcript range', () => {
    const container = document.createElement('div')
    container.replaceChildren(nodes('<div data-display-index="4">chat text</div>'))
    const outside = document.createElement('p')
    outside.textContent = 'dashboard chrome'
    document.body.append(container, outside)

    const selected = selection(container.firstChild!.firstChild!, outside.firstChild!)

    expect(selectedRowRange(container, selected)).toBeNull()
    expect(selectionTouchesContainer(container, selected)).toBe(true)
  })

  describe('nextRetainedRange', () => {
    // A handle over the scroller's padding under the composer sits inside the
    // transcript but on no row. Releasing there let the start row unmount and
    // re-rooted the selection at the top of the transcript.
    const build = () => {
      document.body.replaceChildren(nodes('<div id="sc"><div data-display-index="4"><p id="r4">start row</p></div><div id="pad"></div></div><p id="out">outside</p>'))
      return document.getElementById('sc') as HTMLElement
    }
    const text = (id: string) => document.getElementById(id)!.firstChild ?? document.getElementById(id)!

    it('keeps the last retained span while an endpoint sits on no row inside the transcript', () => {
      const sc = build()
      expect(nextRetainedRange(sc, selection(text('r4'), document.getElementById('pad')!))).toBe('keep')
    })
    it('replaces the span when both endpoints sit on rows', () => {
      const sc = build()
      expect(nextRetainedRange(sc, selection(text('r4'), text('r4')))).toEqual({ start: 4, end: 5 })
    })
    // A fresh Android long-press can report an endpoint outside the rows; the
    // selection itself must come through untouched.
    it('never rewrites the selection', () => {
      const sc = build()
      const selected = selection(text('r4'), text('out'))
      selected.setBaseAndExtent = () => { throw new Error('selection rewritten') }
      expect(nextRetainedRange(sc, selected)).toBe('keep')
      expect(selected.focusNode).toBe(text('out'))
    })
    it('releases when the selection collapses or leaves the transcript', () => {
      const sc = build()
      expect(nextRetainedRange(sc, null)).toBeNull()
      expect(nextRetainedRange(sc, { ...selection(text('r4'), text('r4')), isCollapsed: true } as Selection)).toBeNull()
      expect(nextRetainedRange(sc, selection(text('out'), text('out')))).toBeNull()
    })
  })

  describe('restoreEndpointToTranscript', () => {
    const build = () => {
      document.body.replaceChildren(nodes('<p id="title">title</p><div id="sc"><div data-display-index="4"><p id="a">start row text</p></div><div data-display-index="5"><p id="b">end row text</p></div></div>'))
      return document.getElementById('sc') as HTMLElement
    }
    const t = (id: string) => document.getElementById(id)!.firstChild!

    it('puts a start handle that landed on the title back where it was', () => {
      const sc = build()
      const settled = selection(t('a'), t('b'))
      settled.anchorOffset = 6
      settled.focusOffset = 3
      const last = rowEndpoints(sc, settled)
      const moved = selection(t('title'), t('b'))
      moved.anchorOffset = 2
      moved.focusOffset = 7
      expect(restoreEndpointToTranscript(sc, moved, last)).toBe(true)
      expect(moved.anchorNode).toBe(t('a'))
      expect(moved.anchorOffset).toBe(6)
      expect(moved.focusOffset).toBe(7)
    })
    it('puts a start parked between rows back at its character in a re-rendered row', () => {
      const sc = build()
      const settled = selection(t('a'), t('b'))
      settled.anchorOffset = 6
      const last = rowEndpoints(sc, settled)
      // The start row re-renders: same text, new nodes.
      const row = sc.querySelector('[data-display-index="4"]')!
      row.replaceChildren(nodes('<p>start </p><p>row text</p>'))
      const drifted = selection(sc, t('b'))
      drifted.anchorOffset = 0
      drifted.focusOffset = 9
      expect(restoreEndpointToTranscript(sc, drifted, last)).toBe(true)
      expect(drifted.anchorNode).toBe(row.firstChild!.firstChild)
      expect(drifted.anchorOffset).toBe(6)
      expect(drifted.focusOffset).toBe(9)
    })
    it('leaves a fresh long-press alone', () => {
      const sc = build()
      const fresh = selection(t('a'), t('title'))
      expect(restoreEndpointToTranscript(sc, fresh, null)).toBe(false)
      expect(fresh.focusNode).toBe(t('title'))
    })
    it('leaves a selection wholly inside or outside the transcript alone', () => {
      const sc = build()
      const last = rowEndpoints(sc, selection(t('a'), t('b')))
      expect(restoreEndpointToTranscript(sc, selection(t('a'), t('b')), last)).toBe(false)
      expect(restoreEndpointToTranscript(sc, selection(t('title'), t('title')), last)).toBe(false)
      // A dragged END over the scroller's padding is the reader's, not drift.
      expect(restoreEndpointToTranscript(sc, selection(t('a'), sc), last)).toBe(false)
    })
  })
})
