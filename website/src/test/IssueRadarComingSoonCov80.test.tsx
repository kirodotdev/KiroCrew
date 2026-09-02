import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Radar } from 'lucide-react'

// SettingsView is a pure router between two pages; both are mocked so what is
// asserted is the routing decision and the remount key, not either page's body.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))
vi.mock('../apps/issue-radar/views/settings/RepoSettings', () => ({
  default: ({ repoRef }: { repoRef: { owner: string; repo: string } }) => (
    <div data-testid="repo-page">{`${repoRef.owner}/${repoRef.repo}`}</div>
  ),
}))
vi.mock('../apps/issue-radar/views/settings/GeneralSettings', () => ({
  default: ({ anchor }: { anchor: string }) => <div data-testid="general-page">{anchor}</div>,
}))

const ComingSoon = (await import('../apps/issue-radar/views/ComingSoon')).default
const SettingsView = (await import('../apps/issue-radar/views/SettingsView')).default

beforeEach(() => {
  ctx.value = {}
})

describe('ComingSoon', () => {
  it('renders the caller-supplied heading, blurb and icon', () => {
    const { container } = render(
      <ComingSoon icon={Radar} title="zzq-heading" blurb="zzq-blurb-body" />,
    )
    expect(screen.getByRole('heading', { name: 'zzq-heading' })).toBeInTheDocument()
    expect(screen.getByText('zzq-blurb-body')).toBeInTheDocument()
    // The icon is the caller's, not a hardcoded one — a placeholder view that
    // always drew the same glyph would be indistinguishable between surfaces.
    expect(container.querySelector('svg')).not.toBeNull()
  })
})

describe('SettingsView', () => {
  it('routes a repo target to the repo page, keyed on the slug', () => {
    ctx.value = { settingsTarget: { kind: 'repo', owner: 'zzq-owner', repo: 'zzq-repo' } }
    render(<SettingsView />)
    expect(screen.getByTestId('repo-page')).toHaveTextContent('zzq-owner/zzq-repo')
    expect(screen.queryByTestId('general-page')).toBeNull()
  })

  it('routes a general target to the general page, forwarding its anchor', () => {
    ctx.value = { settingsTarget: { kind: 'general', anchor: 'repos' } }
    render(<SettingsView />)
    expect(screen.getByTestId('general-page')).toHaveTextContent('repos')
    expect(screen.queryByTestId('repo-page')).toBeNull()
  })

  it('falls back to the account anchor when the target carries none', () => {
    // A persisted `{kind:'general'}` written before anchors existed has no
    // anchor; defaulting is what stops the page scrolling nowhere.
    ctx.value = { settingsTarget: { kind: 'general' } }
    render(<SettingsView />)
    expect(screen.getByTestId('general-page')).toHaveTextContent('account')
  })
})
