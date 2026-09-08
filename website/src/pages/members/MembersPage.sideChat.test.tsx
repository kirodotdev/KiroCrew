import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { writeSideChatDraft } from '../../chat-core/composer/sideChatDrafts'

/* The Members page has no activity panel — the chat page's home for the Side
 * Chat — so the selection toolbar's "Ask" on a member thread needs a surface
 * of its own: the detail drawer, switched to a Side Chat view bound to the
 * member's slot. These tests pin that wiring end to end from the page's side:
 * the pane is handed an opener, the opener puts the member's Side Chat in the
 * drawer, and the drawer's Details action / close find their way back. */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    // Same defaults as MembersPage.test.tsx: the drawer's wake block reads
    // the default crew, the auto-patrol block reads the loop registry.
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

/* The stub exposes the host-provided opener as a button, so a test can press
 * "Ask" the way the pane's selection toolbar would (the toolbar itself is the
 * pane's business — see ChatPane.selectionActions.test.tsx). */
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, openSideChat }: { slotKey: string; openSideChat?: (slot: string) => void }) => (
    <div data-testid="chat-pane-stub">
      {slotKey}
      {openSideChat && <button onClick={() => openSideChat(slotKey)}>stub-ask</button>}
    </div>
  ),
}))
vi.mock('../chat/SideChat', () => ({
  default: ({ slot }: { slot: string }) => <div data-testid="side-chat-stub">{slot}</div>,
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

async function openThread() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row()], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  // Wide viewport: the drawer starts open on details, as it does on desktop.
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((q: string) => ({ matches: q.includes('min-width'), addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })),
  })
  renderWithProviders(<MembersPage />)
  fireEvent.click(await screen.findByText('oncall'))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
}

beforeEach(() => { vi.clearAllMocks() })

describe('MembersPage Side Chat drawer (selection Ask)', () => {
  it('hands the thread pane a Side Chat opener — the pane offers Ask only because of it', async () => {
    await openThread()
    expect(screen.getByRole('button', { name: 'stub-ask' })).toBeInTheDocument()
  })

  it('Ask swaps the drawer to the Side Chat for the MEMBER slot, and Details brings the details back', async () => {
    await openThread()
    expect(screen.getByTestId('member-drawer')).toBeInTheDocument()
    expect(screen.queryByTestId('member-side-chat')).toBeNull()

    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    const side = await screen.findByTestId('member-side-chat')
    // Bound to the member's own thread slot — the context the question is about.
    expect(side.querySelector('[data-testid="side-chat-stub"]')).toHaveTextContent('member-oncall')
    // The views crossfade (mode="wait"), so the outgoing details leave a beat later.
    await waitFor(() => expect(screen.queryByTestId('member-drawer')).toBeNull())
    // The header names the view; the avatar keeps the identity.
    expect(screen.getByText('Side Chat')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('member-drawer-details'))
    await waitFor(() => expect(screen.getByTestId('member-drawer')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('member-side-chat')).toBeNull())
  })

  it('the details view offers a way back into the Side Chat only while the member holds an unsent draft', async () => {
    await openThread()
    // No draft → no way in except the toolbar's Ask.
    expect(screen.queryByTestId('member-drawer-side-chat')).toBeNull()
    // A draft appears (typed in the Side Chat, then the user went to Details).
    act(() => { writeSideChatDraft('member-oncall', 'half a question') })
    const back = await screen.findByTestId('member-drawer-side-chat')
    expect(back).toHaveTextContent('Side Chat')
    // It appears out of nowhere for a user who left mid-question, so it says
    // why it is there.
    expect(back).toHaveAttribute('title', expect.stringContaining('unsent question'))
    expect(back).toHaveAccessibleDescription(/unsent question/)
    fireEvent.click(back)
    await screen.findByTestId('member-side-chat')
    // Draft cleared (sent) → the entry disappears again.
    fireEvent.click(screen.getByTestId('member-drawer-details'))
    await waitFor(() => expect(screen.getByTestId('member-drawer')).toBeInTheDocument())
    act(() => { writeSideChatDraft('member-oncall', '') })
    await waitFor(() => expect(screen.queryByTestId('member-drawer-side-chat')).toBeNull())
  })

  it('closing the drawer forgets the Side Chat view: the Details toggle reopens details', async () => {
    await openThread()
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    await screen.findByTestId('member-side-chat')

    // Header toggle closes the open drawer …
    fireEvent.click(screen.getByTestId('member-drawer-toggle'))
    await waitFor(() => expect(screen.queryByTestId('member-side-chat')).toBeNull())
    // … and reopens it on details, not on the dismissed Side Chat.
    fireEvent.click(screen.getByTestId('member-drawer-toggle'))
    await waitFor(() => expect(screen.getByTestId('member-drawer')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('member-side-chat')).toBeNull())
  })

  it('switching members returns the drawer to details — a Side Chat is about the member it was asked on', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row(), row({ name: 'fixer', slug: 'fixer' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: true }),
    )
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: q.includes('min-width'), addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })),
    })
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    await screen.findByTestId('member-side-chat')

    fireEvent.click(screen.getByText('fixer'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-fixer'))
    // Details for fixer, not oncall's Side Chat carried across.
    await waitFor(() => expect(screen.getByTestId('member-drawer')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('member-side-chat')).toBeNull())
  })

  it('never mounts a Side Chat on a thread key the opener rejected (slug collision)', async () => {
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
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: q.includes('min-width'), addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })),
    })
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'stub-ask' })) })
    await screen.findByTestId('member-side-chat')

    fireEvent.click(screen.getByText('other'))
    // The collision surfaces as its own notice; no pane, so no Ask …
    await screen.findByTestId('member-thread-collision')
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // … and no Side Chat on the roster's unconfirmed `member-other` key either.
    // (waitFor: the previous member's Side Chat is still crossfading out.)
    await waitFor(() => expect(screen.queryByTestId('member-side-chat')).toBeNull())
    expect(screen.queryByTestId('side-chat-stub')).toBeNull()
  })
})
