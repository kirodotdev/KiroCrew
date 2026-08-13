import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

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

// Must match ChatInput's own constants.
const INPUT_DRAG_MIN_H = 93
const FILE_PREVIEW_H = 81
const MANUAL_H = 300

const outerOf = () => screen.getByLabelText('Message input').closest('.input-area') as HTMLElement

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  // A persisted manual height is what activates the minHeight compensation.
  localStorage.setItem('mc-input-height', String(MANUAL_H))
})

describe('ChatInput preview-strip height compensation', () => {
  it('compensates for a dirs-only preview strip', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingDirs={['/repo/website/docs']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + FILE_PREVIEW_H}px`)
  })

  it('compensates for a files-only preview strip (unchanged)', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.txt']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + FILE_PREVIEW_H}px`)
  })

  it('does not compensate when nothing is staged', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H}px`)
  })
})
