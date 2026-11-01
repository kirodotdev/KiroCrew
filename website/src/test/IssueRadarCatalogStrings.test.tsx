/**
 * Issue Radar strings that used to be English in every language: the Connect
 * button of both connect hosts, the Account / Repositories settings rows, the
 * Review button's failed-lookup hint and the provider's change-request nouns.
 *
 * en-XA brackets every catalog value, so a bracketed render proves the string
 * came from the catalog; an English literal would render bare. The review
 * prompt is the opposite case: it is model-facing text and must stay English
 * whatever the interface language.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// `/all` for every catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const flow = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/ConnectPanel', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/issue-radar/ConnectPanel')>()),
  default: () => null,
  useConnectFlow: () => flow.value,
}))

const getInvestigation = vi.fn()
vi.mock('../apps/issue-radar/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/issue-radar/api')>()
  return { ...actual, issueRadarApi: { ...actual.issueRadarApi, getInvestigation: (...a: unknown[]) => getInvestigation(...a) } }
})
vi.mock('../apps/issue-radar/lib/review', () => ({
  useReviewPr: () => ({ reviewPr: vi.fn(), busy: false, error: null, concludedFor: null, openOlderSessions: vi.fn() }),
}))
vi.mock('../apps/issue-radar/components/AgentSessionButton', () => ({
  default: ({ startHint }: { startHint: string }) => <span data-testid="start-hint">{startHint}</span>,
}))

const ConnectRepoModal = (await import('../apps/issue-radar/ConnectRepoModal')).default
const WelcomeCarousel = (await import('../apps/issue-radar/WelcomeCarousel')).default
const SettingsSection = (await import('../apps/issue-radar/components/SettingsSection')).default
const ReviewButton = (await import('../apps/issue-radar/components/ReviewButton')).default
const { providerTerms } = await import('../apps/issue-radar/lib/links')
const { buildReviewPrompt } = await import('../apps/issue-radar/lib/review.prompt')

const GH = { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }
const GL = { owner: 'grp', repo: 'proj', provider: 'gitlab' as const, host: 'gitlab.com' }
const PULL = {
  number: 7, title: 'Fix it', state: 'open', labels: [], author: 'octo', url: 'https://github.com/acme/widget/pull/7',
} as unknown as Parameters<typeof buildReviewPrompt>[3]

/** A bracketed en-XA value, which only the catalog can produce. */
const PSEUDO = /^\[[^[\]]+\]$/

function withTargets(n: number) {
  flow.value = {
    provider: 'github',
    targets: Array.from({ length: n }, (_, i) => `acme/repo-${i}`),
    pending: false,
    progress: null,
    submit: vi.fn(),
  }
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>
}

beforeEach(async () => {
  Element.prototype.scrollIntoView = () => {}
  ctx.value = {
    openIssues: vi.fn(), setSelectedIssue: vi.fn(), setStateFilter: vi.fn(), setQuery: vi.fn(),
    clearFilters: vi.fn(), setSelectedPull: vi.fn(), setPrQuery: vi.fn(), clearPrFilters: vi.fn(),
    repos: [], mainView: 'settings', settingsTarget: { kind: 'general', anchor: 'account' },
    openSettings: vi.fn(), onAddRepo: vi.fn(),
  }
  await i18next.changeLanguage('en-XA')
})

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('Issue Radar Connect button', () => {
  it.each([1, 3])('reads from the catalog in the connect dialog with %i repo(s) picked', (n) => {
    withTargets(n)
    render(<ConnectRepoModal onConnected={vi.fn()} onClose={vi.fn()} />)
    const button = screen.getByRole('button', { name: n > 1 ? /Çøññèçţ 3/ : /Çøññèçţ/ })
    expect(button.textContent).toMatch(PSEUDO)
  })

  it.each([1, 3])('reads from the catalog on the welcome carousel with %i repo(s) picked', async (n) => {
    withTargets(n)
    const user = userEvent.setup()
    render(<WelcomeCarousel onConnected={vi.fn()} />)
    // Walk to the last slide, where the Connect button lives.
    for (let i = 0; i < 10 && !screen.queryByRole('button', { name: /Çøññèçţ/ }); i++) {
      await user.click(screen.getByRole('button', { name: /Ñèẋţ/ }))
    }
    const button = screen.getByRole('button', { name: n > 1 ? /Çøññèçţ 3/ : /Çøññèçţ/ })
    expect(button.textContent).toMatch(PSEUDO)
  })

  it('keeps the English wording', async () => {
    await i18next.changeLanguage('en')
    withTargets(3)
    const { unmount } = render(<ConnectRepoModal onConnected={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Connect 3' })).toBeTruthy()
    unmount()
    withTargets(1)
    render(<ConnectRepoModal onConnected={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
  })
})

describe('Issue Radar settings rail', () => {
  it('labels the Account and Repositories rows from the catalog', () => {
    render(<SettingsSection />)
    for (const label of ['[Àççøùñţ ···········]', '[Ŕèþøşìţøŕìèş ···········]']) {
      expect(screen.getByText(label).closest('button')).toBeTruthy()
    }
  })
})

describe('Issue Radar Review button', () => {
  it('explains a failed session lookup from the catalog', async () => {
    getInvestigation.mockRejectedValue(new Error('boom'))
    render(wrap(<ReviewButton repoRef={GH} pull={PULL} />))
    await waitFor(() => expect(screen.getByTestId('start-hint').textContent).toMatch(/^\[Çøùĺð ñøţ çĥèçķ/))
  })
})

describe('provider change-request nouns', () => {
  it('follow the interface language', () => {
    for (const ref of [GH, GL]) {
      const t = providerTerms(ref)
      for (const noun of [t.changeRequest, t.changeRequestTitle, t.changeRequestPlural, t.changeRequestPluralTitle]) {
        expect(noun, `${ref.provider}: ${noun}`).toMatch(PSEUDO)
      }
    }
  })

  it('stay English inside the model-facing review prompt', () => {
    expect(buildReviewPrompt(GH, 'acme', 'widget', PULL)).toContain('GitHub pull request #7')
    expect(buildReviewPrompt(GL, 'grp', 'proj', PULL)).toContain('Review this merge request')
    expect(buildReviewPrompt(GL, 'grp', 'proj', PULL)).not.toMatch(/ɱèŕğè/)
  })
})
