import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { server } from '../../integration/mocks/server'
import MemoryBackupsCard from '../pages/overview/MemoryBackupsCard'
import { renderWithProviders } from './helpers'

it.each([false, true])('explains manual backup outcomes without a timer promise with privateMemory=%s', async privateMemory => {
  const store = privateMemory ? 'member-reviewer' : 'default'
  server.use(
    http.get('*/api/memory/backups', () => HttpResponse.json({ backups: [] })),
    http.post('*/api/memory/backup', async ({ request }) => {
      expect(await request.json()).toEqual({ store })
      return HttpResponse.json({ backed_up: 0, skipped: 1, pruned: 0, failed: 0 })
    }),
  )
  const view = renderWithProviders(<MemoryBackupsCard store={store} privateMemory={privateMemory} />)
  expect(await screen.findByText('Use Back up now to create a copy of this memory.')).toBeVisible()
  expect(screen.queryByText(/Backups are taken on a timer/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Back up now' }))
  expect(await screen.findByText('Nothing to back up: 1')).toBeVisible()
  expect(screen.queryByText('Old backup copies removed: 0')).toBeNull()
  expect(screen.queryByText('Failed: 0')).toBeNull()
  expect(screen.queryByText(/Already recent/)).toBeNull()
  view.unmount()
})

it.each([0, 1])('keeps copied and pruned outcomes and reports failed=%s through an alert', async failed => {
  server.use(
    http.get('*/api/memory/backups', () => HttpResponse.json({ backups: [] })),
    http.post('*/api/memory/backup', () => HttpResponse.json({ backed_up: 1, skipped: 0, pruned: 2, failed })),
  )
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  await screen.findByText('Use Back up now to create a copy of this memory.')
  fireEvent.click(screen.getByRole('button', { name: 'Back up now' }))
  expect(await screen.findByText('Copies taken: 1')).toBeVisible()
  expect(screen.getByText('Old backup copies removed: 2')).toBeVisible()
  expect(screen.queryByText('Nothing to back up: 0')).toBeNull()
  if (failed) expect(screen.getByRole('alert')).toHaveTextContent('Failed: 1')
  else expect(screen.queryByRole('alert')).toBeNull()
  view.unmount()
})

it('shows the recovery failure and concrete copy names after a refresh', async () => {
  server.use(http.get('*/api/memory/backups', () => HttpResponse.json({
    backups: [], pending: true, restart_required: true,
    restore_error: 'The staged restore cannot be validated.',
    recovery: {
      journal: 'pending-restore.json',
      staged_copies: ['memory.restore-staged'],
      previous_copies: ['memory.superseded-previous'],
      instruction: 'Preserve these copies before repairing the journal.',
    },
  })))
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('The staged restore cannot be validated.')
  expect(alert).toHaveTextContent('pending-restore.json')
  expect(alert).toHaveTextContent('memory.restore-staged')
  expect(alert).toHaveTextContent('memory.superseded-previous')
  expect(screen.queryByText(/Restart the Kiro Crew gateway to restore this backup/)).toBeNull()
  expect(screen.queryByText(/Current memory stays active/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Cancel staged restore' })).toBeEnabled()
  view.unmount()
})

it('keeps failed activation distinct from ready staging after cancellation', async () => {
  let pending = true
  server.use(
    http.get('*/api/memory/backups', () => HttpResponse.json({
      backups: [], pending, restart_required: true, activation_failed: true,
      restore_error: 'Member recovery failed. Cancel the staged restore, then restart the gateway.',
    })),
    http.post('*/api/memory/restore/cancel', () => {
      pending = false
      return HttpResponse.json({
        ok: true, cancelled: true, pending: false, pending_restore: null,
        restart_required: true, activation_failed: true,
        restore_error: 'Member recovery failed. Cancel the staged restore, then restart the gateway.',
      })
    }),
  )
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Member recovery failed')
  expect(screen.queryByText(/Restart the Kiro Crew gateway to restore this backup/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel staged restore' }))
  expect(await screen.findByText(/Restore cancelled; memory and backup unchanged/)).toHaveTextContent('Startup recovery failed. Restart the Kiro Crew gateway to retry.')
  expect(screen.getByRole('alert')).toHaveTextContent('Member recovery failed')
  expect(screen.queryByText(/Restart the Kiro Crew gateway to restore this backup/)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Cancel staged restore' })).toBeNull()
  view.unmount()
})

it('uses the cancellation response while the status refetch fails', async () => {
  let reads = 0
  let recovered = false
  server.use(
    http.get('*/api/memory/backups', () => {
      reads += 1
      if (recovered) return HttpResponse.json({
        backups: [], pending: false, pending_restore: null, restart_required: false,
      })
      return reads === 1
        ? HttpResponse.json({ backups: [], pending: true, restart_required: true, restore_error: 'Old staging failure.' })
        : HttpResponse.json({ error: 'Status refresh unavailable' }, { status: 503 })
    }),
    http.post('*/api/memory/restore/cancel', () => HttpResponse.json({
      ok: true, cancelled: true, pending: false, pending_restore: null,
      restart_required: true, activation_failed: true,
      restore_error: 'Activation failed before cancellation; restart the gateway.',
    })),
  )
  const view = renderWithProviders(<MemoryBackupsCard store="member-reviewer" privateMemory />, {
    queryDefaults: { retryDelay: 0 },
  })
  expect(await screen.findByRole('alert')).toHaveTextContent('Old staging failure.')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel staged restore' }))
  expect(await screen.findByText(/Restore cancelled; memory and backup unchanged/)).toHaveTextContent('Startup recovery failed. Restart the Kiro Crew gateway to retry.')
  await waitFor(() => expect(screen.getAllByRole('alert').some(
    alert => alert.textContent?.includes('Status refresh unavailable'),
  )).toBe(true))
  expect(screen.getAllByRole('alert').some(
    alert => alert.textContent?.includes('Activation failed before cancellation'),
  )).toBe(true)
  expect(screen.queryByText('Old staging failure.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Cancel staged restore' })).toBeNull()
  recovered = true
  fireEvent.click(screen.getByRole('button', { name: 'Retry memory access' }))
  await waitFor(() => expect(screen.queryAllByRole('alert')).toHaveLength(0))
  view.unmount()
})

it.each([false, true])('stages, remounts and cancels recovery with privateMemory=%s', async privateMemory => {
  const store = privateMemory ? 'member-reviewer' : 'default'
  const name = privateMemory ? 'memory.snapshot.zip' : 'memory.snapshot.db'
  let pending = false
  const writes: unknown[] = []
  server.use(
    http.get('*/api/memory/backups', ({ request }) => {
      expect(new URL(request.url).searchParams.get('store')).toBe(store)
      return HttpResponse.json({
        backups: [{ name, size_bytes: 1000, taken_at: '2026-09-07T12:00:00Z' }],
        pending, restart_required: pending,
      })
    }),
    http.post('*/api/memory/restore', async ({ request }) => {
      writes.push(await request.json())
      pending = true
      return HttpResponse.json({ ok: true, pending: true, restart_required: true })
    }),
    http.post('*/api/memory/restore/cancel', async ({ request }) => {
      expect(await request.json()).toEqual({ store })
      pending = false
      return HttpResponse.json({ ok: true, cancelled: true, pending: false })
    }),
  )
  const view = renderWithProviders(<MemoryBackupsCard store={store} privateMemory={privateMemory} />)
  const restoreButton = await screen.findByRole('button', { name: 'Restore backup', exact: true })
  expect(screen.getByText('Before restoring, Kiro Crew keeps the current memory store as a recovery copy.')).toBeVisible()
  fireEvent.click(restoreButton)
  expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBe(restoreButton)
  expect(restoreButton).toBeDisabled()
  expect(restoreButton).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText(/Current memory is saved beside the store before restoration/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
  expect(await screen.findByText('Restart the Kiro Crew gateway to restore this backup. Current memory stays active until then.')).toBeVisible()
  expect(screen.getByText('Restarting the gateway briefly interrupts active conversations and scheduled work.')).toBeVisible()
  expect(writes).toEqual([{ name, store }])
  expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBeDisabled()
  expect(screen.getByText('Cancel the staged restore before choosing another backup.')).toBeVisible()
  const restart = screen.getByRole('link', { name: 'Open restart controls' })
  const cancel = screen.getByRole('button', { name: 'Cancel staged restore' })
  expect(restart).toHaveAttribute('href', '/settings/about')
  expect(restart.parentElement).toBe(cancel.parentElement)
  expect(restart.parentElement).toHaveClass('gap-3')

  view.unmount()
  const remounted = renderWithProviders(<MemoryBackupsCard store={store} privateMemory={privateMemory} />)
  expect(await screen.findByText('Restart the Kiro Crew gateway to restore this backup. Current memory stays active until then.')).toBeVisible()
  expect(screen.getByText('Restarting the gateway briefly interrupts active conversations and scheduled work.')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel staged restore' }))
  const cancelled = await screen.findByText('Staged restore cancelled. Current memory and backup are unchanged.')
  expect(cancelled).toBeVisible()
  expect(cancelled).not.toHaveTextContent(/restart/i)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBeEnabled())
  expect(screen.queryByRole('button', { name: 'Cancel staged restore' })).toBeNull()
  expect(screen.queryByRole('link', { name: 'Open restart controls' })).toBeNull()
  remounted.unmount()
})
