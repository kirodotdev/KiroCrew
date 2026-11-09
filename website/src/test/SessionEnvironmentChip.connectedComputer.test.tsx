import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import SessionEnvironmentChip from '../components/SessionEnvironmentChip'
import { api, type SessionEnvironmentProfile, type SlotEnvironment } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import '../i18n'

// Issue 29949: a chat bound to one of the user's own computers. The chip names
// that computer, says when it is offline, and never offers to move the chat to
// another machine once it is fixed (B10, B12).

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

const computer = (over: Partial<SessionEnvironmentProfile>): SessionEnvironmentProfile => ({
  id: 'laptop', name: 'laptop', provider: 'connected_computer', workdir: '/home/u/proj',
  connected: true, label_is_shared: false, ...over,
})
const mockEnv = (value: SlotEnvironment) => vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(value)
const chip = (disabled = false) => wrap(<SessionEnvironmentChip slot="chat-1" disabled={disabled} />)

beforeEach(() => { vi.restoreAllMocks() })
afterEach(cleanup)

describe('SessionEnvironmentChip: connected computers', () => {
  it('B09/B10: the picker marks an offline computer and tells same-named ones apart by id', async () => {
    mockEnv({ profile: null, profiles: [
      computer({}),
      computer({ id: 'laptop-2', connected: false, workdir: '', label_is_shared: true }),
    ] })
    chip()
    await userEvent.click(await screen.findByTestId('composer-environment-chip'))
    const offline = await screen.findByRole('menuitemradio', { name: /laptop.*ID laptop-2.*Offline/ })
    expect(offline).toBeTruthy()
    expect(screen.getByRole('menuitemradio', { name: /\/home\/u\/proj/ })).toBeTruthy()
  })

  it('B10: a fixed chat on an offline computer says so and offers no other machine', async () => {
    const health = vi.spyOn(api, 'chatSlotEnvironmentHealth')
    mockEnv({ profile: 'laptop', provider: 'connected_computer', locked: true, profiles: [computer({ connected: false, workdir: '' })] })
    chip()
    expect(await screen.findByText(/laptop is offline/)).toBeTruthy()
    await userEvent.click(screen.getByTestId('composer-environment-chip'))
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    await waitFor(() => expect(health).not.toHaveBeenCalled())
  })

  it('B12: a fixed chat whose computer was removed stays bound and says it cannot run', async () => {
    mockEnv({ profile: 'laptop', provider: 'connected_computer', locked: true, profiles: [] })
    chip()
    expect(await screen.findByText(/no longer connected to this gateway/)).toBeTruthy()
    expect(screen.queryByText('This machine')).toBeNull()
  })

  it('an online fixed computer shows no offline note', async () => {
    mockEnv({ profile: 'laptop', provider: 'connected_computer', locked: true, profiles: [computer({})] })
    chip()
    expect(await screen.findByTestId('composer-environment-chip')).toHaveAccessibleName(/laptop/)
    expect(screen.queryByText(/is offline/)).toBeNull()
  })
})
