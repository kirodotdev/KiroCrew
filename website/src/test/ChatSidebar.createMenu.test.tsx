/**
 * The split create-button's caret menu must list the ORDINARY chat, not only
 * the alternative ways to create one.
 *
 * Two load-bearing assertions:
 *   (1) "New chat" renders in the menu alongside "New autopilot chat" — a menu
 *       that offers only autopilot + folder entries reads as if the caret could
 *       not make a plain chat at all;
 *   (2) it creates a PLAIN chat even when `defaultAutopilot` is on. The main
 *       segment honours that preference; this entry names its mode, so it must
 *       pin it — otherwise the one control that says "New chat" hands back an
 *       autopilot session.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so the trigger is activated by keyboard — the path jsdom does handle.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { SETTINGS_CREW_MEMBERS_PREVIEW_ID } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

// `defaultAutopilot` is the whole point of assertion (2), so the config mock is
// a mutable box the tests flip between renders.
const cfg = vi.hoisted(() => ({ value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown> }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), listInstances: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

const mobileViewport = { value: false }

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    get matches() { return mobileViewport.value }, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
// Not mocked: the gate reads real localStorage, so the fixture that turns crew
// on is the same write the Settings > Developer > Feature Previews toggle performs.
import { PREVIEW_CREW, PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'

/** Where the router is: the "Crew Members" entry navigates rather than creates,
 *  so its tests read the destination back instead of a create call. */
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname}{loc.search}</div>
}

function renderSidebar(opts: { warm?: Record<string, unknown>; defaultAgent?: string } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
    // Omitted entirely unless a test asks for a connected peer, which is also
    // the shape every other sidebar harness renders under — the sidebar's read
    // of the instances slice has to stay guarded.
    ...(opts.warm ? { instances: { warm: opts.warm } as unknown as RootState['instances'] } : {}),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={opts.defaultAgent ?? ''} installedAgents={[]}
            />
            <LocationProbe />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return view
}

