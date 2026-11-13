// The Tab-to-fill follow-up suggestion (issue #28788) through the REAL
// ChatInput: the fill is recorded as a user edit, so one Ctrl/Cmd+Z clears it,
// including right after a switch between two chats whose drafts are both empty
// (the undo recorder's slot-settling window, keyed on autoFocusKey).
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

const OPTIONS = ['Open an issue', 'Skip it']

function ControlledInput({ slot }: { slot: string }) {
  const [v, setV] = React.useState('')
  return (
    <ChatInput {...defaultProps} value={v} onChange={setV} autoFocusKey={slot}
      followUpOptions={OPTIONS} onFollowUpSelect={() => {}} />
  )
}

describe('ChatInput follow-up Tab fill', () => {
  it('shows the first option as the placeholder and Tab writes it into the draft', () => {
    renderWithProviders(<ControlledInput slot="A" />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.placeholder).toContain('Open an issue')
    fireEvent.keyDown(ta, { key: 'Tab' })
    expect(ta.value).toBe('Open an issue')
  })

  it('undoes a Tab fill made right after switching between two empty drafts', () => {
    const { rerender } = renderWithProviders(<ControlledInput slot="A" />)
    rerender(<ControlledInput slot="B" />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.keyDown(ta, { key: 'Tab' })
    expect(ta.value).toBe('Open an issue')
    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(ta.value).toBe('')
  })
})
