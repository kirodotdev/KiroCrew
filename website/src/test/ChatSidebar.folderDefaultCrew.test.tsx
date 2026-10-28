/**
 * A folder's Default crew routes its New chat (the folder `+`) to "New chat on
 * crew" for that crew.
 *
 * Load-bearing assertions:
 *   (1) a connected crew: the chat is minted on the crew and nothing is created
 *       locally;
 *   (2) an offline crew: the click says so and starts NOTHING until the person
 *       picks Local, which then starts exactly one local chat, filed in the folder;
 *   (3) the Remote crew sessions preview off: the pin is ignored and the chat is
 *       local, whatever the stored value;
 *   (4) the stored crew survives a failed or declined create (no implicit write).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(), listInstances: vi.fn(), chatFolders: vi.fn(), updateChatFolder: vi.fn(),
  instancesCapabilities: vi.fn(), crewPeerPost: vi.fn(), dashboardConfig: vi.fn(), setSlotFolder: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import { PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'
import { closeCrewWindow, currentCrewWindow } from '../pages/chat/crew-window/crewWindowStore'

const FOLDER_ID = 'f-crew'
const CREW = 'i-nobita'
const folderWith = (extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id: FOLDER_ID, name: 'Billing', order: 0, collapsed: false, default_crew: CREW, ...extra }) as ChatFolder

function renderSidebar(opts: { warm?: Record<string, unknown>; folder?: ChatFolder; folders?: ChatFolder[]; onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void; crewsLoaded?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
    ...(opts.warm ? { instances: { warm: opts.warm } as unknown as RootState['instances'] } : {}),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const folders = opts.folders ?? [opts.folder ?? folderWith()]
  mocks.chatFolders.mockResolvedValue(folders)
  qc.setQueryData(['chat-folders'], folders)
  // The crew list as a loaded sidebar has it: the router reads names from it.
  if (opts.crewsLoaded) qc.setQueryData(['instances'], { instances: [{ id: CREW, name: 'nobita' }] })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onOpenSlotInNewTab={opts.onOpenSlotInNewTab}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, qc }
}

const folderPlus = () => screen.getByTestId(`folder-new-chat-${FOLDER_ID}`)
const WARM = { [CREW]: { port: 7879, token: 't' } }

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  closeCrewWindow()
  mocks.createChatSlot.mockImplementation((...args: unknown[]) =>
    Promise.resolve({ key: 'chat-new-1', folder_id: (args[7] as string) || '' }))
  mocks.instancesCapabilities.mockResolvedValue({ version_match: true, version: '0.9.0', local_version: '0.9.0' })
  mocks.dashboardConfig.mockResolvedValue({ default_memory_mode: 'persistent' })
  mocks.crewPeerPost.mockResolvedValue({ key: 'peer-new-1' })
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.listInstances.mockResolvedValue({
    active: true, warm_set_cap: 5, sso: {},
    instances: [{ id: CREW, name: 'nobita' }],
  })
})
afterEach(() => {
  closeCrewWindow()
  vi.clearAllMocks()
})

describe('folder default crew: the folder + button', () => {
  it('starts the chat on the connected crew and creates nothing locally', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar({ warm: WARM })
    fireEvent.click(folderPlus())

    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledWith(CREW, 'api/chat/slots', { memory_mode: 'persistent' }))
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: CREW, key: 'peer-new-1' }))
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
  })

  it('honours the background-tab gesture: the chat is created on the crew and no window opens', async () => {
    // Ctrl-click (Cmd on mac) and middle-click keep the user where they are, as
    // they do for a local folder create; a crew window is not a tab, so the
    // chat is only created and listed in the crew's group.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ warm: WARM, onOpenSlotInNewTab, crewsLoaded: true })
    fireEvent.click(folderPlus(), { ctrlKey: true })

    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledWith(CREW, 'api/chat/slots', { memory_mode: 'persistent' }))
    // startChatOnCrew opens a window in the same continuation that receives the
    // create's reply, so once that reply has settled a window would already be open.
    await mocks.crewPeerPost.mock.results[0].value
    expect(currentCrewWindow()).toBeNull()
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
    // Nothing opened, so the folder says where the chat went, as a status.
    const status = await screen.findByTestId(`folder-crew-status-${FOLDER_ID}`)
    expect(status).toHaveTextContent('Started a chat on nobita')
    expect(status).toHaveAttribute('role', 'status')
  })

  it('says the crew create is under way, and a second click on the same folder starts nothing more', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    let release!: (v: { key: string }) => void
    mocks.crewPeerPost.mockReturnValueOnce(new Promise(r => { release = r }))
    renderSidebar({ warm: WARM, crewsLoaded: true })
    fireEvent.click(folderPlus())
    expect(await screen.findByTestId(`folder-crew-status-${FOLDER_ID}`)).toHaveTextContent('Starting a chat on nobita')
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledTimes(1))
    fireEvent.click(folderPlus())
    await new Promise(r => setTimeout(r, 0))
    expect(mocks.instancesCapabilities).toHaveBeenCalledTimes(1)
    release({ key: 'peer-new-1' })
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: CREW, key: 'peer-new-1' }))
    // The window says it now: the progress line goes.
    await waitFor(() => expect(screen.queryByTestId(`folder-crew-status-${FOLDER_ID}`)).toBeNull())
  })

  it('does not let a slow create on one crew folder swallow New chat on another', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    let release!: (v: { key: string }) => void
    mocks.crewPeerPost.mockReturnValueOnce(new Promise(r => { release = r }))
    const other = { ...folderWith(), id: 'f-crew-2', name: 'Payroll', order: 1 } as ChatFolder
    const store = createTestStore({
      dashboard: {
        status: {}, connected: true, slots: [], approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      } as unknown as RootState['dashboard'],
      chat: { activeSlot: null } as unknown as RootState['chat'],
      instances: { warm: WARM } as unknown as RootState['instances'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const folders = [folderWith(), other]
    mocks.chatFolders.mockResolvedValue(folders)
    qc.setQueryData(['chat-folders'], folders)
    render(
      <QueryClientProvider client={qc}><Provider store={store}><ThemeProvider><MemoryRouter>
        <ChatSidebar slots={[]} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]} />
      </MemoryRouter></ThemeProvider></Provider></QueryClientProvider>,
    )
    fireEvent.click(folderPlus())
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId('folder-new-chat-f-crew-2'))
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledTimes(2))
    release({ key: 'peer-new-1' })
  })

  // Opus 5.5 on c290d3765e: a Local folder's + while a crew create is in flight
  // makes the crew create stale; its outcome must still settle its own folder's
  // "Starting…" line instead of leaving it up with nothing in flight.
  it.each([
    ['fails', 'shows the failure in place of the Starting line'],
    ['succeeds', 'clears the Starting line'],
  ])('a crew create overtaken by a Local folder create that %s %s', async (outcome) => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    let settle!: { resolve: (v: { key: string }) => void; reject: (e: Error) => void }
    mocks.crewPeerPost.mockReturnValueOnce(new Promise((resolve, reject) => { settle = { resolve, reject } }))
    const local = { ...folderWith({ default_crew: '' }), id: 'f-local', name: 'Notes', order: 1 } as ChatFolder
    renderSidebar({ warm: WARM, crewsLoaded: true, folders: [folderWith(), local] })
    fireEvent.click(folderPlus())
    expect(await screen.findByTestId(`folder-crew-status-${FOLDER_ID}`)).toHaveTextContent('Starting a chat on nobita')
    await waitFor(() => expect(mocks.crewPeerPost).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId('folder-new-chat-f-local'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    await mocks.createChatSlot.mock.results[0].value
    if (outcome === 'fails') {
      settle.reject(new Error('peer refused the session'))
      const notice = await screen.findByTestId(`folder-create-error-${FOLDER_ID}`)
      expect(notice).toHaveTextContent('peer refused the session')
    } else {
      settle.resolve({ key: 'peer-new-1' })
      await mocks.crewPeerPost.mock.results[0].value
    }
    await waitFor(() => expect(screen.queryByTestId(`folder-crew-status-${FOLDER_ID}`)).toBeNull())
  })

  it('calls a crew that is no longer registered "no longer registered", not offline', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    mocks.listInstances.mockResolvedValue({ active: true, warm_set_cap: 5, sso: {}, instances: [{ id: 'i-other', name: 'other' }] })
    const { qc } = renderSidebar({ warm: { 'i-other': { port: 1, token: 't' } } })
    qc.setQueryData(['instances'], { instances: [{ id: 'i-other', name: 'other' }] })
    fireEvent.click(folderPlus())
    const notice = await screen.findByTestId(`folder-create-error-${FOLDER_ID}`)
    expect(notice).toHaveTextContent(`${CREW} is no longer a registered crew`)
    expect(notice).not.toHaveTextContent('offline')
    expect(screen.getByTestId(`folder-create-error-local-${FOLDER_ID}`)).toBeInTheDocument()
  })

  it('says the crew is offline, starts nothing, and keeps the folder pinned', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    // Registered but not connected: no warm entry.
    const { qc } = renderSidebar({ warm: { 'i-other': { port: 1, token: 't' } } })
    fireEvent.click(folderPlus())

    const notice = await screen.findByTestId(`folder-create-error-${FOLDER_ID}`)
    expect(notice.textContent).toContain('nobita is offline')
    // A state, not a failure: the warn-tone status notice, with no ask-agent.
    expect(notice.getAttribute('role')).toBe('status')
    expect(notice.getAttribute('data-tone')).toBe('warn')
    // Inside the notice, next to the state it fixes (UX r121), not under it.
    expect(within(notice).getByTestId(`folder-create-error-local-${FOLDER_ID}`).textContent).toBe('Start this chat on Local instead')
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
    // The refusal never edits the folder: the pin stays for the next click.
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])?.[0]?.default_crew).toBe(CREW)
  })

  it('offers Local for that one chat: one local chat in the folder, pin untouched', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    const { qc } = renderSidebar({ warm: { 'i-other': { port: 1, token: 't' } } })
    fireEvent.click(folderPlus())
    await screen.findByTestId(`folder-create-error-local-${FOLDER_ID}`)
    // Queried again: the list re-renders once the crew list lands, and a node
    // found a tick earlier can already be detached.
    fireEvent.click(screen.getByTestId(`folder-create-error-local-${FOLDER_ID}`))

    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(mocks.createChatSlot.mock.calls[0][7]).toBe(FOLDER_ID)
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTestId(`folder-create-error-${FOLDER_ID}`)).toBeNull())
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])?.[0]?.default_crew).toBe(CREW)
    // The next click asks again: Local was a one-chat answer, not a stored one.
    fireEvent.click(folderPlus())
    expect(await screen.findByTestId(`folder-create-error-${FOLDER_ID}`)).toBeTruthy()
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('ignores the pin while the Remote crew sessions preview is off', async () => {
    renderSidebar({ warm: WARM })
    fireEvent.click(folderPlus())

    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(mocks.createChatSlot.mock.calls[0][7]).toBe(FOLDER_ID)
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
    expect(screen.queryByTestId(`folder-create-error-${FOLDER_ID}`)).toBeNull()
  })

  it('creates locally in a folder with no default crew, preview on', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar({ warm: WARM, folder: folderWith({ default_crew: '' }) })
    fireEvent.click(folderPlus())

    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(mocks.crewPeerPost).not.toHaveBeenCalled()
  })

  it('reports a failed crew create under the folder instead of falling back to Local', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    mocks.crewPeerPost.mockRejectedValue(new Error('peer refused the session'))
    renderSidebar({ warm: WARM })
    fireEvent.click(folderPlus())

    const notice = await screen.findByTestId(`folder-create-error-${FOLDER_ID}`)
    expect(notice.textContent).toContain('peer refused the session')
    expect(screen.queryByTestId(`folder-create-error-local-${FOLDER_ID}`)).toBeNull()
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
  })
})

describe('folder default crew: Folder settings', () => {
  // Synchronous by necessity: Radix tears the folder menu down on the first
  // macrotask in jsdom, so the menu is driven in the same tick it opens.
  function openFolderSettings() {
    fireEvent.keyDown(screen.getByTestId(`folder-menu-${FOLDER_ID}`), { key: 'Enter' })
    fireEvent.click(screen.getByTestId(`folder-settings-${FOLDER_ID}`))
  }

  it('shows the Default crew picker with online state while the preview is on, and PATCHes only that field', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    const { qc } = renderSidebar({ warm: WARM, folder: folderWith({ default_crew: '' }) })
    // The registered-crew list lands asynchronously; the picker offers it once it has.
    await waitFor(() => expect(qc.getQueryData(['instances'])).toBeTruthy())
    openFolderSettings()

    const trigger = await screen.findByRole('button', { name: 'Default crew' })
    expect(trigger).toHaveTextContent('Local')
    // The modal's focus move to the name field would dismiss a popup opened first.
    await waitFor(() => expect(screen.getByTestId('folder-config-name')).toHaveFocus())
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'nobita (online)' }))
    fireEvent.click(screen.getByTestId('folder-config-submit'))

    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(FOLDER_ID, { default_crew: CREW }))
  })

  it('offers no Default crew picker while the preview is off', () => {
    renderSidebar({ warm: WARM })
    openFolderSettings()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Default crew' })).toBeNull()
  })
})
