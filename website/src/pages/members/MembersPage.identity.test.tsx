import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* The id / display_name split (design: Crew Member = Custom Agent + Wrapper,
 * rollout step 1). `name` on a roster row is the member's ID — the key every
 * route addresses — and `display_name` is what a person reads. These tests pin
 * that every surface on the page renders the LABEL while the id keeps doing
 * its job as the address (URL param, thread pin, avatar seed). */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    updateKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'
import { memberLabel, narrowRoster, sortRoster } from './rosterFilter'

function row(name: string, overrides: Record<string, unknown> = {}) {
  return {
    name,
    slug: name,
    slot_key: '',
    running: false,
    kiro_agent: name,
    workspace: 'default',
    memory_store: 'default',
    model: '',
    source: 'kirocrew',
    starred: false,
    display_name: name,
    role: '',
    ...overrides,
  }
}

/** The incident row after migration: id `case-competition`, label as typed. */
const MIGRATED = row('case-competition', { display_name: 'case competition' })
const HIRED = row('triage', {
  display_name: 'Checkout triage',
  role: 'Oncall Triage Engineer',
})
const SHIPPED = row('default', { source: 'builtin' })

async function renderPage(members = [MIGRATED, HIRED, SHIPPED], search = '') {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members })
  const utils = renderWithProviders(<MembersPage />, { route: '/members' + search })
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  await screen.findByTestId('member-roster')
  return utils
}

const NO_SIGNALS = () => ({ running: false, needsYou: false, unread: false, patrolling: false })

/** Wide enough to dock the side panel BESIDE the thread (see the page's
 *  panelSitsBeside); happy-dom's default puts it in closed overlay mode. */
const WIDE_WINDOW = 1440

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  Object.defineProperty(window, 'innerWidth', { value: WIDE_WINDOW, configurable: true, writable: true })
})

describe('memberLabel + roster model', () => {
  it('reads the display name and falls back to the id for a pre-split row', () => {
    expect(memberLabel({ name: 'case-competition', display_name: 'case competition' })).toBe('case competition')
    expect(memberLabel({ name: 'triage' })).toBe('triage')
    expect(memberLabel({ name: 'triage', display_name: '' })).toBe('triage')
  })

  it('sorts by label, not by id', () => {
    const a = row('zzz', { display_name: 'Alpha' })
    const b = row('aaa', { display_name: 'Zulu' })
    expect(sortRoster([b, a], 'name').map((m) => m.name)).toEqual(['zzz', 'aaa'])
  })

  it('search matches the label, the id and the role', () => {
    const q = (search: string) =>
      narrowRoster([MIGRATED, HIRED, SHIPPED], { search, starredOnly: false, source: 'all', status: new Set() }, NO_SIGNALS).map(
        (m) => m.name,
      )
    expect(q('checkout')).toEqual(['triage']) // label
    expect(q('triage')).toEqual(['triage']) // id and role both hit
    expect(q('oncall')).toEqual(['triage']) // role only
    expect(q('case comp')).toEqual(['case-competition']) // typed label with a space
  })
})

describe('MembersPage renders identity', () => {
  it('gate: the migrated member is listed under its original display name', async () => {
    await renderPage()
    const roster = screen.getByTestId('member-roster')
    const labels = within(roster).getAllByTestId('member-row-label').map((el) => el.textContent)
    expect(labels.some((l) => l?.startsWith('case competition'))).toBe(true)
    // The id is the address, never the roster text.
    expect(within(roster).queryByText('case-competition')).toBeNull()
  })

  it('shows the role beside the label and never invents one', async () => {
    await renderPage()
    const roster = screen.getByTestId('member-roster')
    const roles = within(roster).getAllByTestId('member-row-role').map((el) => el.textContent)
    expect(roles).toHaveLength(1)
    expect(roles[0]).toContain('Oncall Triage Engineer')
  })

  it('addresses the open member by id while the header wears the label', async () => {
    await renderPage([MIGRATED, HIRED, SHIPPED], '?member=triage')
    expect(await screen.findByTestId('member-header-label')).toHaveTextContent('Checkout triage')
    expect(screen.getByTestId('member-header-role')).toHaveTextContent('Oncall Triage Engineer')
    // The thread pins to the ID: the slot key derives from it, not the label.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('triage'))
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-triage')
  })

  it('drawer: id, role and provenance rows', async () => {
    await renderPage([MIGRATED, HIRED, SHIPPED], '?member=triage')
    expect(await screen.findByTestId('member-summary-label')).toHaveTextContent('Checkout triage')
    expect(screen.getByTestId('member-summary-role')).toHaveTextContent('Oncall Triage Engineer')
    expect(screen.getByTestId('member-config-id')).toHaveTextContent('triage')
    expect(screen.getByTestId('member-config-role')).toHaveTextContent('Oncall Triage Engineer')
    expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Created here')
  })

  it('drawer: a hand-made member reads as created here with no role', async () => {
    await renderPage([MIGRATED, HIRED, SHIPPED], '?member=case-competition')
    expect(await screen.findByTestId('member-config-id')).toHaveTextContent('case-competition')
    expect(screen.getByTestId('member-config-role')).toHaveTextContent('None')
    expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Created here')
    expect(screen.queryByTestId('member-summary-role')).toBeNull()
  })

  it('drawer: a shipped member reads as built-in', async () => {
    await renderPage([MIGRATED, HIRED, SHIPPED], '?member=default')
    expect(await screen.findByTestId('member-config-provenance')).toHaveTextContent('Built-in')
  })
})

describe('MembersPage Source row reads the normalized source', () => {
  it('a package-installed member reads as from packages, never as created here', async () => {
    await renderPage([row('pkg-a', { source: 'package' })], '?member=pkg-a')
    expect(await screen.findByTestId('member-config-provenance')).toHaveTextContent('From packages')
  })
})
