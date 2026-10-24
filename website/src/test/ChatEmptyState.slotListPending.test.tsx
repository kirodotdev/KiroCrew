/**
 * The chat pane with no session open must not claim "you have no chats" while
 * the first slot list is still on its way (#21785). It shows a neutral loading
 * placeholder until a list lands, and falls back to the empty state once a list
 * lands empty or every fetch in flight has failed.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import type { UseMutationResult } from '@tanstack/react-query'
import type { ChatSlot } from '../types'
import dashboardReducer, { fetchSlots, sseConnected, sseSlots } from '../store/dashboardSlice'

vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...await importOriginal<typeof import('../store/chatSlice')>(),
  createSlot: (opts: unknown) => ({ type: 'chat/createSlot', opts }),
}))

import ChatEmptyState from '../pages/chat/page/ChatEmptyState'

const START = 'Start a new chat'
const slot = { key: 'slot-a', messages: 0, running: false } as unknown as ChatSlot

function renderPane(newSlotFailed = false) {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  const mutation = { isPending: false, mutate: vi.fn() } as unknown as UseMutationResult<ChatSlot, Error, void, unknown>
  render(
    <Provider store={store}>
      <ChatEmptyState
        dispatch={vi.fn() as never} pendingAgent="" pendingAgentKindRef={{ current: undefined }} pendingModel=""
        newSessionRef={{ current: false }} newSlotFailed={newSlotFailed} newSlotMutation={mutation}
        setNewSlotFailed={vi.fn()} setSidError={vi.fn()}
      />
    </Provider>,
  )
  return store
}

const loading = () => screen.queryByTestId('chat-pane-loading')
const startButton = () => screen.queryByRole('button', { name: START })

describe('ChatEmptyState before the first slot list lands', () => {
  it('shows a loading placeholder, not the start-a-new-chat empty state', () => {
    const store = renderPane()
    act(() => { store.dispatch(fetchSlots.pending('r1', undefined)) })
    expect(loading()).toBeTruthy()
    expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true')
    expect(startButton()).toBeNull()
    expect(screen.queryByText('What can I do for you?')).toBeNull()
  })

  it('shows the empty state once a gateway with no sessions returns its empty list', () => {
    const store = renderPane()
    act(() => { store.dispatch(fetchSlots.pending('r1', undefined)) })
    act(() => { store.dispatch(fetchSlots.fulfilled([], 'r1', undefined)) })
    expect(loading()).toBeNull()
    expect(startButton()).toBeTruthy()
  })

  it('falls back to the empty state when the slot fetch fails', () => {
    const store = renderPane()
    act(() => { store.dispatch(fetchSlots.pending('r1', undefined)) })
    act(() => { store.dispatch(fetchSlots.rejected(new Error('gateway down'), 'r1', undefined)) })
    expect(loading()).toBeNull()
    expect(startButton()).toBeTruthy()
  })

  it('keeps waiting while another fetch is still in flight after one fails', () => {
    const store = renderPane()
    act(() => {
      store.dispatch(fetchSlots.pending('r1', undefined))
      store.dispatch(fetchSlots.pending('r2', undefined))
      store.dispatch(fetchSlots.rejected(new Error('gateway down'), 'r1', undefined))
    })
    expect(loading()).toBeTruthy()
    act(() => { store.dispatch(fetchSlots.rejected(new Error('gateway down'), 'r2', undefined)) })
    expect(loading()).toBeNull()
    expect(startButton()).toBeTruthy()
  })

  it('waits again while a retry fetch is in flight after the first one failed', () => {
    const store = renderPane()
    act(() => {
      store.dispatch(fetchSlots.pending('r1', undefined))
      store.dispatch(fetchSlots.rejected(new Error('gateway down'), 'r1', undefined))
    })
    expect(startButton()).toBeTruthy()
    // The reconnect catch-up dispatches a fresh fetch.
    act(() => { store.dispatch(fetchSlots.pending('r2', undefined)) })
    expect(loading()).toBeTruthy()
    expect(startButton()).toBeNull()
    act(() => { store.dispatch(fetchSlots.fulfilled([slot], 'r2', undefined)) })
    expect(loading()).toBeNull()
  })

  it('does not wait again on a socket reconnect once a list has landed', () => {
    const store = renderPane()
    act(() => { store.dispatch(sseSlots([slot])) })
    act(() => { store.dispatch(sseSlots([])) })
    // A reconnect clears `slotsLoaded`; the empty list already landed, so
    // the pane keeps its empty state instead of spinning until the next frame.
    act(() => { store.dispatch(sseConnected()) })
    expect(store.getState().dashboard.slotsLoaded).toBe(false)
    expect(loading()).toBeNull()
    expect(startButton()).toBeTruthy()
  })

  it('still offers the retry button after a failed create', () => {
    renderPane(true)
    expect(loading()).toBeNull()
    expect(startButton()).toBeTruthy()
  })
})
