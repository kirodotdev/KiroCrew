import { describe, it, expect, afterEach, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import FindingDetail, { type FindingDetailData } from './FindingDetail'

const FP = 'fp/with space:1'
const URL_PATH = `/api/apps/auto-improvement/findings/${encodeURIComponent(FP)}`

function stubFinding(status: number, finding?: FindingDetailData) {
  const fetchMock = vi.fn((url: string) => {
    if (url !== URL_PATH) return Promise.reject(new Error(`unexpected url ${url}`))
    return Promise.resolve({
      ok: status < 400,
      status,
      json: () => Promise.resolve({ finding }),
    })
  })
  vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
  return fetchMock
}

// The gate flag's icon carries its vote as a class: text-ok (passed), text-danger (failed),
// text-muted (not applicable, the record omits the flag).
function flagIcon(label: string): string {
  return screen.getByText(label).querySelector('svg')?.getAttribute('class') ?? ''
}

const FULL: FindingDetailData = {
  fp: FP,
  kind: 'bug',
  target: 'src/x.py',
  status: 'kept',
  note: 'kept after review',
  pr: 'https://code.example.test/pr/42',
  candidate: {
    signature: 'Null pointer when the queue is empty',
    hypothesis: 'The drain loop reads the head before checking length',
    reproducing_test: { test_id: 't1', test_path: 'test/test_queue_repro.py' },
  },
  gate: {
    passed: false,
    red: true,
    green: false,
    staygreen: true,
    build_ok: true,
    lint_ok: false,
    collected: true,
    detail: 'two stages disagreed',
    failing_tests: ['test_a', 'test_b', 'test_c', 'test_d', 'test_e', 'test_f'],
  },
  archive: { cycle: 7, primary_delta: '+1.5%', noise_band: '0.4%' },
  run: { branch: 'auto/fix-1', base_sha: '0123456789abcdef' },
  diff: '@@ -1 +1 @@\n-old\n+new',
  diffTruncated: true,
  prBody: 'Fixes the empty-queue read.',
  history: [
    { status: 'seen', ts: 1 },
    { status: 'gated_out', ts: 2 },
    { status: 'kept', ts: 3 },
  ],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('FindingDetail', () => {
  it('shows the loading label until the finding arrives', async () => {
    stubFinding(200, FULL)
    renderWithProviders(<FindingDetail fp={FP} />)
    expect(screen.getByText(i18nT('autoImprovement.loadingDetail'))).toBeTruthy()
    await screen.findByText(FULL.candidate!.signature!)
  })

  it('shows the unavailable label when the request fails', async () => {
    const fetchMock = stubFinding(500)
    renderWithProviders(<FindingDetail fp={FP} />)
    expect(await screen.findByText(i18nT('autoImprovement.detailUnavailable'))).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledWith(URL_PATH)
  })

  it('shows the unavailable label when the response carries no finding', async () => {
    stubFinding(200, undefined)
    renderWithProviders(<FindingDetail fp={FP} />)
    expect(await screen.findByText(i18nT('autoImprovement.detailUnavailable'))).toBeTruthy()
  })

  it('renders the defect, hypothesis, gate votes, failing tests and facts', async () => {
    stubFinding(200, FULL)
    renderWithProviders(<FindingDetail fp={FP} />)

    expect(await screen.findByText(FULL.candidate!.signature!)).toBeTruthy()
    expect(screen.getByText(FULL.candidate!.hypothesis!)).toBeTruthy()
    expect(screen.getByText(FULL.gate!.detail!)).toBeTruthy()
    expect(screen.getByText(FULL.candidate!.reproducing_test!.test_path!)).toBeTruthy()
    expect(screen.getByText('7')).toBeTruthy()
    expect(screen.getByText(/\+1\.5%/)).toBeTruthy()
    expect(screen.getByText(/0\.4%/)).toBeTruthy()
    expect(screen.getByText(`auto/fix-1 @ ${'0123456789'}`)).toBeTruthy()
    expect(screen.getByText(FP)).toBeTruthy()
    expect(screen.getByText(FULL.note!)).toBeTruthy()

    // Each gate flag shows its label with an icon whose class carries the vote.
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.red'))).toContain('text-ok')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.green'))).toContain('text-danger')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.staygreen'))).toContain('text-ok')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.build'))).toContain('text-ok')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.lint'))).toContain('text-danger')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.collects'))).toContain('text-ok')

    // Only the first five failing tests are named.
    const regressed = screen.getByText(/test_e/)
    expect(regressed.textContent).toContain('test_a, test_b, test_c, test_d, test_e')
    expect(regressed.textContent).not.toContain('test_f')
  })

  it('shows a gate flag the record omits as not applicable rather than as a failure', async () => {
    stubFinding(200, {
      ...FULL,
      gate: { passed: false, red: true, green: false, staygreen: true, build_ok: true, lint_ok: false },
    })
    renderWithProviders(<FindingDetail fp={FP} />)
    await screen.findByText(FULL.candidate!.signature!)
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.collects'))).toContain('text-muted')
    expect(flagIcon(i18nT('apps.autoImprovement.findingDetail.lint'))).toContain('text-danger')
  })

  it('links a drafted PR and offers the diff and PR body as disclosures', async () => {
    stubFinding(200, FULL)
    const { container } = renderWithProviders(<FindingDetail fp={FP} />)
    const link = (await screen.findByText(FULL.pr!)).closest('a')
    expect(link?.getAttribute('href')).toBe(FULL.pr)
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')

    const pre = container.querySelector('pre')
    expect(pre?.textContent).toContain('+new')
    expect(pre?.textContent).toContain(i18nT('autoImprovement.truncated'))
    expect(screen.getByText('Fixes the empty-queue read.')).toBeTruthy()
  })

  it('shows the queued badge instead of a link when the PR is only queued', async () => {
    stubFinding(200, { ...FULL, pr: 'QUEUED:cand-9' })
    renderWithProviders(<FindingDetail fp={FP} />)
    expect(await screen.findByText(i18nT('autoImprovement.queuedOnly'))).toBeTruthy()
    expect(document.querySelector('a[target="_blank"]')).toBeNull()
  })

  it('draws the status history as an arrowed sequence', async () => {
    stubFinding(200, FULL)
    renderWithProviders(<FindingDetail fp={FP} />)
    await screen.findByText('gated_out')
    const items = screen.getAllByRole('listitem')
    expect(items.map((li) => li.textContent?.replace('→', ''))).toEqual(['seen', 'gated_out', 'kept'])
    expect(screen.getAllByText('→')).toHaveLength(2)
  })

  it('omits the gate block, history and PR rows when those fields are absent', async () => {
    stubFinding(200, {
      fp: FP,
      candidate: { signature: 'Only a signature' },
      history: [{ status: 'kept' }],
    })
    renderWithProviders(<FindingDetail fp={FP} />)
    expect(await screen.findByText('Only a signature')).toBeTruthy()
    expect(screen.queryByText(i18nT('autoImprovement.gateHeading'))).toBeNull()
    expect(screen.queryByText(i18nT('autoImprovement.historyHeading'))).toBeNull()
    expect(screen.queryByText(i18nT('autoImprovement.colPr'))).toBeNull()
    expect(screen.queryByText(i18nT('autoImprovement.queuedOnly'))).toBeNull()
  })
})
