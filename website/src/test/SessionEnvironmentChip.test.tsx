import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import SessionEnvironmentChip from '../components/SessionEnvironmentChip'
import { START_POLL_MS, slotEnvironmentQueryKey, useEnvironmentStartLabel } from '../components/sessionEnvironmentHooks'
import { api, type SlotEnvironment } from '../api/client'
import { ApiError } from '../api/apiError'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import '../i18n'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return { store, qc, ...render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>) }
}

const DEV = { id: 'dev', name: 'Coder MicroVM', provider: 'coder', workdir: '/workspace/crew-session' }

function envResponse(profile: string | null, profiles = [DEV]): SlotEnvironment {
  return { profile, profiles }
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('SessionEnvironmentChip', () => {
  it('renders nothing when no profile is configured', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null, []))
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('chat-1'))
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
  })

  it('renders nothing when the endpoint refuses (an app caller is not the owner)', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(new ApiError(403, 'owner_only', '{"code":"owner_only"}'))
    const { qc } = wrap(<SessionEnvironmentChip slot="chat-1" />)
    await waitFor(() => expect(qc.getQueryState(slotEnvironmentQueryKey('chat-1'))?.status).toBe('error'))
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    new Error('environment read failed'),
    new ApiError(403, 'sign in again', '{"code":"auth_required"}'),
  ])('surfaces real environment read failures without discarding a composer draft: %s', async (error) => {
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(error)
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(error.message)
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('surfaces a failed refresh even when an old environment is cached', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev'))
    const { qc } = wrap(<SessionEnvironmentChip slot="chat-1" />)
    await screen.findByTestId('composer-environment-chip')
    get.mockRejectedValue(new Error('refresh failed'))
    await act(async () => { await qc.invalidateQueries({ queryKey: slotEnvironmentQueryKey('chat-1') }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('refresh failed')
  })

  it('shows where the session runs and binds it to a profile', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: 'dev', applies: 'next_start' })
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on This machine' })
    await u.click(chip)
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    expect(put).toHaveBeenCalledWith('chat-1', 'dev')
    expect(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' })).toBeInTheDocument()
  })

  it("shows each profile's provider mark, and the chosen one on the chip", async () => {
    const old = { id: 'old', name: 'Older gateway', provider: '', workdir: '/w' }
    const fly = { id: 'fly', name: 'Fly box', provider: 'sprites', workdir: '/w' }
    const box = { id: 'bx', name: 'Boxd box', provider: 'boxd', workdir: '/w' }
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev', [DEV, old, fly, box]))
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on Coder MicroVM' })
    expect(chip.querySelector('[data-testid="provider-mark-coder"]')).not.toBeNull()
    await u.click(chip)
    const item = (name: RegExp) => screen.getByRole('menuitemradio', { name })
    await screen.findByRole('menuitemradio', { name: /Older gateway/ })
    expect(item(/Coder MicroVM/).querySelector('[data-testid="provider-mark-coder"]')).not.toBeNull()
    expect(item(/Fly box/).querySelector('[data-testid="provider-mark-sprites"]')).not.toBeNull()
    expect(item(/Boxd box/).querySelector('[data-testid="provider-mark-boxd"]')).not.toBeNull()
    // An unknown provider keeps the generic cloud rather than guessing a brand.
    expect(item(/Older gateway/).querySelector('[data-testid^="provider-mark-"]')).toBeNull()
  })

  it('unbinds back to this machine with null, and re-picking the current one sends nothing', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev'))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: null, applies: 'next_start' })
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await u.click(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    expect(put).not.toHaveBeenCalled()
    await u.click(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /This machine/ }))
    expect(put).toHaveBeenCalledWith('chat-1', null)
  })

  it('re-reads the profiles when the menu opens', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev'))
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on Coder MicroVM' })
    const calls = get.mock.calls.length
    await u.click(chip)
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls))
  })

  it('is disabled while a turn runs, and says why', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    const chip = await screen.findByRole('button', { name: 'Stop the current response to switch environments' })
    expect(chip).toBeDisabled()
  })

  it('shows the start stage and elapsed time while a remote start runs, then the name again', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 30, elapsed_s: 42 },
    })
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    const chip = await screen.findByTestId('composer-environment-chip')
    // The stage only: the running step in the transcript carries the one timer.
    await waitFor(() => expect(chip).toHaveTextContent(/^Starting session environment$/))
    expect(chip).toHaveAccessibleName(/Starting Coder MicroVM: Starting session environment \(3\d/)
    // The stage alone is announced.
    expect(screen.getByRole('status')).toHaveTextContent('Starting session environment')
    // Polls while starting.
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1), { timeout: 2500 })
    // The backend clears the stage and the runtime attaches; the next poll reverts.
    get.mockResolvedValue({ ...envResponse('dev'), starting: null, attached: true })
    await waitFor(() => expect(screen.getByTestId('composer-environment-chip')).toHaveTextContent('Coder MicroVM'), { timeout: 2500 })
  })

  it('shows setup, not nothing, before the first stage is reported', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), starting: null, attached: false })
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    await waitFor(() => expect(screen.getByTestId('composer-environment-chip')).toHaveTextContent('Checking environment'))
  })

  it('never shows a start stage for a local session, and does not poll', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse(null),
      starting: { stage: 'provisioning', stage_elapsed_s: 1, elapsed_s: 1 },
    })
    vi.useFakeTimers()
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('composer-environment-chip')).toHaveTextContent('This machine')
    await act(async () => { await vi.advanceTimersByTimeAsync(START_POLL_MS * 3) })
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('is a fixed status, not a picker, once the session has started', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), locked: true })
    const put = vi.spyOn(api, 'setChatSlotEnvironment')
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByTestId('composer-environment-chip')
    expect(chip).toHaveTextContent('Coder MicroVM')
    expect(chip).toHaveAccessibleName('Runs on Coder MicroVM. Fixed for this session.')
    expect(chip.tagName).toBe('SPAN')
    expect(screen.queryByRole('button', { name: /Coder MicroVM/ })).toBeNull()
    await u.click(chip)
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(put).not.toHaveBeenCalled()
  })

  it('reports a failed switch and keeps the last confirmed environment', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    vi.spyOn(api, 'setChatSlotEnvironment').mockRejectedValue(new Error('choices unreadable'))
    const u = userEvent.setup()
    const { store } = wrap(<SessionEnvironmentChip slot="chat-1" />)
    await u.click(await screen.findByRole('button', { name: 'Runs on This machine' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toBeTruthy())
    expect(await screen.findByRole('button', { name: 'Runs on This machine' })).toBeInTheDocument()
  })
})

