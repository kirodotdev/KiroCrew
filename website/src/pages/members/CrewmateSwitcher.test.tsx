import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import type { MemberRosterRow } from '../../api/client'
import type { MemberSignals } from './rosterFilter'

/* The crewmate switcher (crewmate-panel IA): the roster column folded into one
 * header chip. Closed it stacks the first faces and a chevron, the roster's
 * name sliding in on hover; open it is a searchable list of every crewmate
 * with the current one marked, and a "New crewmate" footer. It only REPORTS a
 * pick — the page owns the URL — so these cases pin the chip's reading, the
 * search, the pick, and the create door — and, since the folded roster is the
 * desktop norm, the live signals the column used to carry: each row's working
 * / needs-you / unread cue, the chip's own needs-you dot for a crewmate other
 * than the open one, and `CrewmateUnreadColumn` (issue #19045) -- the page
 * draws it under the chip: one pill per unread crewmate, capped, each opening
 * that thread, the "+N" pill asking the page to open the chip's list.
 */

vi.mock('../../components/CrewStateAvatar', () => ({
  default: ({ seed }: { seed: string }) => <span data-testid="avatar-stub">{seed}</span>,
}))

import CrewmateSwitcher, { CrewmateUnreadColumn } from './CrewmateSwitcher'

const row = (name: string, overrides: Partial<MemberRosterRow> = {}): MemberRosterRow => ({
  name, slug: name.toLowerCase().replace(/\s+/g, '-'), bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
  ...overrides,
} as MemberRosterRow)

const MEMBERS = [
  row('oncall', { kiro_agent: 'kirocrew-oncall' }),
  row('scribe', { running: true }),
  row('Radar One', { display_name: 'Radar One', kiro_agent: 'radar-template', last_message: 'Swept the board' } as Partial<MemberRosterRow>),
  row('fixer'),
]

function setup(props: Partial<Parameters<typeof CrewmateSwitcher>[0]> = {}) {
  const onPick = vi.fn()
  const onCreate = vi.fn()
  render(<CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={onPick} onCreate={onCreate} {...props} />)
  return { onPick, onCreate }
}

const chip = () => screen.getByTestId('crewmate-switcher')
const open = async () => {
  fireEvent.click(chip())
  return await screen.findByTestId('crewmate-switcher-list')
}
const options = (list: HTMLElement) => within(list).getAllByRole('option')

describe('CrewmateSwitcher closed chip', () => {
  it('stacks the OTHER mates\' faces in roster order; no count, the roster word folded until hover', () => {
    setup()
    const faces = within(chip()).getAllByTestId('avatar-stub').map((f) => f.textContent)
    // Three faces at most, none of them the open crewmate: the identity pill
    // beside the chip already shows that face, and a copy here read as a
    // duplicate rather than as "the rest of the crew".
    expect(faces).toEqual(['oncall', 'Radar One', 'fixer'])
    // No roster count: how big the crew is was not what a reader came to
    // this corner for (issue #19045).
    expect(screen.queryByTestId('crewmate-switcher-count')).toBeNull()
    // The roster word is in the DOM but folded to a zero-width column at
    // rest; it slides open on hover, focus or while the list is open. The
    // button is named by aria-label, so the fold changes nothing spoken.
    const label = screen.getByTestId('crewmate-switcher-label')
    expect(label).toHaveTextContent('Mates')
    expect(label.className).toContain('grid-cols-[0fr]')
    expect(label.className).toContain('group-hover:grid-cols-[1fr]')
    expect(label.className).toContain('group-focus-visible:grid-cols-[1fr]')
    expect(label.className).toContain('group-data-[state=open]:grid-cols-[1fr]')
    expect(label.className).toContain('motion-reduce:transition-none')
    expect((label.firstElementChild as HTMLElement).className).toContain('overflow-hidden')
    expect(chip()).toHaveAccessibleName('Mates')
    expect(chip()).toHaveAttribute('title', 'Switch mate')
    expect(chip()).toHaveAttribute('aria-expanded', 'false')
  })

  it('falls back to the open mate\'s own face when it is the whole roster', () => {
    setup({ members: [MEMBERS[1]] })
    const faces = within(chip()).getAllByTestId('avatar-stub').map((f) => f.textContent)
    expect(faces).toEqual(['scribe'])
  })
})

describe('CrewmateSwitcher roster action', () => {
  it('offers to show the full roster and closes the list on pick', async () => {
    const onShowRoster = vi.fn()
    render(<CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} onShowRoster={onShowRoster} />)
    const list = await open()
    const action = within(list).getByTestId('crewmate-switcher-roster')
    expect(action).toHaveTextContent('Show the full roster')
    fireEvent.click(action)
    expect(onShowRoster).toHaveBeenCalledTimes(1)
    // Picking the action closes the list, like every other footer action.
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())
  })

  it('draws no roster action when the page passes no handler', async () => {
    setup()
    const list = await open()
    expect(within(list).queryByTestId('crewmate-switcher-roster')).toBeNull()
  })
})

