import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Routes, Route, useLocation } from 'react-router-dom'
import AgentSwitchNoticeBanner from '../components/AgentSwitchNoticeBanner'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { NavigationLeaveGuardProvider, usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'

const createChatSlot = vi.fn()
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, createChatSlot: (...a: unknown[]) => createChatSlot(...a) } }
})

const NEW_SLOT = { key: 'new-chat', agent: 'writer', agent_kind: 'member', messages: [] }

function deferred<T>() {
  let resolve: (v: T) => void = () => {}
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>
}

function BannerFromStore() {
  const n = useAppSelector(s => s.chat.agentSwitchNotice)
  return n ? <AgentSwitchNoticeBanner notice={n} /> : null
}

// `createChatForView` reads the page from `window.location`, so each test
// starts the address bar on the same route the router renders.
function renderBanner(notice: { message: string; newChatAgent?: string; error?: true }, route = '/agents', guard?: () => boolean, atStake = false) {
  window.history.pushState(null, '', route)
  const store = createTestStore()
  store.dispatch(setAgentSwitchNotice(notice))
  const page = (
    <>
      <LocationProbe />
      <Routes>
        <Route path="*" element={<BannerFromStore />} />
      </Routes>
    </>
  )
  // A page holding a draft registers its veto with the shell's leave guard.
  const ui = guard
    ? <NavigationLeaveGuardProvider><GuardedPage guard={guard} atStake={atStake}>{page}</GuardedPage></NavigationLeaveGuardProvider>
    : page
  return renderWithProviders(ui, { store, route })
}

function GuardedPage({ guard, atStake, children }: { guard: () => boolean; atStake: boolean; children: React.ReactNode }) {
  useRegisterNavigationLeaveGuard(guard)
  usePublishNavigationStake(atStake)
  return <>{children}</>
}

// The shape `agentSwitchFailureNotice` gives a refused crewmate pick.
const OFFER = { message: 'writer has its own memory', newChatAgent: 'writer', error: true as const }

