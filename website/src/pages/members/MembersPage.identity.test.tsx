import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
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
    listApps: vi.fn(() => Promise.resolve([])),
    fireMember: vi.fn(),
    memberRoleUpdatePlan: vi.fn(() => Promise.resolve({ member: '', template: '', member_version: '1.2.0', installed_version: '1.2.0', update_available: false, member_fingerprint: 'x', template_fingerprint: 'y', fields: [] })),
    detachMember: vi.fn(() => Promise.resolve({ ok: true })),
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

  it('drawer: a member bound to its own copy names the template it came from', async () => {
    // Copy-on-hire binds `kiro_agent` to the copy's stem (= the id). Read as a
    // template name that is one answer; the editor's "reviewer (Customized)" is
    // another. The drawer therefore says what the copy is OF.
    const hired = row('triage', { display_name: 'Checkout triage', kiro_agent: 'triage', template_origin: 'reviewer' })
    await renderPage([hired, SHIPPED], '?member=triage')
    expect(await screen.findByTestId('member-config-template')).toHaveTextContent('reviewer — customized copy')
    expect(screen.getByTestId('member-config-template')).not.toHaveTextContent(/^triage$/)
  })

  it('drawer: a member bound to a shared template shows the template itself', async () => {
    await renderPage([MIGRATED, SHIPPED], '?member=case-competition')
    expect(await screen.findByTestId('member-config-template')).toHaveTextContent('case-competition')
  })
})

describe('MembersPage fire (design step 5)', () => {
  it('withholds the verb for the default member and, after a fire, says where the thread went from the roster', async () => {
    const dflt = row('default')
    vi.mocked(api.defaultAgent).mockResolvedValue({ default_agent: 'default' })
    vi.mocked(api.fireMember).mockImplementation(async () => {
      // The server's roster no longer lists the fired member.
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [MIGRATED, dflt] })
      return {
        ok: true,
        thread: { state: 'archived' },
        lived_state: 'archived',
      }
    })
    await renderPage([MIGRATED, HIRED, dflt], '?member=default')
    await screen.findByTestId('member-config-id')
    expect(screen.queryByTestId('member-fire')).toBeNull()
    // The hired member can be fired.
    fireEvent.click(screen.getByText('Checkout triage'))
    await waitFor(() => expect(screen.getByTestId('member-config-id')).toHaveTextContent('triage'))
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('triage', { purge: false }))
    // The row is gone with the drawer; the roster carries the outcome until dismissed.
    const notice = await screen.findByTestId('member-fired-notice')
    expect(notice).toHaveTextContent(
      'Checkout triage has been fired. Their conversation, activity, briefing and rules are archived; the conversation still opens from History on the Chat page.',
    )
    // "History" is not on this surface: the word is a link to the pane that holds it.
    const history = within(notice).getByTestId('member-fired-history-link')
    expect(history).toHaveTextContent('History')
    expect(history).toHaveAttribute('href', '/chat?history=1')
    expect(navigateSpy).toHaveBeenCalledWith('/members')
    // One voice: the gone-member fallback stays quiet for the member the fired
    // notice already speaks for (it would otherwise say the same thing again,
    // by id: "'triage' is no longer on the roster").
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    // The fired row left the roster with the drawer -- evicted from the cache
    // before the navigation, not a beat later when the refetch lands.
    const labels = within(screen.getByTestId('member-roster')).getAllByTestId('member-row-label').map((el) => el.textContent)
    expect(labels.some((l) => l?.includes('Checkout triage'))).toBe(false)
    fireEvent.click(screen.getByTestId('member-fired-dismiss'))
    expect(screen.queryByTestId('member-fired-notice')).toBeNull()
  })

  it('a thread the purge could not delete says why, and the reason is the one the server gave', async () => {
    vi.mocked(api.fireMember).mockResolvedValue({
      ok: true,
      thread: { state: 'kept', kept_reason: 'cron_claim_unreadable' },
      lived_state: 'purged',
    })
    await renderPage([MIGRATED, HIRED], '?member=triage')
    await screen.findByTestId('member-config-id')
    fireEvent.click(screen.getByTestId('member-fire-start'))
    fireEvent.click(screen.getByTestId('member-fire-purge'))
    fireEvent.click(screen.getByTestId('confirm-fire-member'))
    await waitFor(() => expect(api.fireMember).toHaveBeenCalledWith('triage', { purge: true }))
    const notice = await screen.findByTestId('member-fired-notice')
    expect(notice).toHaveTextContent('a scheduled job still refers to it')
    expect(notice).not.toHaveTextContent('may own it')
    expect(within(notice).getByTestId('member-fired-history-link')).toHaveAttribute('href', '/chat?history=1')
  })
})