describe('useEnvironmentStartLabel steps', () => {
  function Probe() {
    const { data } = useQuery({
      queryKey: slotEnvironmentQueryKey('chat-1'),
      queryFn: () => api.chatSlotEnvironment('chat-1'),
    })
    return <span>{`probe:${data?.starting?.stage ?? ''}`}</span>
  }

  function Steps({ connectedBefore = false }: { connectedBefore?: boolean }) {
    const start = useEnvironmentStartLabel('chat-1', true, connectedBefore)
    return <ul data-testid="steps">{start?.steps.map(step => <li key={step.key}>{`${step.key}:${step.state}`}</li>)}</ul>
  }

  it('marks every stage before the current one done, run or not needed', async () => {
    // A warm pool start never enters provisioning (the workspace is already up)
    // and connects in under one poll, so the first read says starting_agent.
    // Both earlier stages still achieved their outcome: each shows done.
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 3,
        entered: ['checking', 'connecting', 'starting_agent'],
      },
    })
    wrap(<Steps />)
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    for (const key of ['checking', 'provisioning', 'connecting']) {
      expect(screen.getByText(`${key}:done`)).toBeInTheDocument()
    }
  })

  it('shows no setup for an active session reconnecting to a running workspace', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      attached: true,
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 2,
        entered: ['checking', 'connecting', 'starting_agent'],
      },
    })
    // A first-connection twin shares the query, so once IT shows steps the read
    // has landed, and the reconnecting session's empty list is a real verdict.
    wrap(<><Steps connectedBefore /><div data-testid="first"><Steps /></div></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    const [reconnecting] = screen.getAllByTestId('steps')
    expect(reconnecting).toBeEmptyDOMElement()
  })

  it('takes the gateway at its word when it says the start reconnects', async () => {
    // The composer chip has no transcript to consult; the gateway's flag is what
    // keeps it from saying "Starting agent" while the transcript says "Thinking…".
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      attached: true,
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 2,
        entered: ['checking', 'connecting', 'starting_agent'], reconnect: true,
      },
    })
    wrap(<><Steps /><Probe /></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    // The probe reads the same query: once it shows the stage, the hook has the
    // response too, so an empty list here is a real verdict, not an early read.
    expect(await screen.findByText('probe:starting_agent')).toBeInTheDocument()
    expect(screen.getByTestId('steps')).toBeEmptyDOMElement()
  })

  it('still shows setup when a reconnect has to start the workspace again', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 4, elapsed_s: 5, entered: ['checking', 'provisioning'] },
    })
    wrap(<Steps connectedBefore />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
  })

  it('shows setup on a first connection even before the gateway reports a stage', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), attached: false })
    wrap(<Steps />)
    expect(await screen.findByText('checking:current')).toBeInTheDocument()
  })

  it('marks later stages pending while an earlier one runs', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 9, elapsed_s: 10 },
    })
    wrap(<Steps />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
    expect(screen.getByText('checking:done')).toBeInTheDocument()
    expect(screen.getByText('connecting:pending')).toBeInTheDocument()
    expect(screen.getByText('starting_agent:pending')).toBeInTheDocument()
  })
})