function openCreateMenu() {
  const caret = screen.getByLabelText('More create options')
  fireEvent.keyDown(caret, { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  mobileViewport.value = false
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  installSoftNavigate(() => {})
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
  mocks.listInstances.mockResolvedValue({
    active: true, warm_set_cap: 5, sso: {},
    instances: [{ id: 'i-nobita', name: 'nobita' }, { id: 'i-gian', name: 'gian' }],
  })
})
afterEach(() => {
  mobileViewport.value = false
  __resetNavSeamForTests()
  vi.clearAllMocks()
})

describe('create-button caret menu', () => {
  it('lists "New chat" next to "New autopilot chat"', async () => {
    renderSidebar()
    openCreateMenu()
    expect(await screen.findByText('New chat')).toBeTruthy()
    expect(screen.getByText('New autopilot chat')).toBeTruthy()
  })

  it('explains the engineered entries, at the point of choice', async () => {
    // The moment a user cannot tell Autopilot from Crew Members is the moment
    // this menu opens. Before this, the only explanation was a native title= on
    // the sidebar badge — i.e. visible only after the session already existed.
    // The Members page is on, so the crew gloss describes the page itself.
    localStorage.setItem(PREVIEW_CREW, '1')
    renderSidebar()
    openCreateMenu()
    await screen.findByText('New autopilot chat')
    // The contrast that matters: one job in stages vs standing agents you talk to.
    expect(screen.getByText(/One job, done in steps/)).toBeTruthy()
    expect(screen.getByText(/Opens the Crew Members page/)).toBeTruthy()
  })

  it('leaves the plain entries single-line', async () => {
    // "New chat" / "New folder" need no gloss, and describing them would bury
    // the contrast between the two engineered modes.
    renderSidebar()
    openCreateMenu()
    await screen.findByText('New chat')
    // Assert on the menu ITEM (the role=menuitem ancestor), not the text node:
    // "New chat" is a bare child of the menu container, so parentElement there
    // is the whole menu and would sweep in every sibling's copy.
    for (const label of ['New chat', 'New folder']) {
      const item = screen.getByText(label).closest('[role="menuitem"]')
      expect(item).not.toBeNull()
      expect(item?.textContent?.trim()).toBe(label)
    }
  })

  it('"New chat" creates a plain session even when defaultAutopilot is on', async () => {
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByText('New chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    // createSlot passes the mode positionally; assert no call carried 'orchestrator'.
    for (const call of mocks.createChatSlot.mock.calls) {
      expect(call).not.toContain('orchestrator')
    }
  })

  it('"New autopilot chat" still creates an orchestrator session', async () => {
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByText('New autopilot chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    expect(mocks.createChatSlot.mock.calls.some(c => c.includes('orchestrator'))).toBe(true)
  })

  // "Crew Members" — Crew Mode retired, and the entry that used to create a
  // `mode: 'crew'` session is now the door to the Members page. It is NOT
  // preview-gated: the flag only decides where the click lands.
  it('lists Crew Members whatever the preview flag says, and never creates a session', async () => {
    // Asserted on a plain `localStorage.clear()` (the beforeEach), which is the
    // state a fresh install is in — the state the old entry was hidden in.
    renderSidebar()
    openCreateMenu()
    // Anchor on a sibling entry first: an empty query below would also pass if
    // the menu simply failed to open.
    await screen.findByText('New autopilot chat')
    const item = screen.getByTestId('open-crew-members')
    expect(item.textContent).toContain('Crew Members')
    // The retired ingress and its experimental tag are gone, not merely hidden.
    expect(screen.queryByTestId('new-crew-chat')).toBeNull()
    expect(screen.queryByText('New Crew Mode chat')).toBeNull()
    expect(item.querySelector('[data-testid="crew-experimental-tag"]')).toBeNull()
    fireEvent.click(item)
    await waitFor(() => expect(screen.getByTestId('location').textContent).not.toBe('/'))
    expect(mocks.createChatSlot).not.toHaveBeenCalled()
  })

  it('opens the Members page when the preview flag is on', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('open-crew-members'))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/members'))
  })

  it('opens the Feature Previews card that turns the page on when the flag is off', async () => {
    // Not a toast telling the user to go and find the switch: the click lands
    // ON the switch, ringed, via the same `?highlight=` deep link Settings
    // search uses.
    renderSidebar()
    openCreateMenu()
    const item = await screen.findByTestId('open-crew-members')
    // The gloss discloses the detour BEFORE the click, instead of promising the
    // page and then landing somewhere else (UX review on #9519).
    expect(item.textContent).toMatch(/Opens Settings first/)
    expect(item.textContent).not.toMatch(/Opens the Crew Members page/)
    fireEvent.click(item)
    await waitFor(() => expect(screen.getByTestId('location').textContent)
      .toBe(`/settings/developer?highlight=${SETTINGS_CREW_MEMBERS_PREVIEW_ID}`))
  })

  it('deep-links to an id the settings registry still knows', () => {
    // Registry ids derive from the card's LABEL, so a relabel silently breaks
    // an inlined string — this pins the constant to a live entry on the
    // developer tab, the same guard `SETTINGS_DEFAULT_MODEL_ID` carries.
    const entry = SETTINGS_REGISTRY.find(e => e.id === SETTINGS_CREW_MEMBERS_PREVIEW_ID)
    expect(entry, `no registry entry for ${SETTINGS_CREW_MEMBERS_PREVIEW_ID}`).toBeDefined()
    expect(entry?.tab).toBe('developer')
  })

  // "New chat on crew" — creating a session that runs on a connected peer. The
  // row mirrors "New chat in folder": a dynamic list behind one submenu. It is
  // preview-gated on its OWN flag (not the Crew Members page's), so both conditions have to
  // hold: a warm peer AND the opt-in.
  it('offers no crew entry when no peer holds a live tunnel', async () => {
    // Absent, not disabled. A disabled row on a single-machine install
    // advertises a capability that install may never have, and every existing
    // sidebar harness renders with no instances slice at all — so this is also
    // the shape that proves the slice read stays guarded.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar()
    openCreateMenu()
    await screen.findByText('New chat')
    expect(screen.queryByTestId('new-chat-on-crew')).toBeNull()
    expect(screen.queryByText('New chat on crew')).toBeNull()
  })

  it('offers no crew entry on a fresh install even with a peer connected', async () => {
    // The gate is the point: a connected crew alone must not surface the entry,
    // because the landing is what is unfinished. Anchored on a sibling entry so
    // an empty query cannot pass on a menu that simply failed to open.
    renderSidebar({ warm: { 'i-nobita': { local_port: 7879, token: 't' } } })
    openCreateMenu()
    await screen.findByText('New chat')
    expect(screen.queryByTestId('new-chat-on-crew')).toBeNull()
  })

  it('lists each connected crew and creates the session on the one picked', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar({ warm: { 'i-nobita': { local_port: 7879, token: 't' } } })
    openCreateMenu()
    // The trigger names the action; the crew names live one level down, so a
    // second connected peer never lengthens the top-level menu.
    const trigger = await screen.findByTestId('new-chat-on-crew')
    expect(trigger.textContent).toContain('New chat on crew')
    fireEvent.keyDown(trigger, { key: 'ArrowRight' })

    // Only the WARM peer is offered: `listInstances` also returns gian, which
    // holds no tunnel, and a row for it would fail the moment it was clicked.
    const row = await screen.findByTestId('new-chat-on-crew-i-nobita')
    expect(row.textContent).toContain('nobita')
    expect(screen.queryByTestId('new-chat-on-crew-i-gian')).toBeNull()

    fireEvent.click(row)
    // The session is created LOCALLY and bound to the peer for execution, so it
    // lands in this machine's list with a crew chip. This replaced an earlier
    // shape that POSTed straight to the peer's own create endpoint through the
    // proxy: the session then existed only over there, and the only way to reach
    // it was to switch to that crew's iframe pane. `instance_id` is what carries
    // the binding, and it must be sent at BIRTH — the backend opens the peer's
    // slot before creating the local one, so a disconnected or version-skewed
    // peer fails the create instead of leaving a session that cannot send.
    await waitFor(() =>
      expect(mocks.createChatSlot).toHaveBeenCalledWith(
        undefined, undefined, undefined, undefined, 'persistent', undefined, undefined, undefined,
        undefined, 'i-nobita',
      ),
    )
  })

  it.each([
    ['desktop', 'Enter', false, '{Enter}'],
    ['desktop', 'Space', false, ' '],
    ['mobile', 'Enter', true, '{Enter}'],
    ['mobile', 'Space', true, ' '],
  ])('keeps the crew row action in %s and stages its failure with %s', async (_surface, _label, mobile, key) => {
    const user = userEvent.setup()
    mobileViewport.value = mobile
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    mocks.createChatSlot.mockRejectedValue(new Error('peer version mismatch'))
    renderSidebar({ warm: { 'i-nobita': { local_port: 7879, token: 't' } } })
    openCreateMenu()

    if (!mobile) {
      const trigger = await screen.findByTestId('new-chat-on-crew')
      fireEvent.keyDown(trigger, { key: 'ArrowRight' })
    }
    let row = await screen.findByTestId('new-chat-on-crew-i-nobita')
    row.focus()
    await user.keyboard('{Enter}')
    const alert = await screen.findByTestId('new-chat-on-crew-error')
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)

    row = await screen.findByTestId('new-chat-on-crew-i-nobita')
    row.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
    await screen.findByTestId('new-chat-on-crew-error')

    row = await screen.findByTestId('new-chat-on-crew-i-nobita')
    row.focus()
    await user.keyboard('{ArrowDown}')
    const handoff = screen.getByRole('menuitem', { name: /^Ask the agent$/i })
    expect(handoff).toHaveFocus()
    expect(handoff).toHaveAttribute('aria-describedby', alert.id)
    await user.keyboard(key)

    expect(consumeChatHandoff()).toContain('peer version mismatch')
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(2)
  })

  it('sends no agent with a crew create even when this machine has a default', async () => {
    // The assertion above pins `agent` to undefined too, but only because its
    // fixture configures no default — so it would still pass if the entry sent
    // one. This is the case that makes the omission load-bearing: `defaultAgent`
    // names a crew from THIS machine's roster and the backend forwards any agent
    // it is given straight to the peer, where that name means nothing (or means
    // a different crew). The peer applies its own default instead.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar({ warm: { 'i-nobita': { local_port: 7879, token: 't' } }, defaultAgent: 'planner' })
    openCreateMenu()
    const trigger = await screen.findByTestId('new-chat-on-crew')
    fireEvent.keyDown(trigger, { key: 'ArrowRight' })
    fireEvent.click(await screen.findByTestId('new-chat-on-crew-i-nobita'))

    // `agent` is the SECOND positional argument; `instance_id` the tenth.
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    const call = mocks.createChatSlot.mock.calls.at(-1)
    expect(call?.[1]).toBeUndefined()
    expect(call?.[9]).toBe('i-nobita')
    expect(call).not.toContain('planner')
  })

  it('still stamps the default agent on an ordinary local create', async () => {
    // The contrast that makes the subtraction above a deliberate one rather than
    // a dropped argument: the local entry DOES carry this machine's default.
    renderSidebar({ defaultAgent: 'planner' })
    openCreateMenu()
    fireEvent.click(await screen.findByText('New chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    expect(mocks.createChatSlot.mock.calls.at(-1)?.[1]).toBe('planner')
  })
})