describe('CrewmateSwitcher open list', () => {
  it('lists every mate with the current one selected', async () => {
    setup()
    const list = await open()
    expect(chip()).toHaveAttribute('aria-expanded', 'true')
    const opts = options(list)
    expect(opts.map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['oncall', 'scribe', 'Radar One', 'fixer'])
    expect(opts.map((o) => o.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false', 'false'])
    expect(opts[1]).toHaveAttribute('data-active', 'true')
    // The second line: Working for a running crewmate, else its agent or last message.
    expect(opts[1]).toHaveTextContent('Working')
    expect(opts[0]).toHaveTextContent('kirocrew-oncall')
  })

  it('filters by display name, slug-ish name, and agent template; says when nothing matches', async () => {
    setup()
    const list = await open()
    const search = screen.getByTestId('crewmate-switcher-search')
    expect(search).toHaveAccessibleName('Find a mate')

    fireEvent.change(search, { target: { value: 'radar' } })
    // Matches the display name AND the agent template ("radar-template").
    expect(options(list).map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['Radar One'])

    fireEvent.change(search, { target: { value: 'FIX' } })
    expect(options(list).map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['fixer'])

    fireEvent.change(search, { target: { value: 'nobody' } })
    expect(within(list).queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByTestId('crewmate-switcher-empty')).toHaveTextContent('No mate matches')
    // The create door is still offered under an empty result.
    expect(screen.getByTestId('crewmate-switcher-create')).toBeInTheDocument()
  })

  it('runs the column\'s hide rule: an unchatted app row is neither listed nor counted, and the search still reaches it', async () => {
    // `has_dm_message: false` + `dashboard_created: false` is exactly what the
    // column hides -- a row created by sync or an app that nobody has chatted
    // with. The chip used to list it, so the two disagreed about the crew.
    const hidden = row('ghostwriter', { kiro_agent: 'kirocrew-ghost', has_dm_message: false, dashboard_created: false } as Partial<MemberRosterRow>)
    setup({ members: [...MEMBERS, hidden] })
    const list = await open()
    expect(options(list).map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['oncall', 'scribe', 'Radar One', 'fixer'])

    fireEvent.change(screen.getByTestId('crewmate-switcher-search'), { target: { value: 'ghost' } })
    expect(options(list).map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['ghostwriter'])
  })

  it('keeps the OPEN mate listed even when the hide rule would drop it', async () => {
    const hidden = row('ghostwriter', { has_dm_message: false, dashboard_created: false } as Partial<MemberRosterRow>)
    setup({ members: [...MEMBERS, hidden], activeName: 'ghostwriter' })
    const list = await open()
    const opts = options(list)
    expect(opts.map((o) => within(o).getByTestId('avatar-stub').textContent)).toEqual(['oncall', 'scribe', 'Radar One', 'fixer', 'ghostwriter'])
    expect(opts[4]).toHaveAttribute('aria-selected', 'true')
  })

  it('reports a pick by name and closes; picking the current mate reports nothing', async () => {
    const { onPick } = setup()
    let list = await open()
    fireEvent.click(options(list)[1]) // scribe, already open
    expect(onPick).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())

    list = await open()
    fireEvent.click(options(list)[3])
    expect(onPick).toHaveBeenCalledWith('fixer')
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())
    expect(chip()).toHaveAttribute('aria-expanded', 'false')
  })

  it('forgets the search when the list closes and reopens', async () => {
    setup()
    let list = await open()
    fireEvent.change(screen.getByTestId('crewmate-switcher-search'), { target: { value: 'fix' } })
    expect(options(list)).toHaveLength(1)
    fireEvent.click(options(list)[0])
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())
    list = await open()
    expect(screen.getByTestId('crewmate-switcher-search')).toHaveValue('')
    expect(options(list)).toHaveLength(4)
  })

  it('the New mate footer opens the create dialog and closes the list', async () => {
    const { onCreate, onPick } = setup()
    await open()
    fireEvent.click(screen.getByTestId('crewmate-switcher-create'))
    expect(onCreate).toHaveBeenCalledTimes(1)
    expect(onPick).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())
  })

  it('offers no create door while creation is held', async () => {
    setup({ onCreate: undefined })
    await open()
    expect(screen.queryByTestId('crewmate-switcher-create')).toBeNull()
  })
})

