/**
 * A row the listing draws as `origin: 'self'` restores with no confirmation --
 * but the row's origin is a cached read of the upload ledger, while the
 * backend's 409 `foreign_install_archive` is a judgment made against the
 * drive's CURRENT bytes. When a co-writer has overwritten this install's own
 * archive, the two disagree: the unconfirmed restore is refused, and before
 * this fix nothing on the page could ever send the `foreignOk` override -- the
 * confirmation strip opened only from the pre-flight origin check the self
 * path skips. These tests pin the recovery path end to end: the refusal opens
 * the strip, the strip carries the overwritten-copy explanation (not the
 * foreign-install one, which the row's own attribution contradicts), the
 * contradictory refusal sentence stays down while the strip is open, and
 * accepting retries with `foreignOk: true`.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { server } from '../../integration/mocks/server'
import { BackupSection } from '../apps/aws-control/DrivePage'
import { renderWithProviders } from './helpers'

const SELF_ID = 'a'.repeat(32)
const KEY = 'kirocrew/backups/snapshot/2026-09-10T00-00-00Z.tar.zst'

/** GET /backup/{account}: one archive the ledger attributes to this install. */
const backupStatus = {
  nightly: false,
  runs: {},
  install: { id: SELF_ID, label: 'this box' },
  remote: {
    snapshot: [{ key: KEY, size: 2048, modified: '2026-09-10T00:00:00Z', install: SELF_ID, origin: 'self' }],
    sessions: [],
    installs: [{ id: SELF_ID, label: 'this box', origin: 'self' }],
    others: 1,
    truncated: false,
    max: 25,
  },
}

/**
 * Wire the two endpoints and return the restore-request log. The restore
 * handler is the backend's real contract: an unconfirmed restore of a key
 * whose stored bytes no longer match the ledger is refused with the 409 code,
 * and only `foreignOk: true` is accepted.
 */
function wireDrive() {
  const restoreBodies: Array<Record<string, unknown>> = []
  server.use(
    http.get('*/api/apps/aws-control/backup/:account', () => HttpResponse.json(backupStatus)),
    http.post('*/api/apps/aws-control/backup/:account/restore', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      restoreBodies.push(body)
      if (body.foreignOk !== true) {
        return HttpResponse.json({ code: 'foreign_install_archive' }, { status: 409 })
      }
      return HttpResponse.json({ downloaded: true, path: '/tmp/staged/restore', bytes: 2048, origin: 'unverified', install: SELF_ID })
    }),
  )
  return restoreBodies
}

/** Open the stored-archive disclosure and click Restore on the one row. */
async function restoreTheRow() {
  fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
  const row = await screen.findByTestId('backup-archive-row')
  fireEvent.click(await screen.findByTestId('backup-restore'))
  return row
}

it('opens the confirm strip when a self-origin restore is refused, and the accept retries with foreignOk', async () => {
  const restoreBodies = wireDrive()
  const view = renderWithProviders(<BackupSection account="prod" />)

  await restoreTheRow()
  // The self path mutates at once, with no override.
  await waitFor(() => expect(restoreBodies).toHaveLength(1))
  expect(restoreBodies[0]).toEqual({ key: KEY })

  // The 409 opens the strip -- the state the page could never reach before.
  const strip = await screen.findByTestId('backup-restore-confirm')
  // The copy names the actual state (own upload, since overwritten), not the
  // foreign-install sentence the row's own attribution would contradict.
  expect(strip.textContent).toContain('something else has written over it since')
  // The refusal banner stays down while the strip is open: the strip IS the
  // confirmation that sentence asks for.
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()

  fireEvent.click(screen.getByTestId('backup-restore-confirm-yes'))
  await waitFor(() => expect(restoreBodies).toHaveLength(2))
  expect(restoreBodies[1]).toEqual({ key: KEY, foreignOk: true })
  // The override restore lands: staged path rendered, no error, strip gone.
  expect(await screen.findByTestId('backup-restored')).toBeVisible()
  expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()
  view.unmount()
})

it('keeps a refusal of an attempt that already carried foreignOk as an error, not a re-ask', async () => {
  // A confirmed retry the backend still refuses must NOT reopen the strip:
  // re-asking the question the reader just answered would loop forever. The
  // refusal renders as the error sentence instead.
  const restoreBodies: Array<Record<string, unknown>> = []
  server.use(
    http.get('*/api/apps/aws-control/backup/:account', () => HttpResponse.json(backupStatus)),
    http.post('*/api/apps/aws-control/backup/:account/restore', async ({ request }) => {
      restoreBodies.push((await request.json()) as Record<string, unknown>)
      return HttpResponse.json({ code: 'foreign_install_archive' }, { status: 409 })
    }),
  )
  const view = renderWithProviders(<BackupSection account="prod" />)

  await restoreTheRow()
  const strip = await screen.findByTestId('backup-restore-confirm')
  expect(strip).toBeVisible()
  fireEvent.click(screen.getByTestId('backup-restore-confirm-yes'))
  await waitFor(() => expect(restoreBodies).toHaveLength(2))
  expect(restoreBodies[1]).toEqual({ key: KEY, foreignOk: true })

  // Strip stays closed; the refusal sentence renders now that no strip answers
  // it -- worded for the refused row's own drawn origin (own upload, since
  // overwritten), not the foreign-install sentence the row's attribution
  // contradicts.
  await waitFor(() => expect(screen.getByTestId('backup-restore-error')).toBeVisible())
  expect(screen.getByTestId('backup-restore-error').textContent).toContain('the drive no longer holds what it uploaded')
  expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
  view.unmount()
})

it('carries the failure in the refusal sentence when the disclosure collapses under an open strip', async () => {
  // The strip lives inside the stored-archive disclosure. Collapsing it while
  // a refusal is waiting must hand the failure back to the section-level
  // notice -- otherwise the refused restore is reported nowhere on the page.
  wireDrive()
  const view = renderWithProviders(<BackupSection account="prod" />)

  await restoreTheRow()
  await screen.findByTestId('backup-restore-confirm')
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()

  fireEvent.click(screen.getByTestId('backup-remote-toggle'))
  expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
  const notice = await screen.findByTestId('backup-restore-error')
  expect(notice.textContent).toContain('the drive no longer holds what it uploaded')

  // Re-opening the disclosure re-mounts the strip, which takes the question
  // back from the sentence.
  fireEvent.click(screen.getByTestId('backup-remote-toggle'))
  await screen.findByTestId('backup-restore-confirm')
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()
  view.unmount()
})

it('concludes the refused attempt on Cancel: no strip, no refusal sentence', async () => {
  // Cancel answers the strip's question with "no". Re-rendering the refusal
  // sentence after that would tell the reader to answer it again.
  wireDrive()
  const view = renderWithProviders(<BackupSection account="prod" />)

  await restoreTheRow()
  await screen.findByTestId('backup-restore-confirm')
  fireEvent.click(screen.getByTestId('backup-restore-confirm-no'))
  expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()
  view.unmount()
})
