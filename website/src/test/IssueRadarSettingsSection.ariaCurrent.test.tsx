/**
 * Issue Radar's settings rail opens one page of the main area per row and paints the open
 * row with a class. A screen reader gets no state from a class, and two repositories of
 * one slug read the same, so the open row is the only thing that tells them apart. The row
 * that is the open page carries `aria-current="page"`; no other row does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const SettingsSection = (await import('../apps/issue-radar/components/SettingsSection')).default

const REPOS = [
  { owner: 'zzq-org', repo: 'alpha-pkg', provider: 'github', host: 'github.com' },
  { owner: 'zzq-org', repo: 'beta-pkg', provider: 'gitlab', host: 'gitlab.example.com' },
]

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    repos: REPOS,
    mainView: 'settings',
    settingsTarget: { kind: 'general', anchor: 'account' },
    openSettings: vi.fn(),
    onAddRepo: vi.fn(),
  }
})

const rowOf = (label: string) => screen.getByText(label).closest('button') as HTMLButtonElement
const current = () => Array.from(document.querySelectorAll('button[aria-current]')).map(b => b.textContent)

describe('Issue Radar settings rail: the open page is the current page', () => {
  it('marks the open row with aria-current="page"', () => {
    render(<SettingsSection />)
    expect(rowOf('Account')).toHaveAttribute('aria-current', 'page')
  })

  it('marks no other row, the connect action included', () => {
    render(<SettingsSection />)
    expect(rowOf('Repositories')).not.toHaveAttribute('aria-current')
    expect(rowOf('zzq-org/alpha-pkg')).not.toHaveAttribute('aria-current')
    expect(rowOf('Connect repo')).not.toHaveAttribute('aria-current')
  })

  it('marks only the forge that is open when one slug exists on two', () => {
    ctx.value = {
      ...ctx.value,
      repos: [
        { owner: 'zzq-org', repo: 'shared', provider: 'github', host: 'github.com' },
        { owner: 'zzq-org', repo: 'shared', provider: 'gitlab', host: 'gitlab.example.com' },
      ],
      settingsTarget: { kind: 'repo', owner: 'zzq-org', repo: 'shared', provider: 'gitlab', host: 'gitlab.example.com' },
    }
    render(<SettingsSection />)
    const rows = screen.getAllByText('zzq-org/shared').map(el => el.closest('button') as HTMLButtonElement)
    expect(rows.map(r => r.hasAttribute('aria-current'))).toEqual([false, true])
  })

  it('marks nothing while the main area shows another view', () => {
    ctx.value = { ...ctx.value, mainView: 'issues' }
    render(<SettingsSection />)
    expect(current()).toEqual([])
  })
})
