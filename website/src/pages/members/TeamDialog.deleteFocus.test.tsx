/**
 * TeamDialog's inline delete confirm keeps keyboard focus in the row.
 *
 * "Delete team" is swapped for a muted anchor when pressed, and "Keep team"
 * swaps it back, so each press unmounts the focused button. Arming moves focus
 * to "Keep team" (the safe choice, so a held Enter cannot delete), and backing
 * out returns it to "Delete team".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TeamDialog from './TeamDialog'
import { api } from '../../api/client'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const team = { id: 't1', name: 'Ops', members: [] }
  render(
    <QueryClientProvider client={qc}>
      <TeamDialog open team={team} teams={[team]} members={[]} onClose={() => {}} onSaved={() => {}} />
    </QueryClientProvider>,
  )
}

describe('TeamDialog delete confirm focus', () => {
  it('arming focuses Keep team, and Keep team returns focus to Delete team', async () => {
    const remove = vi.spyOn(api.teams, 'remove').mockReturnValue(new Promise(() => {}))
    renderDialog()
    const first = screen.getByTestId('team-dialog-delete')
    first.focus()
    fireEvent.click(first)
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('team-dialog-delete-keep')))

    fireEvent.click(screen.getByTestId('team-dialog-delete-keep'))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('team-dialog-delete')))
    expect(remove).not.toHaveBeenCalled()
  })
})
