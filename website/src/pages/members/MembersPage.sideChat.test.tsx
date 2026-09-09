import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'

/* The selection toolbar's "Ask about this" on a member thread lands in the
 * page's side panel: the chat page's tabbed SidePanel is docked here, and its
 * Side tab is the Side Chat's home, exactly as on the chat page. These tests
 * pin that wiring end to end from the page's side: the pane is handed an
 * opener, the opener focuses the Side tab for the MEMBER slot (revealing the
 * overlay on a narrow window), a member switch swaps the whole strip so a Side
 * Chat stays with the member it was asked about, and an unconfirmed thread key
 * gets no Side Chat at all. */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

/* The panel's bodies: everything but the Side view renders nothing; the Side
 * view echoes the slot it is bound to, which is the assertion that matters. */
vi.mock('../chat/ActivityViewer', () => ({
  default: ({ view, slot }: { view: string; slot: string }) =>
    view === 'side' ? <div data-testid="side-chat-stub">{slot}</div> : null,
}))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))

/* The stub exposes the host-provided opener as a button, so a test can press
 * "Ask" the way the pane's selection toolbar would (the toolbar itself is the
 * pane's business — see ChatPane.selectionActions.test.tsx), and records the
 * opener's verdict, which is what decides whether the seam seeds the quote. */
const verdicts: unknown[] = []
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, openSideChat }: { slotKey: string; openSideChat?: (slot: string) => unknown }) => (
    <div data-testid="chat-pane-stub">
      {slotKey}
      {openSideChat && <button onClick={() => { verdicts.push(openSideChat(slotKey)) }}>stub-ask</button>}
    </div>
  ),
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const WIDE_WINDOW = 1440
const NARROW_WINDOW = 1000
function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

/** Tab names as the strip exposes them: pinned chips carry an aria-label,
 *  dynamic tabs (the Side tab) render their title as text -- `name` reads both. */
const tabLabels = () => screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label') ?? t.textContent?.trim() ?? '')
const sideTab = () => screen.queryByRole('tab', { name: 'Side Chat' })

async function openThread() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row()], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  renderWithProviders(<MembersPage />)
  fireEvent.click(await screen.findByText('oncall'))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
}

beforeEach(() => {
  vi.clearAllMocks()
  verdicts.length = 0
  localStorage.clear()
  __resetPanelTabs()
  setWindowWidth(WIDE_WINDOW)
})

describe('MembersPage Side Chat in the side panel (selection Ask)', () => {
  it('hands the thread pane a Side Chat opener — the pane offers Ask only because of it', async () => {
    await openThread()
    expect(screen.getByRole('button', { name: 'stub-ask' })).toBeInTheDocument()
  })

  it('Ask opens the Side tab in the docked panel, bound to the MEMBER slot, and reports the Ask as done', async () => {
    await openThread()
    await screen.findByTestId('member-crew-summary')
    expect(tabLabels()).not.toContain('Side Chat')

    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    // `true`: the seam may seed the selection into this slot's Side Chat draft.
    expect(verdicts).toEqual([true])
    await waitFor(() => expect(sideTab()).toBeInTheDocument())
    expect(sideTab()).toHaveAttribute('aria-selected', 'true')
    // Bound to the member's own thread slot — the context the question is about.
    expect(await screen.findByTestId('side-chat-stub')).toHaveTextContent('member-oncall')
  })

  it('Side Chat is offered from the + menu too: its draft lives in the chat-core store, so the panel unmounting the body loses nothing', async () => {
    await openThread()
    await screen.findByTestId('member-crew-summary')
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    await screen.findByRole('menu')
    expect(screen.getByRole('menuitem', { name: 'Side Chat' })).toBeInTheDocument()
  })

  it('narrow window: Ask reveals the overlay with the Side tab shown', async () => {
    setWindowWidth(NARROW_WINDOW)
    await openThread()
    // Overlay closed by default — the panel is not on screen.
    expect(screen.queryByTestId('member-crew-summary')).toBeNull()
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    expect(verdicts).toEqual([true])
    const overlay = await screen.findByTestId('member-side-panel')
    expect(overlay).toHaveAttribute('data-placement', 'overlay')
    await waitFor(() => expect(sideTab()).toHaveAttribute('aria-selected', 'true'))
    expect(await screen.findByTestId('side-chat-stub')).toHaveTextContent('member-oncall')
  })

  it('switching members swaps the strip — a Side Chat is about the member it was asked on', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row(), row({ name: 'fixer', slug: 'fixer' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: true }),
    )
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    await waitFor(() => expect(sideTab()).toBeInTheDocument())

    fireEvent.click(screen.getByText('fixer'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-fixer'))
    // fixer's own strip: no Side tab carried across from oncall.
    await waitFor(() => expect(tabLabels()).not.toContain('Side Chat'))
    expect(screen.queryByTestId('side-chat-stub')).toBeNull()

    // Back to oncall: the Side tab is still on ITS strip.
    fireEvent.click(screen.getByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    await waitFor(() => expect(tabLabels()).toContain('Side Chat'))
  })

  it('never offers a Side Chat on a thread key the opener rejected (slug collision)', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      // `other` claims a bound roster key the thread endpoint will NOT confirm
      // for it: the slug's thread belongs to another crew.
      members: [row(), row({ name: 'other', slug: 'other', bound: true, slot_key: 'member-other' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve(slug === 'other'
        ? { slot_key: 'member-other', slug, member: 'someone-else', created: false }
        : { slot_key: `member-${slug}`, slug, member: slug, created: true }),
    )
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    await waitFor(() => expect(sideTab()).toBeInTheDocument())

    fireEvent.click(screen.getByText('other'))
    // The collision surfaces as its own notice; no pane, so no Ask …
    await screen.findByTestId('member-thread-collision')
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // … and the strip is the slot-free bucket: only the Crew summary, no Side
    // Chat on the roster's unconfirmed `member-other` key.
    await waitFor(() => expect(tabLabels()).toEqual(['Crew summary']))
    expect(screen.queryByTestId('side-chat-stub')).toBeNull()
  })
})
