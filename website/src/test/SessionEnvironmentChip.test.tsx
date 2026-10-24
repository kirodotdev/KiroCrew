import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import SessionEnvironmentChip from '../components/SessionEnvironmentChip'
import { START_POLL_MS, slotEnvironmentQueryKey, useEnvironmentStartLabel, useSlotRunsHere } from '../components/sessionEnvironmentHooks'
import { api, type SessionEnvironmentStartStage, type SlotEnvironment } from '../api/client'
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
const STAGES: SessionEnvironmentStartStage[] = ['checking', 'connecting', 'starting_agent']

function envResponse(profile: string | null, profiles = [DEV]): SlotEnvironment {
  return { profile, profiles }
}
const mockEnv = (value: SlotEnvironment) => vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(value)
/** A remote ("dev") session, with these fields over the base response. */
const devEnv = (extra: Partial<SlotEnvironment> = {}) => mockEnv({ ...envResponse('dev'), ...extra })
const chip = (disabled = false) => wrap(<SessionEnvironmentChip slot="chat-1" disabled={disabled} />)
const chipEl = () => screen.getByTestId('composer-environment-chip')
const button = (name: string) => screen.findByRole('button', { name })
const radio = (name: RegExp) => screen.findByRole('menuitemradio', { name })

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('SessionEnvironmentChip', () => {
  it('renders nothing when no profile is configured', async () => {
    const get = mockEnv(envResponse(null, []))
    chip()
    await waitFor(() => expect(get).toHaveBeenCalledWith('chat-1'))
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
  })

  it('renders nothing when the endpoint refuses (an app caller is not the owner)', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(new ApiError(403, 'owner_only', '{"code":"owner_only"}'))
    const { qc } = chip()
    await waitFor(() => expect(qc.getQueryState(slotEnvironmentQueryKey('chat-1'))?.status).toBe('error'))
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    new Error('environment read failed'),
    new ApiError(403, 'sign in again', '{"code":"auth_required"}'),
  ])('surfaces real environment read failures without discarding a composer draft: %s', async (error) => {
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(error)
    chip()
    expect(await screen.findByRole('alert')).toHaveTextContent(error.message)
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('surfaces a failed refresh even when an old environment is cached', async () => {
    const get = devEnv()
    const { qc } = chip()
    await screen.findByTestId('composer-environment-chip')
    get.mockRejectedValue(new Error('refresh failed'))
    await act(async () => { await qc.invalidateQueries({ queryKey: slotEnvironmentQueryKey('chat-1') }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('refresh failed')
  })

  it('shows where the session runs and binds it to a profile', async () => {
    let current: string | null = null
    vi.spyOn(api, 'chatSlotEnvironment').mockImplementation(async () => envResponse(current))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockImplementation(async (_slot, profile) => {
      current = profile
      return { profile, applies: 'next_start' }
    })
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    expect(put).toHaveBeenCalledWith('chat-1', 'dev')
    expect(await button('Runs on Coder MicroVM')).toBeInTheDocument()
  })

  it('keeps a successful switch when an older placement read lands after it', async () => {
    let current: string | null = null
    let release!: () => void
    const held = new Promise<void>((resolve) => { release = resolve })
    let calls = 0
    vi.spyOn(api, 'chatSlotEnvironment').mockImplementation(async () => {
      calls += 1
      // The read the menu's opening starts: it read "this machine" before the switch.
      if (calls === 2) { await held; return envResponse(null) }
      return envResponse(current)
    })
    vi.spyOn(api, 'setChatSlotEnvironment').mockImplementation(async (_slot, profile) => {
      current = profile
      return { profile, applies: 'next_start' }
    })
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on This machine'))
    await waitFor(() => expect(calls).toBe(2))
    await u.click(await radio(/Coder MicroVM/))
    expect(await button('Runs on Coder MicroVM')).toBeInTheDocument()
    await act(async () => { release() })
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(3))
    expect(await button('Runs on Coder MicroVM')).toBeInTheDocument()
  })

  it('takes no second pick while a switch is pending, so the last pick is the one sent', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockImplementation(async () => envResponse(null))
    let finish!: () => void
    const held = new Promise<void>((resolve) => { finish = resolve })
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockImplementation(async (_slot, profile) => {
      await held
      return { profile, applies: 'next_start' }
    })
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // The PUT is still out: "This machine" here could not undo it, so it is not offered.
    expect(await button('Runs on Coder MicroVM')).toBeDisabled()
    await act(async () => { finish() })
    await waitFor(() => expect(chipEl()).not.toBeDisabled())
    expect(put).toHaveBeenCalledTimes(1)
    expect(put).toHaveBeenCalledWith('chat-1', 'dev')
  })

  it('keeps a pending switch with its own chat when the composer moves to another chat', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockImplementation(async () => envResponse(null))
    let finish!: () => void
    const held = new Promise<void>((resolve) => { finish = resolve })
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockImplementation(async (_slot, profile) => {
      await held
      return { profile, applies: 'next_start' }
    })
    const u = userEvent.setup()
    const { qc, rerender, store } = chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    expect(await button('Runs on Coder MicroVM')).toBeInTheDocument()
    expect(put).toHaveBeenCalledWith('chat-1', 'dev')
    // The same composer now shows chat-2, which runs on this machine.
    rerender(<Provider store={store}><QueryClientProvider client={qc}><SessionEnvironmentChip slot="chat-2" /></QueryClientProvider></Provider>)
    expect(await button('Runs on This machine')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Runs on Coder MicroVM' })).toBeNull()
    await act(async () => { finish() })
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(qc.getQueryData<SlotEnvironment>(slotEnvironmentQueryKey('chat-2'))?.profile ?? null).toBeNull()
    expect(await button('Runs on This machine')).toBeInTheDocument()
  })

  it('keeps "This machine" reachable for a chat bound to a profile that was removed', async () => {
    mockEnv(envResponse('gone', []))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: null, applies: 'next_start' })
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on gone'))
    await u.click(await radio(/This machine/))
    expect(put).toHaveBeenCalledWith('chat-1', null)
  })

  it("shows each profile's provider mark, and the chosen one on the chip", async () => {
    const old = { id: 'old', name: 'Older gateway', provider: '', workdir: '/w' }
    mockEnv(envResponse('dev', [{ ...DEV, template: 'python-dev' }, old]))
    const u = userEvent.setup()
    chip()
    const picker = await button('Runs on Coder MicroVM')
    expect(picker.querySelector('[data-testid="provider-mark-coder"]')).not.toBeNull()
    await u.click(picker)
    const item = (name: RegExp) => screen.getByRole('menuitemradio', { name })
    await radio(/Older gateway/)
    // The template tells profiles apart; the folder stands in when absent.
    expect(item(/Coder MicroVM/)).toHaveTextContent('python-dev')
    expect(item(/Coder MicroVM/)).not.toHaveTextContent('/workspace/crew-session')
    expect(item(/Older gateway/)).toHaveTextContent('/w')
    expect(item(/Coder MicroVM/)).toHaveAttribute('aria-checked', 'true')
    expect(item(/Coder MicroVM/).querySelector('.lucide-check')).not.toBeNull()
    expect(item(/Older gateway/).querySelector('.lucide-check')).toBeNull()
    expect(item(/Coder MicroVM/).querySelector('[data-testid="provider-mark-coder"]')).not.toBeNull()
    // An unknown provider keeps the generic cloud rather than guessing a brand.
    expect(item(/Older gateway/).querySelector('[data-testid^="provider-mark-"]')).toBeNull()
  })

  it('unbinds back to this machine with null, and re-picking the current one sends nothing', async () => {
    devEnv()
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: null, applies: 'next_start' })
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on Coder MicroVM'))
    await u.click(await radio(/Coder MicroVM/))
    expect(put).not.toHaveBeenCalled()
    await u.click(await button('Runs on Coder MicroVM'))
    await u.click(await radio(/This machine/))
    expect(put).toHaveBeenCalledWith('chat-1', null)
  })

  it('re-reads the profiles when the menu opens', async () => {
    const get = devEnv()
    const u = userEvent.setup()
    chip()
    const picker = await button('Runs on Coder MicroVM')
    const calls = get.mock.calls.length
    await u.click(picker)
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls))
  })

  it('is disabled while a turn runs, and says why', async () => {
    mockEnv(envResponse(null))
    chip(true)
    expect(await button('Stop the current response to switch profiles')).toBeDisabled()
  })

  it('shows the start stage and elapsed time while a remote start runs, then the name again', async () => {
    const get = devEnv({ starting: { stage: 'provisioning', stage_elapsed_s: 30, elapsed_s: 42 } })
    chip(true)
    const el = await screen.findByTestId('composer-environment-chip')
    // The stage only: the running step in the transcript carries the one timer.
    await waitFor(() => expect(el).toHaveTextContent(/^Starting workspace$/))
    expect(el).toHaveAccessibleName(/Starting Coder MicroVM: Starting workspace \(3\d/)
    expect(screen.getByRole('status')).toHaveTextContent('Starting workspace')
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1), { timeout: 2500 })
    // The backend clears the stage and the runtime attaches; the next poll reverts.
    get.mockResolvedValue({ ...envResponse('dev'), starting: null, attached: true })
    await waitFor(() => expect(chipEl()).toHaveTextContent('Coder MicroVM'), { timeout: 2500 })
  })

  it('shows setup, not nothing, before the first stage is reported', async () => {
    devEnv({ starting: null, attached: false })
    chip(true)
    await waitFor(() => expect(chipEl()).toHaveTextContent('Checking profile'))
  })

  it('never shows a start stage for a local session, and does not poll', async () => {
    const get = mockEnv({ ...envResponse(null), starting: { stage: 'provisioning', stage_elapsed_s: 1, elapsed_s: 1 } })
    vi.useFakeTimers()
    chip(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(chipEl()).toHaveTextContent('This machine')
    await act(async () => { await vi.advanceTimersByTimeAsync(START_POLL_MS * 3) })
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('is a fixed status, not a picker, once the session has started', async () => {
    devEnv({ locked: true })
    const put = vi.spyOn(api, 'setChatSlotEnvironment')
    const u = userEvent.setup()
    chip()
    const el = await screen.findByTestId('composer-environment-chip')
    expect(el).toHaveTextContent('Coder MicroVM')
    expect(el).toHaveAccessibleName('Runs on Coder MicroVM. Fixed for this chat.')
    expect(el.tagName).toBe('SPAN')
    // Muted with a lock, so it does not read as the picker.
    expect(el.querySelector('.lucide-lock')).not.toBeNull()
    expect(el).toHaveClass('text-muted')
    expect(screen.queryByRole('button', { name: /Coder MicroVM/ })).toBeNull()
    await u.click(el)
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(put).not.toHaveBeenCalled()
  })

  it('reports a failed switch beside the chip and keeps the last confirmed environment', async () => {
    mockEnv(envResponse(null))
    vi.spyOn(api, 'setChatSlotEnvironment').mockRejectedValue(new ApiError(400, 'unknown_profile', '{"code":"unknown_profile"}'))
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    // An in-page ErrorNotice, not a passing toast, and no hand-off beside the draft.
    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
    // It names where the switch was going and where the chat still runs.
    expect(alert).toHaveTextContent(/Couldn't switch to Coder MicroVM, so this chat still runs on This machine\./)
    // A known refusal says what it means, not the gateway's code.
    expect(alert).toHaveTextContent('That profile is no longer set up in Settings.')
    expect(alert).not.toHaveTextContent('unknown_profile')
    expect(screen.queryByRole('link', { name: /Ask the agent/ })).toBeNull()
    expect(await button('Runs on This machine')).toBeInTheDocument()
  })

  it('tells the user how to retry when the profiles could not be read', async () => {
    mockEnv(envResponse(null))
    vi.spyOn(api, 'setChatSlotEnvironment').mockRejectedValue(new ApiError(503, 'choices_unreadable', '{"code":"choices_unreadable"}'))
    const u = userEvent.setup()
    chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    const alert = await screen.findByRole('alert')
    // There is no Retry button beside the chip, so the notice names the way.
    expect(alert).toHaveTextContent("Couldn't load your profiles. Open the menu again to retry.")
    expect(alert).not.toHaveTextContent('choices_unreadable')
  })

  it('keeps a switch refusal visible when the chat locks while it waits', async () => {
    const get = mockEnv(envResponse(null))
    let refuse!: (error: Error) => void
    vi.spyOn(api, 'setChatSlotEnvironment').mockReturnValue(new Promise((_resolve, fail) => { refuse = fail }))
    const u = userEvent.setup()
    const { qc } = chip()
    await u.click(await button('Runs on This machine'))
    await u.click(await radio(/Coder MicroVM/))
    // The first message's start locks the chat while the switch waits behind it.
    get.mockResolvedValue({ ...envResponse(null), locked: true })
    await act(async () => { await qc.invalidateQueries({ queryKey: slotEnvironmentQueryKey('chat-1') }) })
    await waitFor(() => expect(chipEl().tagName).toBe('SPAN'))
    await act(async () => { refuse(new ApiError(409, 'environment_locked', '{"code":"environment_locked"}')) })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Couldn't switch to Coder MicroVM, so this chat still runs on This machine\./)
    expect(alert).toHaveTextContent("A chat's environment is fixed once its first message is sent.")
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
  const agentStarting = (extra: Partial<NonNullable<SlotEnvironment['starting']>> = {}) => ({
    stage: 'starting_agent' as const, stage_elapsed_s: 1, elapsed_s: 2, entered: STAGES, ...extra,
  })

  it('marks every stage before the current one done, run or not needed', async () => {
    // A warm pool start never enters provisioning and connects in under one
    // poll; both earlier stages still achieved their outcome, so each is done.
    devEnv({ starting: agentStarting({ elapsed_s: 3 }) })
    wrap(<Steps />)
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    for (const key of ['checking', 'provisioning', 'connecting']) {
      expect(screen.getByText(`${key}:done`)).toBeInTheDocument()
    }
  })

  it('shows no setup for an active session reconnecting to a running workspace', async () => {
    const get = devEnv({ attached: true, starting: agentStarting() })
    // A first-connection twin shares the query: once IT shows steps the read
    // has landed, so the reconnecting session's empty list is a real verdict.
    wrap(<><Steps connectedBefore /><div data-testid="first"><Steps /></div></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    const [reconnecting] = screen.getAllByTestId('steps')
    expect(reconnecting).toBeEmptyDOMElement()
  })

  it('takes the gateway at its word when it says the start reconnects', async () => {
    // The chip has no transcript to consult; the gateway's flag keeps it from
    // saying "Starting agent" while the transcript says "Thinking…".
    const get = devEnv({ attached: true, starting: agentStarting({ reconnect: true }) })
    wrap(<><Steps /><Probe /></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    // The probe reads the same query, so once it shows the stage an empty
    // list here is a real verdict, not an early read.
    expect(await screen.findByText('probe:starting_agent')).toBeInTheDocument()
    expect(screen.getByTestId('steps')).toBeEmptyDOMElement()
  })

  it('still shows setup when a reconnect has to start the workspace again', async () => {
    devEnv({ starting: { stage: 'provisioning', stage_elapsed_s: 4, elapsed_s: 5, entered: ['checking', 'provisioning'] } })
    wrap(<Steps connectedBefore />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
  })

  it('shows setup on a first connection even before the gateway reports a stage', async () => {
    devEnv({ attached: false })
    wrap(<Steps />)
    expect(await screen.findByText('checking:current')).toBeInTheDocument()
  })

  it('marks later stages pending while an earlier one runs', async () => {
    devEnv({ starting: { stage: 'provisioning', stage_elapsed_s: 9, elapsed_s: 10 } })
    wrap(<Steps />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
    expect(screen.getByText('checking:done')).toBeInTheDocument()
    expect(screen.getByText('connecting:pending')).toBeInTheDocument()
    expect(screen.getByText('starting_agent:pending')).toBeInTheDocument()
  })
})

describe('useSlotRunsHere', () => {
  // What a `!` command and Run in terminal ask before opening a shell on this machine.
  const runsHere = (slot: string | null) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return renderHook(() => useSlotRunsHere(slot), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    })
  }

  it('is true only for a confirmed local chat, false on a profile, unknown until read', async () => {
    mockEnv(envResponse(null))
    const local = runsHere('chat-1')
    await waitFor(() => expect(local.result.current).toBe(true))
    cleanup()
    devEnv()
    const remote = runsHere('chat-1')
    expect(remote.result.current).toBeUndefined()
    await waitFor(() => expect(remote.result.current).toBe(false))
  })

  it('stays unknown on a failed read, and treats a non-owner caller as local, as on main', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(new Error('choices unreadable'))
    const failed = runsHere('chat-1')
    await waitFor(() => expect(api.chatSlotEnvironment).toHaveBeenCalled())
    expect(failed.result.current).toBeUndefined()
    cleanup()
    vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(new ApiError(403, 'owner_only', '{"code":"owner_only"}'))
    const app = runsHere('chat-2')
    await waitFor(() => expect(app.result.current).toBe(true))
  })
})
