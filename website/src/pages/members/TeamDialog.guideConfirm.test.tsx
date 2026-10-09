/**
 * A guide's removal step on TeamDialog's inline confirm: the first "Delete
 * team" only swaps in "Keep team" and the real delete, so it is never the
 * answer. Only the final, registered delete confirms.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TeamDialog from './TeamDialog'
import { api } from '../../api/client'
import { openDialogsNow, watchConfirmDialog } from '../../guide/guideConfirmWatch'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const flush = () => act(async () => { await Promise.resolve() })

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const team = { id: 't1', name: 'Ops', members: [] }
  render(
    <QueryClientProvider client={qc}>
      <TeamDialog open team={team} teams={[team]} members={[]} onClose={() => {}} onSaved={() => {}} />
    </QueryClientProvider>,
  )
}

describe('TeamDialog inline delete confirm and the guide', () => {
  it('the first Delete team waits, Keep team is no confirm, and only the final delete confirms', async () => {
    vi.spyOn(api.teams, 'remove').mockReturnValue(new Promise(() => {}))
    renderDialog()
    const answer = vi.fn()
    const first = screen.getByTestId('team-dialog-delete')
    const stop = watchConfirmDialog(openDialogsNow(), answer, first)
    fireEvent.click(first)
    await flush()
    // The pressed control is gone (swapped for the anchor): still waiting.
    expect(screen.queryByTestId('team-dialog-delete')).toBeNull()
    expect(answer).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('team-dialog-delete-keep'))
    await flush()
    expect(answer).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('team-dialog-delete'))
    await flush()
    fireEvent.click(screen.getByTestId('team-dialog-delete-confirm'))
    await flush()
    expect(answer).toHaveBeenCalledExactlyOnceWith('confirmed')
    stop()
  })
})