describe('MembersPage Source row reads the normalized source', () => {
  it('a package-installed member reads as from packages, never as created here', async () => {
    await renderPage([row('pkg-a', { source: 'package' })], '?member=pkg-a')
    expect(await screen.findByTestId('member-config-provenance')).toHaveTextContent('From packages')
  })

  it('a member hired from an app template names the app the way the hire picker did', async () => {
    // The picker offered "Oncall pack"; the drawer must not answer "oncall-pack"
    // for the same app (#10596 UX review). The template's agent and version stay.
    vi.mocked(api.listApps).mockResolvedValue([
      { name: 'oncall-pack', version: '1.2.0', enabled: true, manifest: { name: 'oncall-pack', version: '1.2.0', displayName: 'Oncall pack', description: '', author: '' } },
    ] as never)
    const hired = row('Pager-triage', { display_name: 'Pager triage', template: 'oncall-pack/triage', template_version: '1.2.0' })
    await renderPage([hired], '?member=Pager-triage')
    await waitFor(() =>
      expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Hired from Oncall pack — “triage” (v1.2.0)'),
    )
  })

  it('a detach is not silent: the drawer says so where the panel stood, and the template row says what happened to the pair', async () => {
    vi.mocked(api.listApps).mockResolvedValue([
      { name: 'oncall-pack', version: '1.2.0', enabled: true, manifest: { name: 'oncall-pack', version: '1.2.0', displayName: 'Oncall pack', description: '', author: '' } },
    ] as never)
    const hired = row('Pager-triage', { display_name: 'Pager triage', template: 'oncall-pack/triage', template_version: '1.2.0', template_origin: 'triage' })
    await renderPage([hired], '?member=Pager-triage')
    await waitFor(() => expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Hired from Oncall pack'))
    expect(screen.getByTestId('member-config-template')).toHaveTextContent('triage — customized copy')
    // After the detach the roster refetch answers a row with no template: the
    // panel unmounts, and the closure + the "(detached from …)" reading take over.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [{ ...hired, template: '', template_version: '' }] })
    fireEvent.click(await screen.findByTestId('member-detach'))
    fireEvent.click(screen.getByTestId('confirm-detach-member'))
    await waitFor(() => expect(api.detachMember).toHaveBeenCalledWith('Pager-triage'))
    const notice = await screen.findByTestId('member-detached-notice')
    expect(notice).toHaveTextContent('Detached from Oncall pack. This crewmate keeps everything it has and no longer follows that template.')
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Created here')
    expect(screen.getByTestId('member-config-template')).toHaveTextContent('triage — customized copy (detached from Oncall pack)')
    expect(screen.queryByTestId('member-detach')).toBeNull()
  })

  it('drops the version parenthetical when the row records no version', async () => {
    // "(v?)" told the reader nothing; the sentence just ends at the app.
    vi.mocked(api.listApps).mockResolvedValue([
      { name: 'oncall-pack', version: '1.2.0', enabled: true, manifest: { name: 'oncall-pack', version: '1.2.0', displayName: 'Oncall pack', description: '', author: '' } },
    ] as never)
    const hired = row('Pager-triage', { display_name: 'Pager triage', template: 'oncall-pack/triage', template_version: '' })
    await renderPage([hired], '?member=Pager-triage')
    await waitFor(() =>
      expect(screen.getByTestId('member-config-provenance')).toHaveTextContent('Hired from Oncall pack — “triage”'),
    )
    expect(screen.getByTestId('member-config-provenance')).not.toHaveTextContent('(v')
  })

  it('falls back to the app id when the app is gone, with no notice', async () => {
    vi.mocked(api.listApps).mockResolvedValue([] as never)
    const hired = row('Pager-triage', { display_name: 'Pager triage', template: 'oncall-pack/triage', template_version: '1.2.0' })
    await renderPage([hired], '?member=Pager-triage')
    expect(await screen.findByTestId('member-config-provenance')).toHaveTextContent('Hired from oncall-pack — “triage” (v1.2.0)')
    expect(screen.queryByTestId('member-config-provenance-error')).toBeNull()
  })

  it('says so when the app list could not be read, instead of passing the id off as the name', async () => {
    // The row still says something (the id); the failed read is an ErrorNotice
    // with the agent hand-off, not a silent substitution.
    vi.mocked(api.listApps).mockRejectedValue(new Error('boom'))
    const hired = row('Pager-triage', { display_name: 'Pager triage', template: 'oncall-pack/triage', template_version: '1.2.0' })
    await renderPage([hired], '?member=Pager-triage')
    expect(await screen.findByTestId('member-config-provenance')).toHaveTextContent('Hired from oncall-pack — “triage” (v1.2.0)')
    const notice = await screen.findByTestId('member-config-provenance-error')
    expect(notice).toHaveTextContent("The installed apps could not be read, so the template's app is shown by its id. Nothing is lost; reload to retry.")
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })
})
