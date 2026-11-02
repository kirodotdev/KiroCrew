import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { REFRESH_DEFAULTS, AI_LANGUAGE_FOLLOW } from '../../lib/format'
import { i18nT } from '../../../../i18n/t'
import GeneralSettings from './GeneralSettings'

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../../context', () => ({ useIssueRadar: () => ctx.value }))

const REPO = { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }
const ACTIVE = { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }

let setRefreshPrefs: ReturnType<typeof vi.fn>
let openSettings: ReturnType<typeof vi.fn>
let onAddRepo: ReturnType<typeof vi.fn>

function mount(repos: unknown[] = []) {
  setRefreshPrefs = vi.fn()
  openSettings = vi.fn()
  onAddRepo = vi.fn()
  ctx.value = {
    me: 'octocat',
    repos,
    active: ACTIVE,
    onAddRepo,
    openSettings,
    refreshPrefs: REFRESH_DEFAULTS,
    setRefreshPrefs,
    aiLanguage: AI_LANGUAGE_FOLLOW,
    setAiLanguage: vi.fn(),
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <GeneralSettings anchor="repos" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Element.prototype.scrollIntoView = () => {}
})

describe('GeneralSettings repository list', () => {
  it('opens that repo settings page when its card is clicked', () => {
    mount([REPO])
    fireEvent.click(screen.getByRole('button', { name: /acme\/widget/ }))
    expect(openSettings).toHaveBeenCalledWith({
      kind: 'repo',
      owner: 'acme',
      repo: 'widget',
      provider: 'github',
      host: 'github.com',
    })
  })

  it('hands the connect-another-repo button to onAddRepo', () => {
    mount([REPO])
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.issueRadar.views.settings.generalSettings.connect_another_repo') }))
    expect(onAddRepo).toHaveBeenCalledTimes(1)
    expect(openSettings).not.toHaveBeenCalled()
  })
})

describe('GeneralSettings background refresh toggles', () => {
  it('flips the background polling preference from its current value', () => {
    mount()
    const [pollSwitch] = screen.getAllByRole('switch')
    fireEvent.click(pollSwitch)
    expect(setRefreshPrefs).toHaveBeenCalledWith({ pollInBackground: !REFRESH_DEFAULTS.pollInBackground })
  })

  it('flips the pull-request prefetch preference from its current value', () => {
    mount()
    const prefetchSwitch = screen.getAllByRole('switch')[1]
    fireEvent.click(prefetchSwitch)
    expect(setRefreshPrefs).toHaveBeenCalledWith({ prefetchPulls: !REFRESH_DEFAULTS.prefetchPulls })
  })
})
