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

  it('marks only the repo row matching the full identity', () => {
    // The forge is part of the fixture because it is part of what the app writes:
    // `openSettings` is called with `provider` and `host` from the row (pinned by
    // "carries provider and host into a repo target" above), so a target without
    // them is not a shape this component produces.
    ctx.value = {
      ...ctx.value,
      settingsTarget: {
        kind: 'repo', owner: 'zzq-org', repo: 'beta-pkg',
        provider: 'gitlab', host: 'gitlab.example.com',
      },
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

  /**
   * One slug on two forges must light ONE row.
   *
   * The rows are labelled `owner/repo`, which is identical for both, so a
   * highlight matched on the slug marks the open page twice and the user cannot
   * tell which repository's settings they are editing. Same reason the row key is
   * the scope key rather than the slug: two rows keyed alike is a duplicate React
   * key, which is a defect the same fixture reaches.
   */
  it('highlights only the open forge when one slug exists on two', () => {
    const SAME_SLUG = [
      { owner: 'zzq-org', repo: 'shared', provider: 'github', host: 'github.com' },
      { owner: 'zzq-org', repo: 'shared', provider: 'gitlab', host: 'gitlab.example.com' },
    ]
    ctx.value = {
      ...ctx.value,
      repos: SAME_SLUG,
      settingsTarget: {
        kind: 'repo', owner: 'zzq-org', repo: 'shared',
        provider: 'gitlab', host: 'gitlab.example.com',
      },
    }
    render(<SettingsSection />)
    // Both rows exist and read the same...
    expect(screen.getAllByText('zzq-org/shared')).toHaveLength(2)
    // ...and exactly one of them is marked as the page being edited.
    expect(activeRowLabels()).toHaveLength(1)
  })

  it('highlights neither row when the open page is a different repository', () => {
    ctx.value = {
      ...ctx.value,
      settingsTarget: {
        kind: 'repo', owner: 'zzq-org', repo: 'alpha-pkg',
        // The slug names the GitHub row in REPOS, but on a self-managed GitLab
        // instance that is a different repository, so nothing should light up.
        provider: 'gitlab', host: 'gitlab.example.com',
      },
    }
    render(<SettingsSection />)
    expect(activeRowLabels()).toHaveLength(0)
  })

  /**
   * A forge-less target reads as public GitHub, and that is the honest reading
   * rather than a gap.
   *
   * `settingsTarget` is persisted, so a value written before the forge fields
   * existed can still be restored — the same legacy shape `loadActiveRepo` accepts.
   * It matches a GitHub row, because absent means public GitHub. It does NOT match
   * a GitLab row of the same slug, so on such an install the page is open with no
   * row marked until the next click. That is a narrow, self-correcting cosmetic
   * residue, and the alternative is the defect above: matching the slug lights
   * BOTH rows of a mixed-forge slug and the user cannot tell which page they are on.
   */
  it('treats a legacy forge-less target as public GitHub', () => {
    ctx.value = {
      ...ctx.value,
      settingsTarget: { kind: 'repo', owner: 'zzq-org', repo: 'alpha-pkg' },
    }
    render(<SettingsSection />)
    // alpha-pkg is the GitHub row, so the legacy pointer still finds it.
    expect(activeRowLabels()).toEqual(['zzq-org/alpha-pkg'])
  })
})
