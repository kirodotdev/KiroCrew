import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { CrewTeam, MemberRosterRow } from '../../api/client'

const teamsCreate = vi.fn()
const teamsUpdate = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      teams: {
        ...actual.api.teams,
        create: (...a: unknown[]) => teamsCreate(...a),
        update: (...a: unknown[]) => teamsUpdate(...a),
      },
    },
  }
})

import TeamDialog from './TeamDialog'

/* TeamDialog's CREATE mode, pinned on the component.
 *
 * The Crewmates page no longer draws a New team door -- team creation is hidden
 * for the current phase (`MembersPage.tsx`, the "+" menu) -- so these cases are
 * what keeps the create half honest while nothing on screen reaches it: the
 * checklist it opens with, the gate on a name, the body it posts, and the draft
 * guard. They are also the proof that restoring the door is a revert rather
 * than a rewrite. Edit mode stays covered through the page, where the team
 * view's Edit still opens this dialog.
 */

const row = (name: string): MemberRosterRow => ({
  name, slug: name.toLowerCase(), bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
} as MemberRosterRow)

const MEMBERS = [row('oncall'), row('docs')]
const TRIAGE: CrewTeam = { id: 'abc123abc123', name: 'Triage', members: ['docs'] }

function renderCreate(teams: CrewTeam[] = [], over: Partial<Parameters<typeof TeamDialog>[0]> = {}) {
  const onClose = vi.fn()
  const onSaved = vi.fn()
  renderWithProviders(
    <TeamDialog open team={undefined} teams={teams} members={MEMBERS} onClose={onClose} onSaved={onSaved} {...over} />,
  )
  return { onClose, onSaved }
}

beforeEach(() => {
  teamsCreate.mockReset()
  teamsUpdate.mockReset()
})

describe('TeamDialog create mode', () => {
  it('lists every crewmate with the team it is on, names the one-team rule, and says it creates', async () => {
    renderCreate([TRIAGE])
    const body = await screen.findByTestId('team-dialog-body')
    const rows = within(body).getAllByTestId('team-dialog-row')
    expect(rows).toHaveLength(2)
    // A crewmate already on a team reads "On Triage"; an unplaced one says so.
    expect(rows.map((r) => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('On Triage'), expect.stringContaining('No team')]),
    )
    expect(within(body).getByText(/on one team at a time/i)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toHaveAccessibleName('New team')
    expect(screen.getByTestId('team-dialog-save')).toHaveTextContent('Create team')
  })

  it('gates Create on a name, then posts the picked crewmates in roster order and reports the saved team', async () => {
    const created: CrewTeam = { id: 'def456def456', name: 'Release', members: ['oncall', 'docs'] }
    teamsCreate.mockResolvedValue({ team: created })
    const { onSaved } = renderCreate()
    const body = await screen.findByTestId('team-dialog-body')
    expect(screen.getByTestId('team-dialog-save')).toBeDisabled()
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    expect(screen.getByTestId('team-dialog-save')).toBeEnabled()
    // Picked out of order; the body carries the ROSTER's order.
    fireEvent.click(screen.getByLabelText('docs'))
    fireEvent.click(screen.getByLabelText('oncall'))
    fireEvent.submit(body)
    await waitFor(() => expect(teamsCreate).toHaveBeenCalledWith({ name: 'Release', members: ['oncall', 'docs'] }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created))
    // Create is the whole write: nothing is sent to the update route.
    expect(teamsUpdate).not.toHaveBeenCalled()
  })

  it('a whitespace-only name creates nothing, and a failed create says so inside the dialog', async () => {
    teamsCreate.mockRejectedValue(new Error('teams route down'))
    const { onSaved } = renderCreate()
    const body = await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: '   ' } })
    expect(screen.getByTestId('team-dialog-save')).toBeDisabled()
    fireEvent.submit(body)
    expect(teamsCreate).not.toHaveBeenCalled()

    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    await waitFor(() => expect(screen.getByText('Could not create the team')).toBeInTheDocument())
    expect(onSaved).not.toHaveBeenCalled()
    // The dialog stays open over the draft rather than dropping the typed name.
    expect(screen.getByTestId('team-dialog-body')).toBeInTheDocument()
    expect(screen.getByTestId('team-dialog-name')).toHaveValue('Release')
  })

  it('guards an unsaved draft against Escape; Cancel is still the one-click exit', async () => {
    const { onClose } = renderCreate()
    await screen.findByTestId('team-dialog-body')
    // Nothing typed: Escape is an ordinary dismissal.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))

    onClose.mockClear()
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('team-dialog-cancel'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
