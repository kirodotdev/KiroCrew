// DocView selection-to-comment: the composer must attribute feedback to the
// document the passage was selected IN, even if the user switches tabs before
// submitting. Reading the live `tab` prop at submit time sent the agent a quote
// that does not appear in the file it was told to fix.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React, { useState } from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import DocView, { type Selection } from '../apps/spec-builder/components/DocView'
import type { SpecDetail } from '../apps/spec-builder/api'

const detail = {
  name: 'thing',
  working_dir: '/w',
  spec_dir: '/w/.kiro/specs/thing',
  spec_type: 'feature',
  status: 'planning',
  phase: 'design',
  running: false,
  files: {
    'requirements.md': 'The system SHALL do the requirements thing.',
    'design.md': 'The design thing is layered.',
    'tasks.md': null,
  },
  state: null,
  context: {},
} as unknown as SpecDetail

/** Fake a real text selection inside the document pane. */
function selectInsidePane(node: HTMLElement, text: string) {
  vi.spyOn(window, 'getSelection').mockReturnValue({
    toString: () => text,
    rangeCount: 1,
    getRangeAt: () => ({
      commonAncestorContainer: node,
      getBoundingClientRect: () => ({ left: 10, top: 10, width: 40, height: 12 }),
    }),
  } as unknown as Selection)
  // The listener lives on the scroll container; a mouseup on the rendered text
  // bubbles up to it, which is what a real selection does.
  act(() => { fireEvent.mouseUp(node) })
}

function Harness({ tab, addComment }: { tab: string; addComment: (c: { file: string; quote: string; note: string }) => void }) {
  const [sel, setSel] = useState<Selection | null>(null)
  const [note, setNote] = useState<Selection | null>(null)
  const [draft, setDraft] = useState('')
  return (
    <DocView
      detail={detail}
      tab={tab}
      addComment={addComment}
      composer={{ sel, setSel, note, setNote, draft, setDraft }}
    />
  )
}

describe('DocView selection-to-comment attribution', () => {  beforeEach(() => { vi.restoreAllMocks() })

  it('attributes the comment to the document the passage came from', async () => {
    const addComment = vi.fn()
    const { rerender } = render(
      <Harness tab="requirements" addComment={addComment} />
    )

    const passage = screen.getByText(/requirements thing/i)
    selectInsidePane(passage, 'the requirements thing')
    // Open the composer from the selection pill.
    const pill = await screen.findByRole('button', { name: /comment/i })
    act(() => { fireEvent.click(pill) })

    // User switches document while the composer is open.
    rerender(<Harness tab="design" addComment={addComment} />)

    const input = screen.getByLabelText(/Your feedback on the selected passage/i)
    act(() => { fireEvent.change(input, { target: { value: 'tighten this' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })

    expect(addComment).toHaveBeenCalledTimes(1)
    expect(addComment.mock.calls[0][0]).toMatchObject({
      file: 'requirements.md',
      note: 'tighten this',
    })
  })
})

/**
 * Triple-clicking the document's LAST paragraph must raise the Comment pill
 * (#7891).
 *
 * A multi-click selection of the last block normalizes to a boundary point just
 * PAST the scroll pane, so `range.commonAncestorContainer` is hoisted above it
 * and the pane's ancestor-containment early-return cleared the selection: the
 * review affordance never appeared for the last paragraph, while every other
 * paragraph worked. happy-dom performs no native multi-click selection, so these
 * tests build the normalized geometry from real ranges (rather than the faked
 * range the attribution suite above uses) and assert both sides of the
 * invariant.
 */
function selectNormalizedPastPane(passage: HTMLElement, opts: { endAfter?: HTMLElement } = {}) {
  const pane = passage.closest('.overflow-y-auto') as HTMLElement
  const parent = pane.parentElement!
  const childIndex = (node: Node) => Array.from(parent.childNodes).indexOf(node as ChildNode)
  const range = document.createRange()
  // The near boundary stays inside the pane; only the far one is normalized out.
  range.setStart(passage.firstChild!, 0)
  range.setEnd(parent, childIndex(opts.endAfter ?? pane) + 1)

  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  act(() => { fireEvent.mouseUp(passage) })
  return pane
}

describe('DocView multi-click selection of the last paragraph', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    if (!Range.prototype.getBoundingClientRect) {
      Range.prototype.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
    }
  })

  it('raises the Comment pill when the selection end is normalized past the pane', async () => {
    render(<Harness tab="requirements" addComment={vi.fn()} />)

    selectNormalizedPastPane(screen.getByText(/requirements thing/i))

    expect(await screen.findByRole('button', { name: /comment/i })).toBeInTheDocument()
  })

  it('keeps dismissing a selection that genuinely runs past the pane', () => {
    render(<Harness tab="requirements" addComment={vi.fn()} />)

    const passage = screen.getByText(/requirements thing/i)
    // A text-bearing neighbour after the pane, so the overhang is not merely the
    // pane's closing boundary. Accepting it would quote text from outside the
    // document the comment is attributed to.
    const pane = passage.closest('.overflow-y-auto') as HTMLElement
    const neighbour = document.createElement('div')
    neighbour.textContent = 'text from outside the document'
    pane.parentElement!.appendChild(neighbour)

    selectNormalizedPastPane(passage, { endAfter: neighbour })

    expect(screen.queryByRole('button', { name: /comment/i })).not.toBeInTheDocument()
    neighbour.remove()
  })
})
