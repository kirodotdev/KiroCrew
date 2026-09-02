import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import AssigneePicker from '../apps/issue-radar/components/AssigneePicker'

// Behaviour pins for the assignee editor (AssigneePicker.tsx) -- the control behind
// the ASSIGNEES sidebar block's "Edit" toggle.
//
// It is ONE dropdown rather than a grid of member chips. Two reasons point the same
// way: `max-two-buttons-per-row` caps a horizontal action group at two controls and a
// roster would render N siblings (the rule's own prescribed fix is an overflow menu,
// which counts as one), and a dropdown is what the forges themselves use for this
// job.
//
// What these cover, and why each is load-bearing:
//
//  * Selection is matched CASE-INSENSITIVELY in BOTH directions. The roster and the
//    issue's assignee list can each spell the same person with their own case, and
//    which side differs is not predictable -- a one-sided fold passes while the other
//    is missing, so a real assignee renders as unassigned and a click re-adds them.
//  * `onToggle` receives the ROSTER's spelling, because the write resolves against
//    the roster and the issue's spelling can fail to resolve.
//  * An assignee the roster does NOT list still renders, in its own group, and stays
//    removable -- otherwise someone assigned upstream is stranded on the issue. This
//    is also why an empty roster does not short-circuit while such assignees exist.
//  * The cap disables ADDING without disabling REMOVING; a picker frozen at the cap
//    would be a dead end.
//  * Ordering puts the current set first, then the current user, then the rest, so
//    the two things a triager acts on are never buried in a long roster.

/** Open the dropdown and return its content element.
 *
 * The trigger is the only button rendered before the menu opens, so it is
 * addressed positionally rather than by label -- its label is the current set,
 * which every caller varies. */
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole('button')[0])
  return await screen.findByRole('menu')
}

/** The member rows inside the open menu, in DOM order. */
function rows(menu: HTMLElement) {
  return within(menu).getAllByRole('menuitemcheckbox').map((b) => b.textContent?.trim() ?? '')
}

const MEMBERS = ['alice', 'bob', 'carol']

describe('AssigneePicker — selection', () => {
  it('toggles a selected member off on click', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<AssigneePicker members={MEMBERS} selected={['bob']} onToggle={onToggle} />)
    const menu = await open(user)
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: /bob/ }))
    expect(onToggle).toHaveBeenCalledWith('bob')
  })

  it('treats selection as case-insensitive in BOTH directions', async () => {
    // Testing only one direction passes even when the other fold is missing.
    for (const [member, assigned] of [['Bob', 'bob'], ['bob', 'Bob'], ['BOB', 'bob']]) {
      const user = userEvent.setup()
      const { unmount } = render(
        <AssigneePicker members={[member]} selected={[assigned]} onToggle={vi.fn()} />,
      )
      const menu = await open(user)
      const row = within(menu).getByRole('menuitemcheckbox', { name: new RegExp(member, 'i') })
      expect(row.getAttribute('aria-checked'), `${member}/${assigned}`).toBe('true')
      // And it is NOT surfaced as an assignee the roster does not list.
      expect(within(menu).queryByText(/not repo members/i), `${member}/${assigned}`).toBeNull()
      unmount()
    }
  })

  it('passes the roster spelling to onToggle, not the caller spelling', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<AssigneePicker members={['Bob']} selected={['bob']} onToggle={onToggle} />)
    const menu = await open(user)
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: /Bob/i }))
    expect(onToggle).toHaveBeenCalledWith('Bob')
  })

  it('summarises the current set on the trigger', () => {
    render(<AssigneePicker members={MEMBERS} selected={['alice', 'bob']} onToggle={vi.fn()} />)
    expect(screen.getByRole('button', { name: /alice, bob/ })).toBeTruthy()
  })
})

describe('AssigneePicker — assignees outside the roster', () => {
  it('surfaces an unlisted assignee separately and keeps it removable', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<AssigneePicker members={MEMBERS} selected={['ghost']} onToggle={onToggle} />)
    const menu = await open(user)
    expect(within(menu).getByText(/not repo members/i)).toBeTruthy()
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: /ghost/ }))
    expect(onToggle).toHaveBeenCalledWith('ghost')
  })

  it('still opens when the roster is empty but someone is assigned', async () => {
    // A triage-only repo can derive an empty roster while the issue retains
    // assignees; returning early would make their removal controls unreachable.
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(<AssigneePicker members={[]} selected={['ghost']} onToggle={onToggle} />)
    const menu = await open(user)
    await user.click(within(menu).getByRole('menuitemcheckbox', { name: /ghost/ }))
    expect(onToggle).toHaveBeenCalledWith('ghost')
  })

  it('does not surface a roster member as unlisted', async () => {
    const user = userEvent.setup()
    render(<AssigneePicker members={MEMBERS} selected={['alice']} onToggle={vi.fn()} />)
    const menu = await open(user)
    expect(within(menu).queryByText(/not repo members/i)).toBeNull()
  })
})

describe('AssigneePicker — the cap', () => {
  it('disables adding but never removing when at the cap', async () => {
    const onToggle = vi.fn()
    const user = userEvent.setup()
    render(
      <AssigneePicker members={MEMBERS} selected={['alice']} onToggle={onToggle} atCap />,
    )
    const menu = await open(user)
    const bob = within(menu).getByRole('menuitemcheckbox', { name: /bob/ })
    expect(bob).toBeDisabled()
    await user.click(bob)
    expect(onToggle).not.toHaveBeenCalled()

    // ...but the selected one can still be removed, or the editor is a dead end.
    const alice = within(menu).getByRole('menuitemcheckbox', { name: /alice/ })
    expect(alice).not.toBeDisabled()
    await user.click(alice)
    expect(onToggle).toHaveBeenCalledWith('alice')
  })

  it('enables every member when there is room', async () => {
    const user = userEvent.setup()
    render(<AssigneePicker members={MEMBERS} selected={['alice']} onToggle={vi.fn()} />)
    const menu = await open(user)
    for (const login of MEMBERS) {
      expect(
        within(menu).getByRole('menuitemcheckbox', { name: new RegExp(login) }),
      ).not.toBeDisabled()
    }
  })
})

describe('AssigneePicker — ordering and filtering', () => {
  it('puts the selected set first, then the current user, then the rest', async () => {
    const user = userEvent.setup()
    render(
      <AssigneePicker
        members={['alice', 'bob', 'carol', 'dave']}
        selected={['dave']}
        me="carol"
        onToggle={vi.fn()}
      />,
    )
    const menu = await open(user)
    expect(rows(menu)).toEqual(['dave', 'carol', 'alice', 'bob'])
  })

  it('filters the roster by the search box, case-insensitively', async () => {
    const user = userEvent.setup()
    render(<AssigneePicker members={['Alice', 'bob']} selected={[]} onToggle={vi.fn()} />)
    const menu = await open(user)
    await user.type(within(menu).getByRole('textbox'), 'ALI')
    await waitFor(() => expect(rows(menu)).toEqual(['Alice']))
  })
})

describe('AssigneePicker — nothing to show', () => {
  it('says the roster is unreadable when there is no roster AND nobody assigned', () => {
    render(<AssigneePicker members={[]} selected={[]} onToggle={vi.fn()} />)
    expect(screen.getByText(/No members to assign/i)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('honours a caller-supplied empty message', () => {
    render(
      <AssigneePicker members={[]} selected={[]} onToggle={vi.fn()} emptyText="Nobody here" />,
    )
    expect(screen.getByText('Nobody here')).toBeTruthy()
  })
})
