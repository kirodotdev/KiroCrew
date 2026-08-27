import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ─────────────────────────────────────────────────────
 * The page reads exactly two endpoints; mocking them keeps every case
 * network-free. MemberRosterRow is a type-only import so the mock does not
 * need to provide it. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
  },
}))

/* ChatPane is the full chat stack (WS, Redux slot machinery). The page's own
 * contract is only "mount it with the thread's slot key", so a stub that
 * ECHOES the slot key is the strongest cheap assertion available. */
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, agentLocked }: { slotKey: string; agentLocked?: boolean }) => (
    <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'}>
      {slotKey}
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

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall',
    slug: 'oncall',
    bound: false,
    slot_key: '',
    running: false,
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    model: '',
    ...overrides,
  }
}

async function renderPage(members = [row()], defaultAgent = 'kirocrew') {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members,
    default_agent: defaultAgent,
  })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({
    slot_key: 'member-oncall',
    slug: 'oncall',
    member: 'oncall',
    created: true,
  })
  const utils = renderWithProviders(<MembersPage />)
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  return utils
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('MembersPage roster', () => {
  it('renders one row per member from the API', async () => {
    await renderPage([row(), row({ name: 'research', slug: 'research' })])
    expect(await screen.findByText('oncall')).toBeInTheDocument()
    expect(screen.getByText('research')).toBeInTheDocument()
  })

  it('shows the empty state when no crews exist', async () => {
    await renderPage([])
    expect(
      await screen.findByText(/No crew members yet/i),
    ).toBeInTheDocument()
  })

  it('shows the load-failure state when the roster call rejects', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<MembersPage />)
    expect(
      await screen.findByText(/Could not load the member roster/i),
    ).toBeInTheDocument()
  })
})

describe('MembersPage thread', () => {
  it('opens the pinned DM thread on click: creates the thread, mounts the chat stack on its slot, and shows the pin chip', async () => {
    await renderPage()
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    // The stub echoes the slot key: proves ChatPane received THE member slot,
    // not a fresh ordinary slot. Mutating the mounted key breaks this line.
    const pane = await screen.findByTestId('chat-pane-stub')
    expect(pane).toHaveTextContent('member-oncall')
    // The host declares the pin: ChatPane must not offer the agent picker
    // (every selection would 409 against the server-side pin).
    expect(pane).toHaveAttribute('data-agent-locked', '1')
    expect(screen.getByTestId('member-pin-chip')).toHaveTextContent('oncall')
  })

  it('opens a bound member through the thread endpoint too — the roster binding is never mounted unverified', async () => {
    // dm.json outlives the live slot (restart drops an unmessaged slot while
    // the binding survives), so mounting the roster's slot_key directly would
    // let the first message auto-create an ordinary UNPINNED slot on the
    // member key. The idempotent POST is the only creator/repairer.
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('surfaces a visible error when thread creation fails', async () => {
    await renderPage()
    // Override AFTER renderPage, which installs the default resolved mock.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('409'))
    fireEvent.click(await screen.findByText('oncall'))
    expect(
      await screen.findByText(/Could not open this member's conversation/i),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('surfaces a slug collision instead of silently mounting another member thread', async () => {
    // Two crews folding to one slug: the endpoint attributes the thread to the
    // first-bound crew. Clicking the OTHER one must not mount that thread.
    await renderPage([
      row({ name: 'Oncall', slug: 'oncall' }),
      row({ name: 'oncall', slug: 'oncall' }),
    ])
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({
      slot_key: 'member-oncall',
      slug: 'oncall',
      member: 'Oncall',
      created: false,
    })
    fireEvent.click(await screen.findByText('oncall'))
    expect(await screen.findByText(/shares its identifier with/i)).toBeInTheDocument()
    // The misrouted thread is NOT mounted — that is the entire point.
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('keeps a late failure of a previously selected member out of the active view', async () => {
    let rejectA: (e: Error) => void = () => {}
    const pendingA = new Promise((_, reject) => {
      rejectA = reject
    })
    await renderPage([
      row({ name: 'alpha', slug: 'alpha' }),
      row({ name: 'beta', slug: 'beta' }),
    ])
    ;(api.memberThread as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(pendingA)
      .mockResolvedValueOnce({
        slot_key: 'member-beta',
        slug: 'beta',
        member: 'beta',
        created: true,
      })
    fireEvent.click(await screen.findByText('alpha'))
    fireEvent.click(screen.getByText('beta'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
    rejectA(new Error('late'))
    // The stale rejection lands in alpha's bucket; beta's view stays clean.
    await waitFor(() =>
      expect(screen.queryByText(/Could not open this member's conversation/i)).toBeNull(),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
  })
})

describe('MembersPage drawer and edit jump', () => {
  it('shows the read-only config summary and the shared-memory disclosure', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall', model: 'claude-opus-5' })])
    fireEvent.click(await screen.findByText('oncall'))
    const drawer = await screen.findByTestId('member-drawer')
    expect(drawer).toHaveTextContent('kirocrew')
    expect(drawer).toHaveTextContent('claude-opus-5')
    expect(drawer).toHaveTextContent(/share one memory/i)
  })

  it('toggles the drawer via the Details button', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    expect(await screen.findByTestId('member-drawer')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /details/i }))
    expect(screen.queryByTestId('member-drawer')).toBeNull()
  })

  it('both edit affordances navigate to the crew manager crews tab, never an inline editor', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    fireEvent.click(await screen.findByTestId('member-edit-jump'))
    // Mutation check: the assertion is on the DESTINATION (explicit ?tab=crews
    // beats CapabilitiesPage's remembered last tab), so retargeting the jump
    // anywhere else fails here.
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews')
    navigateSpy.mockClear()
    // Header and drawer share the SAME explicit label ("Edit in crew
    // manager") — a bare "Edit" verb navigating away was a UX finding. Both
    // must route to the manager.
    for (const btn of screen.getAllByRole('button', { name: /edit in crew manager/i })) {
      fireEvent.click(btn)
      expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews')
      navigateSpy.mockClear()
    }
  })
})
