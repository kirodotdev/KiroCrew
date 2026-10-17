import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { PREVIEW_STRIP_H, stubStripHeights } from './stripHeights'

// The attachment preview strip now renders for folder references too, but the
// composer's manual-height compensation used to key off `pendingFiles` alone.
// With a manually resized composer and only a folder staged, the strip appeared
// without the FILE_PREVIEW_H allowance, so it ate into the textarea instead of
// expanding the wrapper.

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

// The composer's own drag floor. The strip height is no longer a constant here:
// it is MEASURED off the rendered strip, which `stubStripHeights` gives a real
// box in jsdom. So these assertions read "drag floor + what the strip measured"
// rather than restating an arithmetic sum that lived in two files.
const INPUT_DRAG_MIN_H = 93
const MANUAL_H = 300

const outerOf = () => screen.getByLabelText('Message input').closest('.input-area') as HTMLElement

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
  // A persisted manual height is what activates the minHeight compensation.
  localStorage.setItem('mc-input-height', String(MANUAL_H))
})

describe('ChatInput preview-strip height compensation', () => {
  it('compensates for a dirs-only preview strip', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingDirs={['/repo/website/docs']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
  })

  it('compensates for a files-only preview strip (unchanged)', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.txt']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
  })

  // A measured height lands one commit AFTER the strip mounts, so the settling
  // 0 -> 81 must read as a BASELINE, not as "a strip just appeared". Without
  // that gate, opening a composer that already has something staged adds the
  // strip's height to the persisted manual height on every single mount, and it
  // compounds. Predicted constants never had this failure mode, so it is the
  // one measurement introduces and the one worth pinning.
  it('does not inflate the persisted height when a strip is present at mount', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.txt']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
    const wrapper = screen.getByTestId('input-wrapper')
    // The persisted height itself is untouched: only the floor moved.
    expect(wrapper.style.height).toBe(`${MANUAL_H}px`)
  })

  it('does not compensate when nothing is staged', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H}px`)
  })
})
