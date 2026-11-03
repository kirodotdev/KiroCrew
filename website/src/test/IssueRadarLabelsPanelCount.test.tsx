/**
 * The tagging view's label header counts the repo's labels inside one
 * translated string, so a language can place the number where its grammar
 * needs it and pick the plural form for that count.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// `/all` for every catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'

vi.mock('../apps/issue-radar/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/issue-radar/api')>()
  return {
    ...actual,
    issueRadarApi: {
      ...actual.issueRadarApi,
      // Never settles: the header under test renders without recommendations.
      getRecommendations: () => new Promise(() => {}),
    },
  }
})

const LabelsPanel = (await import('../apps/issue-radar/views/tagging/LabelsPanel')).default

const REF = { owner: 'o', repo: 'r', provider: 'github' as const, host: 'github.com' }
const LABELS = [
  { name: 'bug', color: 'ff0000', description: '' },
  { name: 'docs', color: '00ff00', description: '' },
]

function renderPanel(labels = LABELS) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LabelsPanel
        repoRef={REF}
        labels={labels}
        labelsKnown
        countByLabel={new Map()}
        canWrite={false}
        aiLanguage=""
      />
    </QueryClientProvider>,
  )
}

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('LabelsPanel label count', () => {
  it('keeps the English wording', () => {
    renderPanel()
    expect(screen.getByText('2 defined · by open-issue count')).toBeTruthy()
  })

  it('counts the labels in the interface language', async () => {
    await i18next.changeLanguage('de')
    renderPanel()
    expect(screen.getByText('2 definiert · nach Anzahl offener Issues')).toBeTruthy()
  })
})