const QUIET: MemberSignals = { running: false, needsYou: false, unread: false, patrolling: false }
/** The page's resolver, as a map by name: anyone not listed is quiet. */
const signalsBy = (by: Record<string, Partial<MemberSignals>>) => (m: MemberRosterRow): MemberSignals => ({ ...QUIET, ...by[m.name] })

describe('CrewmateSwitcher live signals', () => {
  it('each row carries the roster cues — presence while working, a named needs-you dot, a named unread dot', async () => {
    setup({ signals: signalsBy({ oncall: { needsYou: true }, scribe: { running: true }, fixer: { unread: true } }) })
    const list = await open()
    const [oncall, scribe, radar, fixer] = options(list)

    // Needs-you: a warn dot on the right edge, spoken in the row's name, with
    // the state WRITTEN beside it — colour alone is not a state a reader can be
    // sure of. The word is decorative (the named dot already carries the fuller
    // sentence), so the name says it once.
    expect(within(oncall).getByTestId('crewmate-switcher-needs-you-dot')).toHaveAccessibleName('Needs your approval or answer')
    expect(within(oncall).getByTestId('crewmate-switcher-row-needs-you')).toHaveTextContent('Needs you')
    expect(oncall).toHaveAccessibleName(/oncall.*Needs your approval or answer/)
    expect(oncall).not.toHaveAccessibleName(/Needs you\b.*Needs you/)
    expect(within(oncall).queryByTestId('crewmate-switcher-unread-dot')).toBeNull()
    expect(within(oncall).queryByTestId('crewmate-switcher-presence-dot')).toBeNull()

    // Working: the presence dot on the face and the Working line (which is what
    // names the state), no right-edge marker.
    expect(within(scribe).getByTestId('crewmate-switcher-presence-dot')).toBeInTheDocument()
    expect(scribe).toHaveAccessibleName(/scribe.*Working/)
    expect(within(scribe).queryByTestId('crewmate-switcher-needs-you-dot')).toBeNull()

    // Unread: the accent dot, spoken, and written.
    expect(within(fixer).getByTestId('crewmate-switcher-unread-dot')).toHaveAccessibleName('Unread message')
    expect(within(fixer).getByTestId('crewmate-switcher-unread')).toHaveTextContent('Unread')
    expect(fixer).toHaveAccessibleName(/fixer.*Unread message/)
    expect(fixer).not.toHaveAccessibleName(/Unread.*Unread/)

    // A quiet crewmate shows no marker at all — idle is the resting state.
    expect(within(radar).queryByTestId('crewmate-switcher-presence-dot')).toBeNull()
    expect(within(radar).queryByTestId('crewmate-switcher-needs-you-dot')).toBeNull()
    expect(within(radar).queryByTestId('crewmate-switcher-unread-dot')).toBeNull()
  })

  it('a row parked on you AND unread shows both markers, needs-you first', async () => {
    setup({ signals: signalsBy({ fixer: { needsYou: true, unread: true } }) })
    const list = await open()
    const fixer = options(list)[3]
    const marks = within(fixer).getAllByRole('img').map((el) => el.getAttribute('data-testid'))
    expect(marks).toEqual(['crewmate-switcher-needs-you-dot', 'crewmate-switcher-unread-dot'])
  })

  it('the row signals come from the resolver, not the roster row: a stale running flag defers to it', async () => {
    // The roster row says scribe is running; the live resolver says it is not.
    setup({ signals: signalsBy({}) })
    const list = await open()
    const scribe = options(list)[1]
    expect(within(scribe).queryByTestId('crewmate-switcher-presence-dot')).toBeNull()
    expect(scribe).not.toHaveTextContent('Working')
  })

  it('without a resolver a row falls back to its own running flag', async () => {
    setup()
    const list = await open()
    expect(within(options(list)[1]).getByTestId('crewmate-switcher-presence-dot')).toBeInTheDocument()
    expect(chip()).toHaveAccessibleName('Mates')
    expect(screen.queryByTestId('crewmate-switcher-needs-you')).toBeNull()
  })

  it('the closed chip shows a needs-you dot, and says so, only when a mate OTHER than the open one needs you', () => {
    const { unmount } = render(
      <CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} signals={signalsBy({ oncall: { needsYou: true } })} />,
    )
    expect(screen.getByTestId('crewmate-switcher-needs-you')).toBeInTheDocument()
    expect(chip()).toHaveAttribute('data-needs-you', 'true')
    expect(chip()).toHaveAccessibleName('Mates, Needs your approval or answer')
    unmount()

    // Only the OPEN crewmate needs you: its thread is on screen, so the chip
    // stays quiet.
    render(
      <CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} signals={signalsBy({ scribe: { needsYou: true } })} />,
    )
    expect(screen.queryByTestId('crewmate-switcher-needs-you')).toBeNull()
    expect(chip()).not.toHaveAttribute('data-needs-you')
    expect(chip()).toHaveAccessibleName('Mates')
  })

  it('working and unread elsewhere do not light the chip — only needs-you does', () => {
    render(
      <CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} signals={signalsBy({ oncall: { running: true, unread: true } })} />,
    )
    expect(screen.queryByTestId('crewmate-switcher-needs-you')).toBeNull()
    expect(chip()).toHaveAccessibleName('Mates')
  })
})

