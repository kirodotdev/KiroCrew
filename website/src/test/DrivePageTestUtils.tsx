/**
 * Fixtures and mount helpers shared by DrivePage.test.tsx and the suites split from it
 * (DrivePage.library, .backup, .fileActions, .errorSurfaces and .previewSearch). Each of the six
 * keeps its own vi.mock blocks, so the `awsControlApi` imported here is that suite's mock.
 */
import { vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { AppIdentityProvider } from '../app-sdk/identity'
import type {
  DriveStatus, LibraryResponse, BackupStatus, SharesResponse,
} from '../apps/aws-control/types'
import { awsControlApi } from '../apps/aws-control/api'
import {
  DriveSectionView, LibrarySection, BackupSection, AccessSection,
} from '../apps/aws-control/DrivePage'

export const ACCOUNT_ID = '111122223333'

export const driveExists: Extract<DriveStatus, { exists: true }> = {
  exists: true,
  bucket: 'kirocrew-drive-abc123',
  region: 'us-west-2',
  usage: {
    bytes: 3_500_000_000,
    objects: 42,
    sections: {
      library: { objects: 10, bytes: 1_000_000 },
      drive: { objects: 30, bytes: 3_000_000_000 },
      backup: { objects: 2, bytes: 499_000_000 },
    },
  },
}

export const emptyLibrary: LibraryResponse = { artifacts: [] }
export const INSTALL_ID = 'a'.repeat(32)
/** An empty remote half in the new shape: no archives, no co-tenants. */
export const emptyRemote = { snapshot: [], sessions: [], installs: [], others: 0, truncated: false, max: 8 }
export const emptyBackup: BackupStatus = {
  nightly: false,
  runs: {},
  install: { id: INSTALL_ID, label: 'This Mac' },
  remote: emptyRemote,
}
export const noShares: SharesResponse = { shares: [] }

export function stubDrivePresent() {
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

/**
 * Mount ONE of the drive's rail panes directly.
 *
 * The drive ROOT page (three section cards plus the ledger, with internal
 * section state) was removed in the flat-rail IA refactor: the four sections
 * are now named exports rendered as their own panes. So instead of mounting
 * the root and clicking into a section, tests mount the section under test
 * with the props the old flow passed -- the account id, plus the bucket for
 * the two sections that render a CLI drawer. The shares-ledger tests, which
 * used to sit at the root, mount AccessSection.
 */
export async function renderDrive(section: 'drive' | 'library' | 'backup' | 'access') {
  const el =
    section === 'drive' ? <DriveSectionView account={ACCOUNT_ID} bucket={driveExists.bucket} />
    : section === 'library' ? <LibrarySection account={ACCOUNT_ID} bucket={driveExists.bucket} />
    : section === 'backup' ? <BackupSection account={ACCOUNT_ID} />
    : <AccessSection account={ACCOUNT_ID} />
  // Under the app's own identity, the way `BuiltinAppRoute` mounts these
  // sections: their queries are keyed through the host, so a bare render
  // resolves them unprefixed and the cache layout stops being the product's.
  renderWithProviders(
    <AppIdentityProvider appId="aws-control" origin="builtin">
      {el}
    </AppIdentityProvider>,
  )
}

/**
 * Open a per-item overflow menu and choose one of its items.
 *
 * Every per-item action in both folders lives behind a `⋮`, so a test that used
 * to click a bare button has to open the menu first. Enter on the trigger rather
 * than a click: the menu is a portaled Radix dropdown, and a bare click does not
 * open one in jsdom -- which is how the row-overflow tests in DrivePage.test.tsx
 * already drive theirs.
 *
 * `trigger` is an element rather than a test id because a listing with several
 * rows has several triggers and the caller has to say which.
 */
export async function chooseFromMenu(trigger: HTMLElement, itemTestId: string) {
  fireEvent.keyDown(trigger, { key: 'Enter' })
  fireEvent.click(await screen.findByTestId(itemTestId))
}
