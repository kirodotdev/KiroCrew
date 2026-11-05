/**
 * App-contributed kinds of session in the sidebar's create menu
 * (`contributes.sessionKinds`): a row per enabled app's kind, in its own group,
 * that creates a slot bound to `{ app, kind }`.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom, so the caret is opened
 * by keyboard (same harness as ChatSidebar.createMenu.test.tsx).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const DROP = new Set(['layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit', 'transition', 'variants',
    'whileHover', 'whileTap', 'whileInView', 'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete'])
  const make = (tag: string) => React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
    const clean: Record<string, unknown> = {}
    for (const k of Object.keys(props)) if (k !== 'children' && !DROP.has(k)) clean[k] = props[k]
    return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
  })
  const Pass = ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children)
  return { motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }), AnimatePresence: Pass, LayoutGroup: Pass }
})
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(), listApps: vi.fn(), chatFolders: vi.fn(), dashboardConfig: vi.fn(),
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

// api.createChatSlot(name, agent, model, mode, memory_mode, title, artifact,
//   folder_id, agent_kind, no_session_folder, app_agent_owner, default_folder, session_kind)
const ARG_AGENT = 1
const ARG_MEMORY_MODE = 4
const ARG_TITLE = 5
const ARG_AGENT_KIND = 8
const ARG_APP_AGENT_OWNER = 10
const ARG_DEFAULT_FOLDER = 11
const ARG_SESSION_KIND = 12

const projectKind = {
  id: 'project', title: 'Project app', menuLabel: 'New project app',
  menuDescription: 'A React app built for this piece of work',
  icon: 'AppWindow', entry: 'dist/pane.mjs', defaultAgent: 'kirocrew-conductor',
}
const appRow = (name: string, kinds: unknown[], enabled = true) => ({
  name, displayName: name, version: '1.0.0', enabled, manifest: { name, contributes: { sessionKinds: kinds } },
})

function renderSidebar(opts: { connected?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: opts.connected ?? true, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  mocks.chatFolders.mockResolvedValue([])
  qc.setQueryData(['chat-folders'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={[]} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store }
}

function openCreateMenu() {
  fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
}

/** The menu's rows and separators, in order, by test id (or text). */
function menuRows(): string[] {
  return Array.from(screen.getByRole('menu').querySelectorAll('[role="menuitem"], [role="separator"]'))
    .map(el => el.getAttribute('role') === 'separator' ? '---' : el.getAttribute('data-testid') ?? el.textContent?.trim() ?? '')
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
  mocks.dashboardConfig.mockResolvedValue({ default_memory_mode: 'incognito' })
  mocks.listApps.mockResolvedValue([])
})
afterEach(() => vi.clearAllMocks())

describe('create menu: app-contributed session kinds', () => {
  it('lists an enabled app’s kinds in their own group under New chat and New folder', async () => {
    mocks.listApps.mockResolvedValue([
      appRow('zeta-app', [{ ...projectKind, id: 'z', menuLabel: 'New zeta thing' }]),
      appRow('project-apps', [projectKind]),
      appRow('switched-off', [{ ...projectKind, menuLabel: 'Must not show' }], false),
    ])
    renderSidebar()
    openCreateMenu()
    const row = await screen.findByTestId('new-session-kind-project-apps-project')
    expect(row.getAttribute('role')).toBe('menuitem')
    // The app's own literals: the label, then its one-line gloss under it.
    expect(row.textContent).toBe('New project appA React app built for this piece of work')
    expect(screen.getByTestId('new-session-kind-zeta-app-z').textContent).toBe('New zeta thingA React app built for this piece of work')
    expect(screen.queryByText('Must not show')).toBeNull()
    expect(menuRows().slice(0, 6)).toEqual([
      'new-plain-chat',
      'new-folder',
      '---',
      'new-session-kind-project-apps-project',
      'new-session-kind-zeta-app-z',
      '---',
    ])
  })

  it('adds no group when no enabled app declares a kind', async () => {
    renderSidebar()
    openCreateMenu()
    await screen.findByRole('menuitem', { name: 'New chat' })
    await waitFor(() => expect(mocks.listApps).toHaveBeenCalled())
    expect(menuRows().slice(0, 4)).toEqual(['new-plain-chat', 'new-folder', '---', 'new-incognito-chat'])
  })

  it('creates a persistent slot bound to { app, kind } on the kind’s agent, as a template, under the kind’s title', async () => {
    mocks.listApps.mockResolvedValue([appRow('project-apps', [projectKind])])
    const { store } = renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('new-session-kind-project-apps-project'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_SESSION_KIND]).toEqual({ app: 'project-apps', kind: 'project' })
    expect(call[ARG_AGENT]).toBe('kirocrew-conductor')
    expect(call[ARG_AGENT_KIND]).toBe('template')
    // A core agent stamped as app-owned would be refused on every turn.
    expect(call[ARG_APP_AGENT_OWNER]).toBeUndefined()
    // The person's default (incognito here) never applies: the binding lives on
    // the persisted birth line.
    expect(call[ARG_MEMORY_MODE]).toBe('persistent')
    expect(call[ARG_TITLE]).toBe('Project app')
    expect(call[ARG_DEFAULT_FOLDER]).toBe(true)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-new-1'))
  })

  it('names the app as the owner of an agent the app ships', async () => {
    mocks.listApps.mockResolvedValue([{
      ...appRow('project-apps', [{ ...projectKind, defaultAgent: 'project-conductor' }]),
      manifest: { name: 'project-apps', agents: ['agents/project-conductor.json'], contributes: { sessionKinds: [{ ...projectKind, defaultAgent: 'project-conductor' }] } },
    }])
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('new-session-kind-project-apps-project'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_AGENT]).toBe('project-conductor')
    expect(call[ARG_AGENT_KIND]).toBe('template')
    expect(call[ARG_APP_AGENT_OWNER]).toBe('project-apps')
    expect(call[ARG_SESSION_KIND]).toEqual({ app: 'project-apps', kind: 'project' })
  })

  it('sends no agent when the kind names none, so the dashboard default applies', async () => {
    const { defaultAgent: _omit, ...noAgent } = projectKind
    void _omit
    mocks.listApps.mockResolvedValue([appRow('project-apps', [noAgent])])
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('new-session-kind-project-apps-project'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_AGENT]).toBeUndefined()
    expect(call[ARG_AGENT_KIND]).toBeUndefined()
    expect(call[ARG_SESSION_KIND]).toEqual({ app: 'project-apps', kind: 'project' })
  })

  it('refuses the create while the gateway is offline', async () => {
    mocks.listApps.mockResolvedValue([appRow('project-apps', [projectKind])])
    renderSidebar({ connected: false })
    openCreateMenu()
    const row = await screen.findByTestId('new-session-kind-project-apps-project')
    expect(row.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(row)
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
  })
})
