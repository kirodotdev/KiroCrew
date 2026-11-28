// The close chip's arm is scoped to the session through the REAL ChatInput,
// which passes its own slot id to FollowUpBar as `actionScope`.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'
import type { OfferedAction } from '../app-sdk/protocol/optionActionTag'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

const CLOSE: OfferedAction = { label: 'Nothing else, close this tab' }

function Pane({ slot, onAction }: { slot: string; onAction: () => void }) {
  return (
    <SlotProvider slotId={slot}>
      <ChatInput {...defaultProps} value="" onChange={() => {}}
        followUpOptions={[]} onFollowUpSelect={() => {}} followUpSourceKey="row-1"
        followUpAction={CLOSE} onFollowUpAction={onAction} followUpActionNeedsSecondClick={() => true} />
    </SlotProvider>
  )
}

const actionChip = () => screen.getByRole('button', { name: new RegExp(CLOSE.label, 'i') })

describe('ChatInput close chip arm scope', () => {
  it('drops the arm on a session switch, even when both replies share a sourceKey', () => {
    const onAction = vi.fn()
    const { rerender } = renderWithProviders(<Pane slot="slot-a" onAction={onAction} />)
    fireEvent.click(actionChip())
    expect(actionChip()).toHaveAttribute('data-armed')
    rerender(<Pane slot="slot-b" onAction={onAction} />)
    expect(actionChip()).not.toHaveAttribute('data-armed')
    fireEvent.click(actionChip())
    expect(onAction).not.toHaveBeenCalled()
  })
})
