import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// The section only reads the navigation + repo-list slice of the context.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const SettingsSection = (await import('../apps/issue-radar/components/SettingsSection')).default

const openSettings = vi.fn()
const onAddRepo = vi.fn()

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
    openSettings,
    onAddRepo,
  }
})

/** The active row is the one carrying the selected-state class. */
function activeRowLabels(): string[] {
  return Array.from(document.querySelectorAll('button'))
    .filter((b) => b.className.includes('bg-accent-subtle'))
    .map((b) => b.textContent ?? '')
}

describe('SettingsSection — navigation', () => {
  it('navigates the general rows with their own anchor', async () => {
    render(<SettingsSection />)
    await userEvent.click(screen.getByText('Repositories'))
    expect(openSettings).toHaveBeenCalledWith({ kind: 'general', anchor: 'repos' })
    await userEvent.click(screen.getByText('Account'))
    expect(openSettings).toHaveBeenCalledWith({ kind: 'general', anchor: 'account' })
  })

  it('carries provider and host into a repo target', async () => {
    // The slug alone does not identify a repo: the same owner/repo exists on
    // GitHub and on a self-managed GitLab, so a target without provider+host
    // would open the wrong repository's settings.
    render(<SettingsSection />)
    await userEvent.click(screen.getByText('zzq-org/beta-pkg'))
    expect(openSettings).toHaveBeenCalledWith({
      kind: 'repo', owner: 'zzq-org', repo: 'beta-pkg',
      provider: 'gitlab', host: 'gitlab.example.com',
    })
  })

  it('offers the connect action through the caller-supplied handler', async () => {
    render(<SettingsSection />)
    await userEvent.click(screen.getByText('Connect repo'))
    expect(onAddRepo).toHaveBeenCalledTimes(1)
    expect(openSettings).not.toHaveBeenCalled()
  })
})

describe('SettingsSection — active row', () => {
  it('marks the general row matching the current anchor', () => {
    render(<SettingsSection />)
    expect(activeRowLabels()).toEqual(['Account'])
  })

  it('marks only the repo row matching owner AND repo', () => {
    ctx.value = {
      ...ctx.value,
      settingsTarget: { kind: 'repo', owner: 'zzq-org', repo: 'beta-pkg' },
    }
    render(<SettingsSection />)
    expect(activeRowLabels()).toEqual(['zzq-org/beta-pkg'])
  })

  it('marks nothing while the main area is showing another view', () => {
    // The rail stays visible on every surface, so highlighting a settings row
    // from the issues view would claim a page the user is not on.
    ctx.value = { ...ctx.value, mainView: 'issues' }
    render(<SettingsSection />)
    expect(activeRowLabels()).toEqual([])
  })

  it('renders a row per connected repo, plus one connect action', () => {
    render(<SettingsSection />)
    expect(screen.getByText('zzq-org/alpha-pkg')).toBeInTheDocument()
    expect(screen.getByText('zzq-org/beta-pkg')).toBeInTheDocument()
  })
})