describe('CrewmateSwitcher opens on an outside request', () => {
  it('a bumped openRequest opens the list; the mount value does not', async () => {
    const { rerender } = render(<CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} openRequest={3} />)
    expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull()
    rerender(<CrewmateSwitcher members={MEMBERS} activeName="scribe" onPick={vi.fn()} openRequest={4} />)
    expect(await screen.findByTestId('crewmate-switcher-list')).toBeInTheDocument()
  })
})

describe('CrewmateUnreadColumn', () => {
  const pills = () => screen.queryAllByTestId('crewmate-switcher-unread-pill')
  const column = (props: Partial<Parameters<typeof CrewmateUnreadColumn>[0]> = {}) => {
    const onPick = vi.fn()
    const onMore = vi.fn()
    const view = render(<CrewmateUnreadColumn members={MEMBERS} activeName="scribe" onPick={onPick} onMore={onMore} {...props} />)
    return { onPick, onMore, ...view }
  }

  it('one named pill per unread mate other than the open one, in roster order; a click picks it', () => {
    const { onPick } = column({ signals: signalsBy({ fixer: { unread: true }, oncall: { unread: true } }) })
    expect(screen.getByTestId('crewmate-switcher-unread-column')).toHaveAttribute('data-count', '2')
    expect(pills().map((p) => p.getAttribute('data-member'))).toEqual(['oncall', 'fixer'])
    // The pill IS the unread signal, so its name says who and why.
    expect(pills()[0]).toHaveAccessibleName('oncall, Unread message')
    expect(pills()[0]).toHaveAttribute('title', 'Unread message')
    expect(pills()[0]).toHaveTextContent('oncall')
    fireEvent.click(pills()[1])
    expect(onPick).toHaveBeenCalledWith('fixer')
    // No "+N" pill: every unread crewmate got its own.
    expect(screen.queryByTestId('crewmate-switcher-unread-more')).toBeNull()
  })

  it('draws nothing when nothing is unread, nor for the OPEN mate alone', () => {
    const { unmount } = column()
    expect(pills()).toHaveLength(0)
    expect(screen.getByTestId('crewmate-switcher-unread-column')).toHaveAttribute('data-count', '0')
    unmount()
    // The open crewmate's thread is on screen: its unread is being read.
    column({ signals: signalsBy({ scribe: { unread: true } }) })
    expect(pills()).toHaveLength(0)
  })

  it('a hidden row (the column\'s hide rule) gets no pill even when unread', () => {
    const hidden = row('ghostwriter', { has_dm_message: false, dashboard_created: false } as Partial<MemberRosterRow>)
    column({ members: [...MEMBERS, hidden], signals: signalsBy({ ghostwriter: { unread: true } }) })
    expect(pills()).toHaveLength(0)
  })

  it('past four pills the rest fold into one "+N" pill that asks for the list', () => {
    const many = ['a', 'b', 'c', 'd', 'e', 'f'].map((n) => row(n))
    const unread = Object.fromEntries(many.map((m) => [m.name, { unread: true }]))
    const { onMore } = column({ members: [MEMBERS[1], ...many], signals: signalsBy(unread) })
    expect(pills().map((p) => p.getAttribute('data-member'))).toEqual(['a', 'b', 'c', 'd'])
    const more = screen.getByTestId('crewmate-switcher-unread-more')
    expect(more).toHaveTextContent('+2 more')
    fireEvent.click(more)
    expect(onMore).toHaveBeenCalledTimes(1)
  })

  it('each pill sits in its own animated row, so it grows in and shrinks out', () => {
    column({ signals: signalsBy({ fixer: { unread: true } }) })
    const rows = screen.getAllByTestId('crewmate-switcher-unread-row')
    expect(rows).toHaveLength(1)
    // The row clips its content while its height animates; the gap above the
    // pill rides inside the row so it animates with it. The clip box is padded
    // by the focus ring's reach (and pulled back by a matching negative
    // margin), so a keyboard focus ring on the pill is not cut off.
    expect(rows[0].className).toContain('overflow-hidden')
    expect(rows[0].className).toContain('px-[3px]')
    expect(rows[0].className).toContain('-mx-[3px]')
    expect(pills()[0].className).toContain('mt-1.5')
  })
})
