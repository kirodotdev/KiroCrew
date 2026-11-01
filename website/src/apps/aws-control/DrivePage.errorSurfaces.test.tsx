/**
 * DrivePage suites: failures render through the shared error notice, with the agent
 * hand-off. DrivePage.test.tsx names the suites split from it; shared fixtures are in
 * src/test/DrivePageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { i18nT } from '../../i18n/t'

/* The sections read only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the 403/409 paths. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      iamPolicy: vi.fn(),
      drive: vi.fn(),
      driveBootstrapPreview: vi.fn(),
      driveBootstrapConfirm: vi.fn(),
      driveList: vi.fn(),
      driveDownload: vi.fn(),
      drivePreview: vi.fn(),
      driveSearch: vi.fn(),
      driveMove: vi.fn(),
      driveUpload: vi.fn(),
      driveDelete: vi.fn(),
      driveFolderCreate: vi.fn(),
      driveFolderDelete: vi.fn(),
      driveShare: vi.fn(),
      shares: vi.fn(),
      shareForget: vi.fn(),
      costs: vi.fn(),
      library: vi.fn(),
      libraryPush: vi.fn(),
      libraryRemove: vi.fn(),
      backup: vi.fn(),
      backupRun: vi.fn(),
      backupNightly: vi.fn(),
      backupNightlySessions: vi.fn(),
      backupRestore: vi.fn(),
      installLabel: vi.fn(),
    },
  }
})

/* Library cards lazily fetch the full artifact through the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    artifact: vi.fn(),
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

/* The preview pane renders code through the dashboard's shared ContentRenderer,
 * whose code surface is Pierre -- and Pierre's real chunk never resolves under
 * vitest. Stub the mount so "the code viewer got the bytes" is assertable here;
 * its chrome is Playwright's to check. */
vi.mock('../../pierre', () => ({
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-mounted">{file.contents}</div>
  ),
}))

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import {
  recordError, consumeChatHandoff, installSoftNavigate,
  __resetErrorJournalForTests, __resetNavSeamForTests,
} from '../../utils/errorReport'
import {
  ACCOUNT_ID, emptyBackup, INSTALL_ID, renderDrive, stubDrivePresent,
} from '../../test/DrivePageTestUtils'

beforeEach(() => {
  vi.clearAllMocks()
  // Reset, not clear: a clear keeps each mock's persistent and queued-once
  // implementations, so a case that stubs a running backup (or holds a read in
  // flight) would hand that state to whichever case runs next, and a case that
  // seeds nothing would pass only after one that did. Every case starts from
  // the same present, empty drive instead.
  for (const group of [awsControlApi, api] as unknown as Array<Record<string, unknown>>) {
    for (const fn of Object.values(group)) {
      if (vi.isMockFunction(fn)) fn.mockReset()
    }
  }
  stubDrivePresent()
  // The grid/list toggle persists per section to localStorage, which outlives a
  // single test. Without this, a test that switches a section's view silently
  // changes what every LATER test in the file renders -- the table controls
  // vanish and the failure points at the wrong test.
  localStorage.clear()
  // Library card previews fetch the full artifact when near the viewport; a
  // never-resolving fetch keeps every preview a placeholder without touching
  // the network in either IntersectionObserver regime.
  vi.mocked(api.artifact).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.artifact>)
})

const BLOCKED_HOST_KEY = 'apps.awsControl.console.backup_nightly_sessions_blocked_host'

/**
 * Every failure this app can show renders through the shared error notice, and
 * every notice offers the agent hand-off with the REAL failure attached — not
 * the localised sentence the reader saw. The cases below are the ones that used
 * to render nothing at all; the migrated ones keep their test ids and are
 * covered by the assertions in DrivePage.test.tsx and the other suites split from it.
 */
