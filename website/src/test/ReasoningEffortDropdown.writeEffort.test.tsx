/**
 * A host can route the effort write elsewhere: the crew window writes the
 * PEER's slot through the hub proxy, because its slot key names no session on
 * this gateway. Without the seam the pick would post to THIS gateway's route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

const makeStore = () => configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
})
let store: ReturnType<typeof makeStore>

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  store = makeStore()
  return Object.assign(
    render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>),
    { client: qc },
  )
}

let localWrite: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.restoreAllMocks()
  localWrite = vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)
})

describe('ReasoningEffortDropdown writeEffort', () => {
  it('writes a pick through the host seam, never this gateway\'s slot route', async () => {
    const writeEffort = vi.fn().mockResolvedValue({ reasoning_effort: '' })
    wrap(<ReasoningEffortDropdown slot="crew-window:x" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} writeEffort={writeEffort} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(writeEffort).toHaveBeenCalledWith(''))
    expect(localWrite).not.toHaveBeenCalled()
  })

  it('hands a refused pick to the host: onWriteError, no dashboard switch notice', async () => {
    const writeEffort = vi.fn().mockRejectedValue(new Error('peer refused'))
    const onWriteError = vi.fn()
    wrap(<ReasoningEffortDropdown slot="crew-window:x" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} writeEffort={writeEffort} onWriteError={onWriteError} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(writeEffort).toHaveBeenCalled())
    // The slider snaps back to the level still in force.
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Use default effort' })).not.toBeChecked())
    expect(onWriteError).toHaveBeenCalledWith(expect.stringContaining('peer refused'))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('keeps writing this gateway\'s slot route when no seam is given', async () => {
    wrap(<ReasoningEffortDropdown slot="chat-1" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))
    await waitFor(() => expect(localWrite).toHaveBeenCalledWith('chat-1', ''))
  })

  it('invalidates the slot capabilities after a successful effort write', async () => {
    const { client } = wrap(<ReasoningEffortDropdown slot="chat-1" currentEffort="high" onClose={vi.fn()} embedded levelsOverride={['low', 'high']} />)
    const capsKey = ['slot-selection-capabilities', 'chat-1']
    client.setQueryData(capsKey, { known: true, effort_supported: true, effort_levels: ['low', 'high'] })

    fireEvent.click(screen.getByRole('switch', { name: 'Use default effort' }))

    await waitFor(() => expect(localWrite).toHaveBeenCalledWith('chat-1', ''))
    await waitFor(() => expect(client.getQueryState(capsKey)?.isInvalidated).toBe(true))
  })
})
