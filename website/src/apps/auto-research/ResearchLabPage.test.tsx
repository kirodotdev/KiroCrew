/**
 * ResearchLabPage — the per-report-section "Copy" button (`ReportSections`).
 *
 * This was the worst case in the sweep: it called `navigator.clipboard?.writeText(text)`
 * and then UNCONDITIONALLY rendered the "Copied" confirmation, so a blocked or
 * missing clipboard showed success over an empty clipboard — worse than doing
 * nothing. It now routes through the shared `copyToClipboard` helper and gates
 * the confirmation on the resolved boolean.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import ResearchLabPage from './ResearchLabPage'
import { api } from '../../api/client'

vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../../utils/clipboard'

vi.mock('../../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      researchCampaigns: vi.fn(),
      researchCampaign: vi.fn(),
      researchReport: vi.fn(),
      researchReportStatus: vi.fn(),
      researchKnowledgeStatus: vi.fn(),
    },
  }
})

// `CampaignDetail` opens a live SSE connection on mount. No push is needed for
// this test, so the stub is inert — just enough that `new EventSource(...)`
// does not throw in the jsdom environment.
class StubEventSource {
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  close() {}
}

const CAMPAIGN = {
  id: 'camp-1',
  name: 'API rate limiting',
  question: 'How do other teams handle API rate limiting?',
  sub_questions: '[]',
  sources: '',
  max_cycles: 10,
  idle_secs: 60,
  status: 'complete',
  total_cycles: 3,
  findings: [],
}

describe('ResearchLabPage report section copy', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', StubEventSource)
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    vi.mocked(api.researchCampaigns).mockResolvedValue([CAMPAIGN])
    vi.mocked(api.researchCampaign).mockResolvedValue(CAMPAIGN)
    vi.mocked(api.researchReportStatus).mockResolvedValue({ slug: null })
    vi.mocked(api.researchKnowledgeStatus).mockResolvedValue({ in_library: false })
    vi.mocked(api.researchReport).mockResolvedValue({
      report: '# Section one\n\nBody one.\n\n# Section two\n\nBody two.',
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  async function openReport() {
    renderWithProviders(<ResearchLabPage />)
    const row = await screen.findByText(CAMPAIGN.question)
    fireEvent.click(row)
    const viewReport = await screen.findByText(i18nT('apps.autoResearch.researchLabPage.view_report'))
    fireEvent.click(viewReport)
    return screen.findAllByTitle(
      i18nT('apps.autoResearch.researchLabPage.copy_this_section_s_markdown_to_paste_into_chat'),
    )
  }

  it('copies the exact section text through the shared helper and confirms, then reverts', async () => {
    const buttons = await openReport()
    fireEvent.click(buttons[0])

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('# Section one\n\nBody one.'))
    await waitFor(() =>
      expect(screen.getByText(i18nT('apps.autoResearch.researchLabPage.copied'))).toBeInTheDocument(),
    )

    vi.advanceTimersByTime(1600)
    await waitFor(() => {
      expect(screen.queryByText(i18nT('apps.autoResearch.researchLabPage.copied'))).toBeNull()
    })
  })

  it('renders no confirmation when the copy fails — the exact defect this fix closes', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    const buttons = await openReport()
    fireEvent.click(buttons[0])

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    // Before the fix this assertion would fail: the old code rendered "Copied"
    // unconditionally regardless of whether the clipboard write ever happened.
    expect(screen.queryByText(i18nT('apps.autoResearch.researchLabPage.copied'))).toBeNull()
  })
})