describe('DrivePage sections: error surfaces reach the agent', () => {
  /** An `AwsControlError` exactly as `request` would build it: code + journaled report. */
  function failure(code: string, status: number, endpoint: string, body: string): AwsControlError {
    const report = recordError({ source: 'api', message: body, status, code, endpoint, detail: body })
    return new AwsControlError(code, status, report)
  }

  const navigated: string[] = []
  beforeEach(() => {
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    navigated.length = 0
    sessionStorage.clear()
    installSoftNavigate((to) => { navigated.push(to) })
    stubDrivePresent()
  })

  it('a failed Files listing is a notice with Retry, not an empty folder', async () => {
    vi.mocked(awsControlApi.driveList)
      .mockRejectedValueOnce(new AwsControlError('aws_call_failed', 502))
      .mockResolvedValue({ files: [], folders: [] })
    await renderDrive('drive')

    const notice = await screen.findByTestId('drive-list-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.files_list_failed'))
    expect(notice).toHaveAttribute('role', 'alert')
    // The empty state must NOT have claimed the folder is empty.
    expect(screen.queryByTestId('drive-empty')).toBeNull()

    fireEvent.click(screen.getByTestId('drive-list-error-retry'))
    expect(await screen.findByTestId('drive-empty')).toBeTruthy()
    expect(awsControlApi.driveList).toHaveBeenCalledTimes(2)
  })

  it('the hand-off carries the endpoint, status and code — not only the sentence on screen', async () => {
    vi.mocked(awsControlApi.driveList).mockRejectedValue(
      failure('aws_call_failed', 502, '/api/apps/aws-control/drive/111122223333/list', 'AccessDenied: s3:ListBucket'),
    )
    await renderDrive('drive')

    const notice = await screen.findByTestId('drive-list-error')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))

    expect(navigated).toEqual(['/chat'])
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain('/api/apps/aws-control/drive/111122223333/list')
    expect(staged).toContain('HTTP 502')
    expect(staged).toContain('aws_call_failed')
    expect(staged).toContain('AccessDenied: s3:ListBucket')
  })

  it('a refused share link is said in the dialog instead of re-enabling the button silently', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockRejectedValue(new AwsControlError('publish_denied', 403))
    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    fireEvent.change(await screen.findByTestId('share-note'), { target: { value: 'for the Q3 review' } })
    fireEvent.click(await screen.findByTestId('share-create'))

    const notice = await screen.findByTestId('share-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.share_failed'))
    // No hand-off here: the note field still holds the typed text, and the
    // hand-off would navigate away from it. The draft survives the refusal.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('share-note')).toHaveValue('for the Q3 review')
    // Still no result: nothing was shared.
    expect(screen.queryByTestId('share-result')).toBeNull()
  })

  it('a failed backup-status read and a refused nightly toggle each get their own line', async () => {
    vi.mocked(awsControlApi.backup)
      .mockRejectedValueOnce(new AwsControlError('http_500', 500))
      .mockResolvedValue(emptyBackup)
    await renderDrive('backup')
    const notice = await screen.findByTestId('backup-status-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.backup_status_failed'))
    // No rows were invented for a status we do not have.
    expect(screen.queryByTestId('backup-row-snapshot')).toBeNull()

    // A read the reader can re-issue offers Retry, and Retry re-reads.
    fireEvent.click(screen.getByTestId('backup-status-error-retry'))
    expect(await screen.findByTestId('backup-row-snapshot')).toBeTruthy()
    expect(screen.queryByTestId('backup-status-error')).toBeNull()
  })

  it('a refused nightly toggle says so under the toggle it failed to move', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.backupNightly).mockRejectedValue(new AwsControlError('restricted_session', 403))
    await renderDrive('backup')

    const toggle = within(await screen.findByTestId('backup-nightly')).getByRole('switch')
    fireEvent.click(toggle)
    const notice = await screen.findByTestId('backup-nightly-error')
    expect(notice.textContent).toContain(i18nT('apps.awsControl.console.backup_nightly'))
    // Adjacent to the row it explains: the notice is the nightly row's next sibling.
    expect(screen.getByTestId('backup-nightly').nextElementSibling).toContainElement(notice)
  })

  it('the transcript switch is its own grant: off while the snapshot nightly is on', async () => {
    // The state most installs are in, and the one a single field could not
    // render: memory is backed up nightly and conversations are not.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup, nightly: true, nightlySessions: false,
    })
    vi.mocked(awsControlApi.backupNightlySessions).mockResolvedValue({ nightlySessions: true } as never)
    await renderDrive('backup')

    const snapshotToggle = within(await screen.findByTestId('backup-nightly')).getByRole('switch')
    const sessionsToggle = within(screen.getByTestId('backup-nightly-sessions')).getByRole('switch')
    expect(snapshotToggle.getAttribute('aria-checked')).toBe('true')
    expect(sessionsToggle.getAttribute('aria-checked')).toBe('false')

    // Flipping it reaches its OWN endpoint, and never the snapshot one.
    fireEvent.click(sessionsToggle)
    await waitFor(() =>
      expect(awsControlApi.backupNightlySessions).toHaveBeenCalledWith(ACCOUNT_ID, true),
    )
    expect(awsControlApi.backupNightly).not.toHaveBeenCalled()
  })

  it('a backend that does not report the transcript grant renders it off', async () => {
    // The field is absent from an older backend. Read as undefined it must show
    // OFF, not an indeterminate switch that a click could flip in the dark: a
    // page that cannot say whether transcripts are being uploaded must not
    // imply they are not by accident.
    const { nightlySessions: _omitted, ...withoutField } = { ...emptyBackup, nightlySessions: false }
    vi.mocked(awsControlApi.backup).mockResolvedValue(withoutField)
    await renderDrive('backup')

    const sessionsToggle = within(await screen.findByTestId('backup-nightly-sessions')).getByRole('switch')
    expect(sessionsToggle.getAttribute('aria-checked')).toBe('false')
  })

  it('a refused transcript toggle says so under its own row', async () => {
    // Its own notice under its own row. Sharing the snapshot row's notice would
    // put the explanation under a switch that did not move.
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.backupNightlySessions).mockRejectedValue(new AwsControlError('restricted_session', 403))
    await renderDrive('backup')

    const toggle = within(await screen.findByTestId('backup-nightly-sessions')).getByRole('switch')
    fireEvent.click(toggle)
    const notice = await screen.findByTestId('backup-nightly-sessions-error')
    expect(screen.getByTestId('backup-nightly-sessions').nextElementSibling).toContainElement(notice)
    // Names the transcript setting rather than reusing the snapshot row's copy.
    // With two nightly switches on the page, "the nightly backup setting" cannot
    // say which one it failed to change.
    expect(notice.textContent).toContain(i18nT('apps.awsControl.console.backup_nightly_sessions'))
  })

  it('a grant that cannot run here says so next to the switch', async () => {
    // The state a surface reading only the grant would show as healthy: the owner
    // asked for nightly transcripts, the host cannot produce them, and without
    // this the console claims the backup is on while nothing is ever uploaded.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: true,
      nightlySessionsBlocked: 'host_unsupported',
    } as never)
    await renderDrive('backup')

    const row = await screen.findByTestId('backup-nightly-sessions')
    const blocked = await screen.findByTestId('backup-nightly-sessions-blocked')
    expect(blocked.textContent).toContain(i18nT(BLOCKED_HOST_KEY))
    // The switch still reads back as the owner set it. Flipping it to reflect
    // the host would answer a question they did not ask.
    expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('true')
  })

  it('no blocked notice while the grant is off', async () => {
    // Nothing is being withheld until the owner asks for it, and the hint above
    // the switch already says what it would do.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: false,
      nightlySessionsBlocked: 'host_unsupported',
    } as never)
    await renderDrive('backup')
    await screen.findByTestId('backup-nightly-sessions')
    expect(screen.queryByTestId('backup-nightly-sessions-blocked')).toBeNull()
  })

  it('no blocked notice when the grant is on and nothing is in the way', async () => {
    // The other half. Without this pair the assertion above is satisfied by a
    // notice that renders unconditionally, which would tell every healthy install
    // its transcripts are not being backed up.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup, nightlySessions: true, nightlySessionsBlocked: null,
    } as never)
    await renderDrive('backup')
    await screen.findByTestId('backup-nightly-sessions')
    expect(screen.queryByTestId('backup-nightly-sessions-blocked')).toBeNull()
  })

  it('the consent hint states the same scope the archive row does', async () => {
    // The switch authorizes the row above it, so the hint has to say what that row
    // says the payload is: every session under this Kiro home, CLI replay logs
    // included, not only chats started here. A hint that says less asks for a yes
    // to more than it states, which is the one decision this feature exists for.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup, nightlySessions: false,
    } as never)
    await renderDrive('backup')

    const row = await screen.findByTestId('backup-nightly-sessions')
    expect(row.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_hint'),
    )
    expect(row.textContent).toContain(i18nT('apps.awsControl.console.backup_sessions_scope'))
    // And where it goes, naming the manual button rather than quoting it, so the
    // sentence cannot point at a label that has since been renamed.
    expect(row.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_destination', {
        button: i18nT('apps.awsControl.console.backup_run_now'),
      }),
    )
  })

  it('a host that cannot make the archive gets no pressable manual run either', async () => {
    // The notice says the schedule produces nothing here; the manual run answers
    // the same capability question and refuses with 501, so leaving the button
    // pressable offers a click that fails.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: true,
      nightlySessionsBlocked: 'host_unsupported',
    } as never)
    await renderDrive('backup')
    expect((await screen.findByTestId('backup-run-sessions')).hasAttribute('disabled')).toBe(true)
    // The snapshot row is untouched: its payload is producible on every platform.
    expect(screen.getByTestId('backup-run-snapshot').hasAttribute('disabled')).toBe(false)
  })

  it('a host that CAN make the archive keeps the manual run pressable', async () => {
    // The other direction, so the disable cannot be one that fires always.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup, nightlySessions: true, nightlySessionsBlocked: null,
    } as never)
    await renderDrive('backup')
    expect((await screen.findByTestId('backup-run-sessions')).hasAttribute('disabled')).toBe(false)
  })

  it('a grant on an account the schedule does not visit says so, and offers the manual run', async () => {
    // The grant is settable on any account while the nightly runs for one, so
    // this is the state where the switch is genuinely on and genuinely idle. The
    // manual run IS per-account, so the next step is a real one here.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: true,
      nightlySessionsBlocked: 'other_account',
    } as never)
    await renderDrive('backup')

    const blocked = await screen.findByTestId('backup-nightly-sessions-blocked')
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_other_account'),
    )
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_next'),
    )
  })

  it('the redaction hold names what the manual run does to the archive', async () => {
    // The nightly stands down here because the sessions archive has no redaction
    // seam, and the manual run uploads those same bytes unredacted. An operator who
    // asked for redaction is the LAST one who should learn that by pressing it, so
    // this state gets its own next step and must not fall back to the generic line.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: true,
      nightlySessionsBlocked: 'redaction_on',
    } as never)
    await renderDrive('backup')

    const blocked = await screen.findByTestId('backup-nightly-sessions-blocked')
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_redaction'),
    )
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_next_redaction'),
    )
    expect(blocked.textContent).not.toBe(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_redaction'),
    )
  })

  it('a host that cannot produce the archive is NOT offered the manual run', async () => {
    // The manual run answers the capability question the same way and refuses, so
    // offering it here names a click that fails. Paired with the test above, which
    // is what stops this being satisfied by dropping the next step everywhere.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightlySessions: true,
      nightlySessionsBlocked: 'host_unsupported',
    } as never)
    await renderDrive('backup')

    const blocked = await screen.findByTestId('backup-nightly-sessions-blocked')
    expect(blocked.textContent).toContain(i18nT(BLOCKED_HOST_KEY))
    expect(blocked.textContent).not.toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_next'),
    )
  })

  it('a snapshot grant on an account the schedule does not visit says so, next to its own switch', async () => {
    // The snapshot mirror of the transcript case. set_nightly records the grant
    // per account while the loop runs for the default one, so switching the
    // snapshot on while viewing a non-default account is the state where the
    // toggle reads back on and nothing is ever backed up. Without this the
    // console shows a running schedule that never visits the account.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightly: true,
      nightlyBlocked: 'other_account',
    } as never)
    await renderDrive('backup')

    const row = await screen.findByTestId('backup-nightly')
    const blocked = await screen.findByTestId('backup-nightly-blocked')
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_blocked_other_account'),
    )
    // The manual run is per-account, so the generic next step is a real one here.
    expect(blocked.textContent).toContain(
      i18nT('apps.awsControl.console.backup_nightly_sessions_blocked_next'),
    )
    // The switch still reads back as the owner set it.
    expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('true')
  })

  it('no snapshot blocked notice while the snapshot grant is off', async () => {
    // Nothing is withheld until the owner asks for it. Paired with the test above
    // so the notice cannot be one that renders unconditionally.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightly: false,
      nightlyBlocked: 'other_account',
    } as never)
    await renderDrive('backup')
    await screen.findByTestId('backup-nightly')
    expect(screen.queryByTestId('backup-nightly-blocked')).toBeNull()
  })

  it('no snapshot blocked notice when the snapshot grant is on and nothing is in the way', async () => {
    // The healthy install: the grant is on, the schedule reaches this account,
    // and the console says nothing rather than implying a problem.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightly: true,
      nightlyBlocked: null,
    } as never)
    await renderDrive('backup')
    await screen.findByTestId('backup-nightly')
    expect(screen.queryByTestId('backup-nightly-blocked')).toBeNull()
  })

  it('an older backend that omits nightlyBlocked shows no snapshot block', async () => {
    // The field is absent from a backend before this change; reading it as "not
    // blocked" is the safe direction -- it never fabricates a notice on an
    // install whose gateway cannot answer the question.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      nightly: true,
    } as never)
    await renderDrive('backup')
    await screen.findByTestId('backup-nightly')
    expect(screen.queryByTestId('backup-nightly-blocked')).toBeNull()
  })

  it('a rejected folder name is a notice WITHOUT the hand-off — nothing reached AWS', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-toggle'))
    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: 'bad/name' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))

    const notice = await screen.findByTestId('drive-folder-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.folder_bad_name'))
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(awsControlApi.driveFolderCreate).not.toHaveBeenCalled()
  })

  it('a refused restore is reported next to the caveat that predicts it', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: true,
      runs: {},
      install: { id: INSTALL_ID, label: 'This Mac' },
      remote: {
        snapshot: [{ key: 'snapshots/' + INSTALL_ID + '/2026-08-24.tar', size: 4096, modified: '2026-08-24T00:00:00Z', install: INSTALL_ID, origin: 'self' }],
        sessions: [],
        installs: [{ id: INSTALL_ID, label: 'This Mac', origin: 'self' }],
        others: 0,
        truncated: false,
        max: 8,
      },
    })
    vi.mocked(awsControlApi.backupRestore).mockRejectedValue(new AwsControlError('aws_call_failed', 502))
    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    fireEvent.click(within(archive).getByTestId('backup-restore'))

    expect(await screen.findByTestId('backup-restore-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.backup_restore_failed'),
    )
    expect(screen.queryByTestId('backup-restored')).toBeNull()
  })

  it('the remote-archive reason the backend reports inside a 200 travels as the message', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      remote: null,
      remoteError: 'AccessDenied: s3:ListBucket on backup/',
    })
    await renderDrive('backup')

    const notice = await screen.findByTestId('backup-remote-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.backup_remote_error'))
    expect(notice).toHaveTextContent('AccessDenied: s3:ListBucket on backup/')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    expect(consumeChatHandoff() ?? '').toContain('AccessDenied: s3:ListBucket on backup/')
  })

  it('a failed share-ledger read is a notice, never "no links"', async () => {
    vi.mocked(awsControlApi.shares).mockRejectedValue(new AwsControlError('http_500', 500))
    await renderDrive('access')

    expect(await screen.findByTestId('access-list-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.access_list_failed'),
    )
    expect(screen.queryByTestId('access-empty')).toBeNull()
  })

  it('a refused Forget keeps the row and says why', async () => {
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2099-01-01T00:00:00Z', note: '',
      }],
    })
    vi.mocked(awsControlApi.shareForget).mockRejectedValue(new AwsControlError('http_500', 500))
    await renderDrive('access')

    fireEvent.click(await screen.findByTestId('access-forget'))
    expect(await screen.findByTestId('access-forget-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.access_forget_failed'),
    )
    expect(screen.getByTestId('access-row')).toBeTruthy()
  })

  it('while the folder-name field is open, no Files notice offers the hand-off — the typed name survives', async () => {
    // Every notice on the pane shares the screen with that one draft, so the
    // gate is the disclosure, not the individual notice.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockRejectedValue(new AwsControlError('aws_call_failed', 502))
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-toggle'))
    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: 'quarterly' } })

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))

    const notice = await screen.findByTestId('drive-delete-error')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('drive-folder-name')).toHaveValue('quarterly')
  })
})
