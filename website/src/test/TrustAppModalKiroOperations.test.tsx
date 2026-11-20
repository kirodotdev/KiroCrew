/**
 * `permissions.kiroOperations` before and after the owner says yes.
 *
 * The trust gate lists each declared Kiro operation, because the install consent
 * approves the names it shows. On the app's page, a name an update added is held
 * back and offered for approval with the other staged grants.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'
import { stagedGrantsOf } from '../pages/appDetailPageStagedGrants'
import { KiroOperationsBox } from '../pages/AppDetailGrantBoxes'
import type { AppInfo } from '../pages/appDetailPageTypes'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'widgets',
  displayName: 'Widgets',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/widgets',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('Kiro operations at the trust gate', () => {
  it('lists every declared operation with the note that the app never gets the sign-in', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ kiroOperations: ['GetWidget', 'ListWidgets'] }) })
    const box = within(dialog.getByTestId('trust-kiro-operations'))
    expect(box.getByText(`${K}.kiro_operations_heading`)).toBeTruthy()
    expect(box.getByText('GetWidget')).toBeTruthy()
    expect(box.getByText('ListWidgets')).toBeTruthy()
    expect(box.getByText(`${K}.kiro_operations_note`)).toBeTruthy()
  })

  it('shows no box for an app that declares none', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({}) })
    expect(dialog.queryByTestId('trust-kiro-operations')).toBeNull()
  })
})

describe('Kiro operations in the permissions card', () => {
  it('lists every declared name and marks the held-back one', () => {
    render(<KiroOperationsBox grant={{ approved: ['GetWidget'], staged: ['ListWidgets'] }} />)
    const box = within(screen.getByTestId('kiro-operations'))
    expect(box.getByTestId('kiro-operation-GetWidget').textContent).toBe('GetWidget')
    expect(box.getByTestId('kiro-operation-ListWidgets').textContent).toContain('pages.appDetailPage.grant_not_approved')
  })

  it('renders nothing when the app declares none', () => {
    const { container } = render(<KiroOperationsBox grant={undefined} />)
    expect(container.innerHTML).toBe('')
  })
})

describe('Kiro operations on the app page', () => {
  it('offers the held-back names for approval', () => {
    const app = { name: 'widgets', kiroOperationsGrant: { approved: ['GetWidget'], staged: ['ListWidgets'] } } as unknown as AppInfo
    const staged = stagedGrantsOf(app)
    expect(staged.stagedKiroOperations).toEqual(['ListWidgets'])
    expect(staged.hasStagedGrants).toBe(true)
  })

  it('holds nothing back once every name is approved', () => {
    const app = { name: 'widgets', kiroOperationsGrant: { approved: ['GetWidget'], staged: [] } } as unknown as AppInfo
    expect(stagedGrantsOf(app).hasStagedGrants).toBe(false)
  })
})
