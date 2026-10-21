/**
 * The empty chat page's Start a new chat button reports a failed create beside
 * itself: with no session open there is no pane notice to carry it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { UseMutationResult } from '@tanstack/react-query'
import type { ChatSlot } from '../types'

vi.mock('../store/chatSlice', () => ({
  createSlot: (opts: unknown) => ({ type: 'chat/createSlot', opts }),
}))

import ChatEmptyState from '../pages/chat/page/ChatEmptyState'

function renderEmpty(dispatch: (action: unknown) => { unwrap: () => Promise<unknown> }) {
  const mutation = { isPending: false, mutate: vi.fn() } as unknown as UseMutationResult<ChatSlot, Error, void, unknown>
  return render(
    <ChatEmptyState
      dispatch={dispatch as never} pendingAgent="" pendingAgentKindRef={{ current: undefined }} pendingModel=""
      newSessionRef={{ current: false }} newSlotFailed={false} newSlotMutation={mutation}
      setNewSlotFailed={vi.fn()} setSidError={vi.fn()}
    />,
  )
}

describe('ChatEmptyState create failure', () => {
  it('asks for the default folder and shows a failed create', async () => {
    const dispatch = vi.fn(() => ({ unwrap: () => Promise.reject(new Error('gateway down')) }))
    renderEmpty(dispatch)
    fireEvent.click(screen.getByRole('button', { name: 'Start a new chat' }))
    expect(dispatch.mock.calls[0][0]).toMatchObject({ opts: { defaultFolder: true } })
    expect(await screen.findByText('gateway down')).toBeTruthy()
    expect(screen.getByTestId('empty-state-create-error')).toBeTruthy()
  })

  it('shows nothing when the create succeeds', async () => {
    const dispatch = vi.fn(() => ({ unwrap: () => Promise.resolve({ key: 'NEW' }) }))
    renderEmpty(dispatch)
    fireEvent.click(screen.getByRole('button', { name: 'Start a new chat' }))
    await Promise.resolve()
    expect(screen.queryByTestId('empty-state-create-error')).toBeNull()
  })
})
