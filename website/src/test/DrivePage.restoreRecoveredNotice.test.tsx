/**
 * A restore can recover this install's own recorded version of a key that
 * something else has written over. The restore then succeeds, so neither the
 * overwritten-copy confirm strip nor its refusal sentence is reached -- and the
 * overwrite is still worth knowing: the next upload to the key can be
 * overwritten the same way. The reply's `recovered` flag is what carries it,
 * and these tests pin that the success block says so only when it is set.
 */
import { fireEvent, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { expect, it } from 'vitest'
import { server } from '../../integration/mocks/server'
import { BackupSection } from '../apps/aws-control/DrivePage'
import { renderWithProviders as renderUnderHost } from './helpers'
import { AppIdentityProvider } from '../app-sdk/identity'
import en from '../i18n/locales/en.json'

function renderSection() {
  return renderUnderHost(
    <AppIdentityProvider appId="aws-control" origin="builtin">
      <BackupSection account="prod" />
    </AppIdentityProvider>,
  )
}

const SELF_ID = 'a'.repeat(32)
const KEY = 'kirocrew/backups/snapshot/2026-09-10T00-00-00Z.tar.zst'
const RECOVERED_NOTE = en.apps.awsControl.console.backup_restore_recovered_note

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

function wireDrive(recovered: boolean) {
  server.use(
    http.get('*/api/apps/aws-control/backup/:account', () => HttpResponse.json(backupStatus)),
    http.post('*/api/apps/aws-control/backup/:account/restore', () =>
      HttpResponse.json({
        downloaded: true,
        path: '/tmp/staged/restore',
        bytes: 2048,
        origin: 'self',
        install: SELF_ID,
        recovered,
      }),
    ),
  )
}

async function restoreTheRow() {
  fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
  await screen.findByTestId('backup-archive-row')
  fireEvent.click(await screen.findByTestId('backup-restore'))
}

it('tells the operator the archive had been written over when the restore recovered it', async () => {
  wireDrive(true)
  const view = renderSection()

  await restoreTheRow()
  const restored = await screen.findByTestId('backup-restored')
  const note = screen.getByTestId('backup-restore-recovered')
  expect(note).toHaveTextContent(RECOVERED_NOTE)
  expect(restored).toContainElement(note)
  // The warning leads the block in the warn tone, so a normal-looking restore
  // does not hide it; the "at the path below" note stays directly above the path.
  expect(note.className).toContain('text-warn')
  const restoredNote = screen.getByText(en.apps.awsControl.console.backup_restored_note)
  const path = screen.getByText('/tmp/staged/restore')
  expect(note.compareDocumentPosition(restoredNote) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(restoredNote.compareDocumentPosition(path) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  // The restore succeeded, so nothing asks the operator to download it anyway.
  expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
  expect(screen.queryByTestId('backup-restore-error')).toBeNull()
  view.unmount()
})

it('says nothing about an overwrite when the restore did not need to recover', async () => {
  wireDrive(false)
  const view = renderSection()

  await restoreTheRow()
  await screen.findByTestId('backup-restored')
  expect(screen.queryByTestId('backup-restore-recovered')).toBeNull()
  view.unmount()
})
