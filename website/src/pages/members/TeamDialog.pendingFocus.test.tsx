// Focus while the team dialog's request is out.
//
// Disabling the control a reader just pressed blurs it to `<body>`, outside the
// dialog, so a keyboard user lost their place before the save or delete answered.
// The pressed control stays focusable and announces itself unavailable instead,
// and a second press sends nothing.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../../test/helpers'
import type { CrewTeam, MemberRosterRow } from '../../api/client'

const teamsCreate = vi.fn()
const teamsRemove = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      teams: {
        ...actual.api.teams,
        create: (...a: unknown[]) => teamsCreate(...a),
        remove: (...a: unknown[]) => teamsRemove(...a),
      },
    },
  }
})

import TeamDialog from './TeamDialog'

const row = (name: string): MemberRosterRow => ({
  name, slug: name.toLowerCase(), bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
} as MemberRosterRow)

const MEMBERS = [row('oncall'), row('docs')]
const TRIAGE: CrewTeam = { id: 'abc123abc123', name: 'Triage', members: ['docs'] }

/** A request the test settles by hand, so the pending state can be read. */
function held() {
  let reject: (err: unknown) => void = () => {}
  const promise = new Promise((_resolve, rej) => { reject = rej })
  return { promise, fail: () => reject(new Error('the store is busy')) }
}

function mount(team?: CrewTeam) {
  renderWithProviders(
    <TeamDialog open team={team} teams={team ? [team] : []} members={MEMBERS} onClose={vi.fn()} onSaved={vi.fn()} onDeleted={vi.fn()} />,
  )
}

beforeEach(() => {
  teamsCreate.mockReset()
  teamsRemove.mockReset()
})

describe('TeamDialog focus while a request is out', () => {
  it('keeps focus on Create team, refuses a second press, and keeps it after a failure', async () => {
    const req = held()
    teamsCreate.mockReturnValue(req.promise)
    mount()
    await userEvent.type(await screen.findByTestId('team-dialog-name'), 'Release')
    const save = screen.getByTestId('team-dialog-save')

    save.focus()
    await userEvent.keyboard('{Enter}')
    expect(teamsCreate).toHaveBeenCalledTimes(1)
    expect(save.getAttribute('aria-disabled')).toBe('true')
    expect(save.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(save)

    await userEvent.keyboard('{Enter}')
    expect(teamsCreate).toHaveBeenCalledTimes(1)
    // Held, it looks like a disabled primary button: the muted surface, not a
    // faded accent. Each `disabled:` look class has its `aria-disabled:` twin.
    for (const look of ['bg-bg-hover', 'text-muted', 'border-transparent', 'shadow-none', 'opacity-60']) {
      expect(save.className).toContain(`aria-disabled:${look}`)
    }

    req.fail()
    expect(await screen.findByText('the store is busy')).toBeTruthy()
    expect(document.activeElement).toBe(save)
    expect(save.hasAttribute('aria-disabled')).toBe(false)
  })

  it('keeps focus in the name field when Enter submits from it', async () => {
    const req = held()
    teamsCreate.mockReturnValue(req.promise)
    mount()
    const field = await screen.findByTestId('team-dialog-name')
    await userEvent.type(field, 'Release')
    // A browser submits the form on Enter here (one text field); user-event
    // counts the checkboxes too and does not, so the submit is fired directly.
    fireEvent.submit(screen.getByTestId('team-dialog-body'))
    await waitFor(() => expect(teamsCreate).toHaveBeenCalledTimes(1))
    expect(field.hasAttribute('disabled')).toBe(false)
    expect(field.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(field)

    // Held read-only, so nothing typed lands after the body was sent.
    await userEvent.type(field, 'X')
    expect((field as HTMLInputElement).value).toBe('Release')

    req.fail()
    expect(await screen.findByText('the store is busy')).toBeTruthy()
    expect(document.activeElement).toBe(field)
  })

  it('keeps focus on the delete confirm button while the delete is out', async () => {
    const req = held()
    teamsRemove.mockReturnValue(req.promise)
    mount(TRIAGE)
    await userEvent.click(await screen.findByTestId('team-dialog-delete'))
    const confirm = screen.getByTestId('team-dialog-delete-confirm')

    confirm.focus()
    await userEvent.keyboard('{Enter}')
    expect(teamsRemove).toHaveBeenCalledTimes(1)
    expect(confirm.getAttribute('aria-disabled')).toBe('true')
    expect(confirm.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(confirm)

    await userEvent.keyboard('{Enter}')
    expect(teamsRemove).toHaveBeenCalledTimes(1)

    req.fail()
    expect(await screen.findByText('the store is busy')).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(confirm))
  })
})
