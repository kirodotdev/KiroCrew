/**
 * ErrorNotice `onStage`: a caller bound to THIS chat's remote workspace stages
 * the hand-off prompt in its own composer. Without it, the hand-off opens a
 * fresh chat exactly as before.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ErrorNotice from '../components/ErrorNotice'
import { askAgentPrompt } from '../components/AskAgentButton'
import { __resetNavSeamForTests, consumeChatHandoff, installSoftNavigate, setGatewayConnected } from '../utils/errorReport'
import '../i18n'

const ASK = /ask the agent/i

afterEach(() => { __resetNavSeamForTests() })

describe('ErrorNotice onStage', () => {
  for (const variant of ['block', 'inline'] as const) {
    it(`${variant}: without onStage, the hand-off still opens a fresh chat`, async () => {
      const navigate = vi.fn()
      installSoftNavigate(navigate)
      setGatewayConnected(true)
      render(<ErrorNotice variant={variant} message="Could not read the history" askAgent />)
      await userEvent.setup().click(screen.getByRole('button', { name: ASK }))
      expect(navigate).toHaveBeenCalled()
      expect(consumeChatHandoff()).toBe(askAgentPrompt({ message: 'Could not read the history' }))
    })

    it(`${variant}: with onStage, the same prompt is staged and nothing navigates`, async () => {
      const navigate = vi.fn()
      const onStage = vi.fn()
      installSoftNavigate(navigate)
      setGatewayConnected(true)
      render(<ErrorNotice variant={variant} message="Could not read the history" askAgent onStage={onStage} />)
      await userEvent.setup().click(screen.getByRole('button', { name: ASK }))
      expect(onStage).toHaveBeenCalledWith(askAgentPrompt({ message: 'Could not read the history' }))
      expect(navigate).not.toHaveBeenCalled()
      expect(consumeChatHandoff()).toBeNull()
    })
  }

  it('block, below: forwards onStage from the stacked hand-off too', async () => {
    const onStage = vi.fn()
    setGatewayConnected(true)
    render(<ErrorNotice message="Could not read the status" title="Read failed" askAgent actionPlacement="below" onStage={onStage} />)
    await userEvent.setup().click(screen.getByRole('button', { name: ASK }))
    expect(onStage).toHaveBeenCalledWith(askAgentPrompt({ message: 'Could not read the status' }))
  })
})
