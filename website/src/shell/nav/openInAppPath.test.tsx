import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, renderHook, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useActiveSlotState } from '../../pages/chat/page/activeSlotState'
import { useAppSelector } from '../../store'
import AgentSwitchNoticeBanner from '../../components/AgentSwitchNoticeBanner'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { createTestStore } from '../../test/helpers'
import { relayPaneOpened, setActiveId } from '../../store/instancesSlice'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { api, ApiError } from '../../api/client'
import * as chatActions from '../../store/chatSlice'
import { useOpenInAppPath } from './openInAppPath'

// The crew-link hook is a unit of its own (`lib/crewDeepLink.test.tsx`); here
// only its place in the order matters, so it answers for any crew link.
const openCrewLink = vi.fn((path: string) => path.includes('crew='))
vi.mock('../../lib/crewDeepLink', async importOriginal => ({
  ...(await importOriginal<typeof import('../../lib/crewDeepLink')>()),
  useCrewDeepLink: () => openCrewLink,
}))

let open: (path: string) => void = () => {}
let here = ''

/** A page holding a draft: its guard refuses to be left. */
function Page({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  open = useOpenInAppPath()
  const location = useLocation()
  here = location.pathname + location.search
  const notice = useAppSelector(s => s.chat.agentSwitchNotice)
  return notice ? <AgentSwitchNoticeBanner notice={notice} /> : null
}

function mount(at: string) {
  const guard = vi.fn(() => false)
  render(
    <Provider store={createTestStore()}>
      <MemoryRouter initialEntries={[at]}>
        <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
      </MemoryRouter>
    </Provider>,
  )
  return guard
}

describe('useOpenInAppPath', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('does not ask the page about the address already on screen', () => {
    const guard = mount('/schedule?job=nightly')
    act(() => { open('/schedule?job=nightly') })
    expect(guard).not.toHaveBeenCalled()
    expect(here).toBe('/schedule?job=nightly')
  })

  it.each(['/apps/detail/demo-app', '/members'])('brings the local Crew forward for %s before asking the page', (target) => {
    const store = createTestStore()
    store.dispatch(setActiveId('remote-1'))
    const guard = vi.fn(() => {
      // The leave question is asked with the local Crew already on screen.
      expect(store.getState().instances.activeId).toBeNull()
      return true
    })
    render(
      <Provider store={store}>
        <MemoryRouter initialEntries={['/schedule']}>
          <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    act(() => { open(target) })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.activeId).toBeNull()
    expect(here).toBe(target)
  })

  it('brings the local Crew forward from a relay Crew pane for an app detail link', () => {
    const store = createTestStore()
    store.dispatch(relayPaneOpened({ id: 'relay-1', name: 'Relay' }))
    const guard = vi.fn(() => {
      expect(store.getState().instances.relayActive).toBeNull()
      return true
    })
    render(
      <Provider store={store}>
        <MemoryRouter initialEntries={['/schedule']}>
          <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    act(() => { open('/apps/detail/demo-app') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.relayActive).toBeNull()
    expect(here).toBe('/apps/detail/demo-app')
  })

  it('hands a crew link to the crew-link handler before asking the page', () => {
    openCrewLink.mockClear()
    const guard = mount('/schedule')
    act(() => { open('/chat?crew=dev-box&crewSid=chat-3-1785870239') })
    expect(openCrewLink).toHaveBeenCalledWith('/chat?crew=dev-box&crewSid=chat-3-1785870239')
    expect(guard).not.toHaveBeenCalled()
    expect(here).toBe('/schedule')
  })

  it('fills the Remote Crew add form on the local Crew for an add link, after the page agrees', () => {
    const store = createTestStore()
    store.dispatch(setActiveId('remote-1'))
    const guard = vi.fn(() => {
      expect(store.getState().instances.activeId).toBeNull()
      return true
    })
    render(
      <Provider store={store}>
        <MemoryRouter initialEntries={['/schedule']}>
          <NavigationLeaveGuardProvider><Page guard={guard} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    act(() => { open('/settings/instances?host=me%40devbox.example.com&port=5476&name=Dev%20box') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.crewForms?.addLink).toEqual({ host: 'me@devbox.example.com', port: '5476', name: 'Dev box' })
    expect(here).toBe('/settings/instances')
  })

  it('does not ask about a same-page link that only adds a fragment', () => {
    const guard = mount('/settings/display?tab=theme')
    act(() => { open('/settings/display?tab=theme#accent') })
    expect(guard).not.toHaveBeenCalled()
  })

  it('asks the page about another address, and a refusal keeps it', () => {
    const guard = mount('/schedule')
    act(() => { open('/settings/security') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(here).toBe('/schedule')
  })

  it('asks when only the query differs, because a pane mounts on the query', () => {
    const guard = mount('/schedule?job=a')
    act(() => { open('/schedule?job=b') })
    expect(guard).toHaveBeenCalledTimes(1)
    expect(here).toBe('/schedule?job=a')
  })

  function mountWithRoster(at: string, openKeys: string[]) {
    const base = createTestStore().getState()
    const seeded = createTestStore({
      dashboard: { ...base.dashboard, slotsLoaded: true, slots: openKeys.map(key => ({ key, title: key })) as never },
      // The pane shows the session the address names, so a refused switch has
      // a session to go back to.
      chat: { ...base.chat, activeSlot: openKeys[0] ?? null },
    })
    render(
      <Provider store={seeded}>
        <MemoryRouter initialEntries={[at]}>
          <NavigationLeaveGuardProvider><Page guard={() => true} /></NavigationLeaveGuardProvider>
        </MemoryRouter>
      </Provider>,
    )
    return seeded
  }

  it('resumes a CLOSED session a link names, as a closed-session chip does, rather than calling it deleted', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new ApiError(404, 'not found'))
    const meta = vi.spyOn(api, 'sessionMeta').mockResolvedValue({ key: 'chat-9-1785000000', title: 'Old run' })
    const resume = vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: false, key: 'chat-9-1785000000' } as never)
    const resumeFromHistory = vi.spyOn(chatActions, 'resumeFromHistory')
    const store = mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000'])
    await act(async () => { open('/chat?sid=chat-9-1785000000') })
    expect(detail).toHaveBeenCalledWith('chat-9-1785000000', expect.anything())
    expect(meta).toHaveBeenCalledWith('chat-9-1785000000')
    expect(resumeFromHistory).toHaveBeenCalledWith({ key: 'chat-9-1785000000', title: 'Old run', leaveSplit: true })
    expect(resume).toHaveBeenCalledWith('chat-9-1785000000', 'Old run')
    expect(store.getState().chat.switchSlotGone).toBeNull()
    expect(here).toBe('/chat')
    meta.mockRestore()
    resume.mockRestore()
  })

  it('keeps a newer session selection when a closed-session lookup settles late', async () => {
    const closedKey = 'chat-9-1785000000'
    const openKey = 'chat-2-1785000000'
    let resolveMeta!: (row: Awaited<ReturnType<typeof api.sessionMeta>>) => void
    const pendingMeta = new Promise<Awaited<ReturnType<typeof api.sessionMeta>>>(resolve => { resolveMeta = resolve })
    const meta = vi.spyOn(api, 'sessionMeta').mockReturnValue(pendingMeta)
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockRejectedValueOnce(new ApiError(404, 'not found'))
      .mockResolvedValue({ messages: [], running: false } as never)
    const resume = vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: closedKey } as never)
    const resumeFromHistory = vi.spyOn(chatActions, 'resumeFromHistory')
    const store = mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000', openKey])

    await act(async () => { open(`/chat?sid=${closedKey}`) })
    expect(detail).toHaveBeenCalledWith(closedKey, expect.anything())
    expect(meta).toHaveBeenCalledWith(closedKey)
    const originalRequestId = store.getState().chat.slotSwitchLatestRequestId
    await act(async () => {
      const selected = await store.dispatch(chatActions.switchSlot({ key: openKey, gesture: true, announceOnMissing: true }))
      expect(chatActions.switchSlot.fulfilled.match(selected)).toBe(true)
    })
    expect(store.getState().chat.slotSwitchLatestRequestId).not.toBe(originalRequestId)
    expect(store.getState().chat.activeSlot).toBe(openKey)

    await act(async () => { resolveMeta({ key: closedKey, title: 'Old run' }) })
    expect(resumeFromHistory).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe(openKey)
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
    expect(store.getState().chat.switchSlotGone).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows the chip failure message when a closed-session lookup rejects, without reporting deletion', async () => {
    const meta = vi.spyOn(api, 'sessionMeta').mockRejectedValue(new Error('HTTP 503'))
    const resume = vi.spyOn(api, 'resumeChatSlot')
    const detail = vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new ApiError(404, 'not found'))
    const store = mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000'])
    const activeBefore = store.getState().chat.activeSlot
    await act(async () => { open('/chat?sid=chat-9-1785000000') })
    expect(await screen.findByText("Couldn't open chat-9-1785000000.")).toBeInTheDocument()
    expect(meta).toHaveBeenCalledWith('chat-9-1785000000')
    expect(store.getState().chat.switchSlotGone).toBeNull()
    expect(store.getState().chat.activeSlot).toBe(activeBefore)
    expect(resume).not.toHaveBeenCalled()
    expect(detail).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/was deleted/)).toBeNull()
  })

  it('shows the chip deleted notice only when the closed-session lookup confirms absence', async () => {
    vi.spyOn(api, 'sessionMeta').mockResolvedValue(null)
    const resume = vi.spyOn(api, 'resumeChatSlot')
    const detail = vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new ApiError(404, 'not found'))
    mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000'])
    await act(async () => { open('/chat?sid=chat-9-1785000000') })
    expect(await screen.findByText(/chat-9-1785000000.*was deleted and cannot be opened/)).toBeInTheDocument()
    expect(resume).not.toHaveBeenCalled()
    expect(detail).toHaveBeenCalledTimes(1)
    expect(screen.queryByText("Couldn't open chat-9-1785000000.")).toBeNull()
  })

  it('switches an OPEN session a link names in place, with no closed-session probe', () => {
    const meta = vi.spyOn(api, 'sessionMeta')
    const store = mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000', 'chat-2-1785000000'])
    act(() => { open('/chat?sid=chat-2-1785000000') })
    expect(meta).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('chat-2-1785000000')
    meta.mockRestore()
  })

  it('leaves Split View and shows the open session named by a link', async () => {
    const keys = ['chat-1-1785000000', 'chat-2-1785000000', 'chat-3-1785000000']
    const base = createTestStore().getState()
    const store = createTestStore({
      dashboard: { ...base.dashboard, slotsLoaded: true, slots: keys.map(key => ({ key, title: key })) as never },
      chat: { ...base.chat, activeSlot: keys[0] },
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false } as never)
    const { result, unmount } = renderHook(() => {
      const activeSlot = useAppSelector(s => s.chat.activeSlot)
      const navigate = useNavigate()
      const [searchParams] = useSearchParams()
      const state = useActiveSlotState({ activeSlot, navigate, searchParams })
      return { ...state, activeSlot, open: useOpenInAppPath() }
    }, {
      wrapper: ({ children }) => (
        <QueryClientProvider client={queryClient}>
          <Provider store={store}>
            <MemoryRouter initialEntries={[`/chat?sid=${keys[0]}`]}>
              <NavigationLeaveGuardProvider>{children}</NavigationLeaveGuardProvider>
            </MemoryRouter>
          </Provider>
        </QueryClientProvider>
      ),
    })
    try {
      act(() => { result.current.setSplitMode(true) })
      expect(result.current.splitMode).toBe(true)
      const leaveSplitSeq = store.getState().chat.leaveSplitSeq ?? 0
      await act(async () => { result.current.open(`/chat?sid=${keys[2]}`) })
      expect(store.getState().chat.leaveSplitSeq).toBe(leaveSplitSeq + 1)
      expect(result.current.splitMode).toBe(false)
      expect(result.current.activeSlot).toBe(keys[2])
    } finally {
      unmount()
      queryClient.clear()
    }
  })

  it('switches to a session the roster does not list yet when the server has it open', async () => {
    // The Crew Companion opens a session it just created before the slots
    // frame lists it: only the server's answer says whether it is open.
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false } as never)
    const meta = vi.spyOn(api, 'sessionMeta')
    const store = mountWithRoster('/chat?sid=chat-1-1785000000', ['chat-1-1785000000'])
    await act(async () => { open('/chat?sid=chat-9-1785000000') })
    expect(detail).toHaveBeenCalledWith('chat-9-1785000000', expect.anything())
    expect(store.getState().chat.activeSlot).toBe('chat-9-1785000000')
    expect(store.getState().chat.switchSlotGone).toBeNull()
    expect(meta).not.toHaveBeenCalled()
  })

  it('does not ask about the session already on screen', () => {
    const guard = mount('/chat?sid=chat-4-1793224442')
    act(() => { open('/chat?sid=chat-4-1793224442') })
    expect(guard).not.toHaveBeenCalled()
  })
})
