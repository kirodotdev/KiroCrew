/**
 * Restoring a memory backup keeps keyboard focus and drops a stale failure.
 *
 * Each step of the confirm flow unmounts or disables the control that started
 * it, which left focus on the page body:
 *   - Restore disables itself while armed, so focus moves to the strip's Cancel;
 *   - Cancel unmounts the strip, so focus returns to that row's Restore;
 *   - Confirm is disabled while the request runs, so a refusal hands focus back
 *     to the strip and a staged restore moves it to "Cancel staged restore".
 * Cancel also clears the error of a refused attempt: the restore it describes
 * is no longer being asked for.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { server } from '../../integration/mocks/server'
import MemoryBackupsCard from '../pages/overview/MemoryBackupsCard'
import { renderWithProviders } from './helpers'

const NAME = 'memory.snapshot.zip'

function serve(onRestore: () => Response | Promise<Response>) {
  let pending = false
  server.use(
    http.get('*/api/memory/backups', () => HttpResponse.json({
      backups: [{ name: NAME, size_bytes: 1000, taken_at: '2026-09-07T12:00:00Z' }],
      pending, restart_required: pending,
    })),
    http.post('*/api/memory/restore', async () => {
      const response = await onRestore()
      if (response.ok) pending = true
      return response
    }),
  )
}

it('arming moves focus to the strip, and Cancel returns it to Restore', async () => {
  serve(() => HttpResponse.json({ ok: true, pending: true, restart_required: true }))
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  const restore = await screen.findByRole('button', { name: 'Restore backup', exact: true })
  restore.focus()
  fireEvent.click(restore)
  const cancel = screen.getByRole('button', { name: 'Cancel' })
  await waitFor(() => expect(cancel).toHaveFocus())
  // The strip's buttons carry its warning, so it is read with them.
  expect(cancel).toHaveAccessibleDescription(/Current memory is saved beside the store before restoration/)
  fireEvent.click(cancel)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toHaveFocus())
  view.unmount()
})

it('a staged restore moves focus to "Cancel staged restore"', async () => {
  serve(() => HttpResponse.json({ ok: true, pending: true, restart_required: true }))
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  fireEvent.click(await screen.findByRole('button', { name: 'Restore backup', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
  const cancelStaged = await screen.findByRole('button', { name: 'Cancel staged restore' })
  await waitFor(() => expect(cancelStaged).toHaveFocus())
  view.unmount()
})

it('a refused restore hands focus back to the strip, and Cancel clears its error', async () => {
  serve(() => HttpResponse.json({ error: 'Restore refused by the store.' }, { status: 409 }))
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  fireEvent.click(await screen.findByRole('button', { name: 'Restore backup', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
  expect(await screen.findByText(/Restore refused by the store/)).toBeInTheDocument()
  const cancel = screen.getByRole('button', { name: 'Cancel' })
  await waitFor(() => expect(cancel).toHaveFocus())
  fireEvent.click(cancel)
  await waitFor(() => expect(screen.queryByText(/Restore refused by the store/)).toBeNull())
  view.unmount()
})

it('a refusal does not pull focus back when the reader moved on while it ran', async () => {
  let refuse!: () => void
  serve(() => new Promise<Response>((resolve) => {
    refuse = () => resolve(HttpResponse.json({ error: 'Restore refused by the store.' }, { status: 409 }))
  }))
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  fireEvent.click(await screen.findByRole('button', { name: 'Restore backup', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
  await waitFor(() => expect(refuse).toBeDefined())
  const backUpNow = screen.getByRole('button', { name: 'Back up now' })
  backUpNow.focus()
  refuse()
  expect(await screen.findByText(/Restore refused by the store/)).toBeInTheDocument()
  expect(backUpNow).toHaveFocus()
  view.unmount()
})
