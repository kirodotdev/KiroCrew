/**
 * The campaign detail's "View report" button opens and closes the report
 * section in place, so it is a disclosure: it carries `aria-expanded` and
 * points at the section with `aria-controls`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import ResearchLabPage from './ResearchLabPage'
import { api } from '../../api/client'

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

// `CampaignDetail` opens an SSE connection on mount; this inert stub keeps
// `new EventSource(...)` from throwing in jsdom.
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

describe('ResearchLabPage report toggle', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', StubEventSource)
    vi.mocked(api.researchCampaigns).mockResolvedValue([CAMPAIGN])
    vi.mocked(api.researchCampaign).mockResolvedValue(CAMPAIGN)
    vi.mocked(api.researchReportStatus).mockResolvedValue({ slug: null })
    vi.mocked(api.researchKnowledgeStatus).mockResolvedValue({ in_library: false })
    vi.mocked(api.researchReport).mockResolvedValue({ report: '# Section one\n\nBody one.' })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('reports its open state and names the section it controls', async () => {
    renderWithProviders(<ResearchLabPage />)
    fireEvent.click(await screen.findByText(CAMPAIGN.question))

    const toggle = await screen.findByRole('button', {
      name: i18nT('apps.autoResearch.researchLabPage.view_report'),
    })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const controlled = toggle.getAttribute('aria-controls')
    expect(controlled).toBeTruthy()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAccessibleName(i18nT('apps.autoResearch.researchLabPage.hide_report'))
    expect(document.getElementById(controlled!)).not.toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })
})