describe('AgentSwitchNoticeBanner', () => {
  beforeEach(() => {
    createChatSlot.mockReset()
  })
  afterEach(() => {
    window.history.pushState(null, '', '/')
  })

  it('shows an informational notice as status text with no new-chat button', () => {
    renderBanner({ message: 'session folder unavailable' })
    const banner = screen.getByTestId('agent-switch-notice')
    expect(banner.getAttribute('role')).toBe('status')
    expect(banner.textContent).toContain('session folder unavailable')
    expect(screen.queryByTestId('agent-switch-notice-error')).toBeNull()
    expect(screen.queryByTestId('agent-switch-notice-new-chat')).toBeNull()
    expect(screen.queryByTestId('agent-switch-notice-create-error')).toBeNull()
  })

  it('shows an error notice through ErrorNotice and keeps the dismiss control', () => {
    const { store } = renderBanner({ message: 'invalid agent name', error: true })
    const banner = screen.getByTestId('agent-switch-notice')
    const error = within(banner).getByTestId('agent-switch-notice-error')
    expect(error.getAttribute('role')).toBe('alert')
    expect(error.textContent).toContain('invalid agent name')
    expect(screen.queryByTestId('agent-switch-notice-new-chat')).toBeNull()
    fireEvent.click(within(banner).getByLabelText('Dismiss'))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('shows the new-chat offer through ErrorNotice with its new-chat button', () => {
    renderBanner(OFFER)
    const banner = screen.getByTestId('agent-switch-notice')
    const error = within(banner).getByTestId('agent-switch-notice-error')
    expect(error.getAttribute('role')).toBe('alert')
    expect(error.textContent).toContain('writer has its own memory')
    expect(within(banner).getByTestId('agent-switch-notice-new-chat').textContent).toBe('New chat with writer')
  })

  it('drops a failed create error when a new notice replaces the offer', async () => {
    createChatSlot.mockRejectedValue(new Error('create failed'))
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    await waitFor(() => expect(screen.getByTestId('agent-switch-notice-create-error').textContent).toContain('create failed'))
    act(() => { store.dispatch(setAgentSwitchNotice({ message: 'a turn is running', error: true })) })
    expect(screen.queryByTestId('agent-switch-notice-create-error')).toBeNull()
    expect(screen.getByTestId('agent-switch-notice').textContent).toContain('a turn is running')
  })

  it('leaves a notice that replaced the offer while the create ran', async () => {
    const pending = deferred<typeof NEW_SLOT>()
    createChatSlot.mockReturnValue(pending.promise)
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    // The create path's own folder-fallback line lands before the create settles.
    act(() => { store.dispatch(setAgentSwitchNotice('session folder unavailable')) })
    await act(async () => { pending.resolve(NEW_SLOT); await pending.promise })
    await waitFor(() => expect(createChatSlot).toHaveBeenCalledTimes(1))
    expect(store.getState().chat.agentSwitchNotice).toEqual({ message: 'session folder unavailable' })
  })

  it('leaves a newer offer for the same crewmate when the earlier create succeeds', async () => {
    const pending = deferred<typeof NEW_SLOT>()
    createChatSlot.mockReturnValue(pending.promise)
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    // The same crewmate refused again in another chat posts a fresh offer.
    act(() => { store.dispatch(setAgentSwitchNotice(OFFER)) })
    const newer = store.getState().chat.agentSwitchNotice
    await act(async () => { pending.resolve(NEW_SLOT); await pending.promise })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-chat'))
    expect(store.getState().chat.agentSwitchNotice).toBe(newer)
  })

  it('does not attach an earlier failed create to a newer offer for the same crewmate', async () => {
    let reject: (e: Error) => void = () => {}
    createChatSlot.mockReturnValue(new Promise((_, r) => { reject = r }))
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    act(() => { store.dispatch(setAgentSwitchNotice(OFFER)) })
    const newer = store.getState().chat.agentSwitchNotice
    await act(async () => { reject(new Error('create failed')) })
    await waitFor(() => expect((screen.getByTestId('agent-switch-notice-new-chat') as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByTestId('agent-switch-notice-create-error')).toBeNull()
    expect(store.getState().chat.agentSwitchNotice).toBe(newer)
  })

  it('creates a chat on the refused crewmate through the shared helper, opens it and clears the notice', async () => {
    createChatSlot.mockResolvedValue(NEW_SLOT)
    const { store } = renderBanner(OFFER)
    const button = screen.getByTestId('agent-switch-notice-new-chat')
    expect(button.textContent).toBe('New chat with writer')
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat'))
    // createChatSlot(project, agent, model, mode, memory_mode, title, color, folderId, agentKind, ...)
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    const args = createChatSlot.mock.calls[0]
    expect(args[1]).toBe('writer')
    expect(args[8]).toBe('member')
    expect(store.getState().chat.activeSlot).toBe('new-chat')
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('creates no chat and stays put when the page keeps its draft', async () => {
    createChatSlot.mockResolvedValue(NEW_SLOT)
    const guard = vi.fn(() => false)
    const { store } = renderBanner(OFFER, '/capabilities?tab=prompts', guard)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    // Let any create that wrongly started settle before checking it did not.
    await act(async () => { await Promise.resolve() })
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(screen.getByTestId('location').textContent).toBe('/capabilities')
    expect(store.getState().chat.activeSlot).not.toBe('new-chat')
    expect(store.getState().chat.agentSwitchNotice).toEqual(OFFER)
    expect(guard).toHaveBeenCalledTimes(1)
  })

  it('keeps the created chat but stays put when the page starts holding work while the create runs', async () => {
    const pending = deferred<typeof NEW_SLOT>()
    createChatSlot.mockReturnValue(pending.promise)
    let dirty = false
    const guard = vi.fn(() => !dirty)
    const { store } = renderBanner(OFFER, '/agents', guard)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    expect(guard).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalledTimes(1))
    // The user starts an edit on the page while the create is in flight.
    dirty = true
    await act(async () => { pending.resolve(NEW_SLOT); await pending.promise })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-chat'))
    await waitFor(() => expect(guard).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('location').textContent).toBe('/agents')
    // The chat exists, so the offer is spent and a retry would make a second one.
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('asks a page holding work once and opens the chat when the user lets it go', async () => {
    const pending = deferred<typeof NEW_SLOT>()
    createChatSlot.mockReturnValue(pending.promise)
    // A `window.confirm` guard that does not clear its draft on accept (the
    // Crewmates page Goals draft): asked again, it asks the user again.
    const confirm = vi.fn(() => true)
    const guard = vi.fn(() => confirm())
    const { store } = renderBanner(OFFER, '/agents', guard, true)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    await waitFor(() => expect(createChatSlot).toHaveBeenCalledTimes(1))
    // A second ask answered "stay" would hide the chat the click made.
    confirm.mockReturnValue(false)
    await act(async () => { pending.resolve(NEW_SLOT); await pending.promise })
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat'))
    expect(store.getState().chat.activeSlot).toBe('new-chat')
    expect(guard).toHaveBeenCalledTimes(1)
  })

  it('does not ask the leave guard on /chat, where the click navigates nowhere', async () => {
    createChatSlot.mockResolvedValue(NEW_SLOT)
    const guard = vi.fn(() => false)
    const { store } = renderBanner(OFFER, '/chat', guard)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-chat'))
    expect(guard).not.toHaveBeenCalled()
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('does not navigate when the user moved to another page while the create ran', async () => {
    const pending = deferred<typeof NEW_SLOT>()
    createChatSlot.mockReturnValue(pending.promise)
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    window.history.pushState(null, '', '/settings')
    await act(async () => { pending.resolve(NEW_SLOT) })
    // The chat exists, so the offer is spent, but the user is not pulled to it.
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toBeNull())
    expect(screen.getByTestId('location').textContent).toBe('/agents')
  })

  it('shows a failed create as an ErrorNotice inside the banner and keeps the button for a retry', async () => {
    createChatSlot.mockRejectedValueOnce(new Error('create failed'))
    const { store } = renderBanner(OFFER)
    fireEvent.click(screen.getByTestId('agent-switch-notice-new-chat'))
    const banner = screen.getByTestId('agent-switch-notice')
    const error = await within(banner).findByTestId('agent-switch-notice-create-error')
    expect(error.getAttribute('role')).toBe('alert')
    expect(error.textContent).toContain('create failed')
    expect(store.getState().chat.agentSwitchNotice).toEqual(OFFER)
    expect(screen.getByTestId('location').textContent).toBe('/agents')
    const button = screen.getByTestId('agent-switch-notice-new-chat') as HTMLButtonElement
    expect(button.disabled).toBe(false)

    createChatSlot.mockResolvedValueOnce(NEW_SLOT)
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/chat'))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})
