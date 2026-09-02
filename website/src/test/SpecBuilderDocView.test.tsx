// DocView selection-to-comment: the composer must attribute feedback to the
// document the passage was selected IN, even if the user switches tabs before
// submitting. Reading the live `tab` prop at submit time sent the agent a quote
// that does not appear in the file it was told to fix.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import DocView from '../apps/spec-builder/components/DocView'
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

describe('DocView selection-to-comment attribution', () => {  beforeEach(() => { vi.restoreAllMocks() })

  it('attributes the comment to the document the passage came from', async () => {
    const addComment = vi.fn()
    const { rerender } = render(
      <DocView detail={detail} tab="requirements" addComment={addComment} />
    )

    const passage = screen.getByText(/requirements thing/i)
    selectInsidePane(passage, 'the requirements thing')
    // Open the composer from the selection pill.
    const pill = await screen.findByRole('button', { name: /comment/i })
    act(() => { fireEvent.click(pill) })

    // User switches document while the composer is open.
    rerender(<DocView detail={detail} tab="design" addComment={addComment} />)

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
