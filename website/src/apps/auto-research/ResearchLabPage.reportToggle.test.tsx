/**
 * The campaign detail's "View report" button, its long-question "Show more"
 * button, each finding card and the guidance list open and close content in
 * place, so each is a disclosure: it carries `aria-expanded`, points at what it
 * reveals with `aria-controls`, and keeps its chevron hidden from assistive
 * tech.
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

describe('ResearchLabPage disclosures', () => {
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

  it('marks the long-question Show more toggle as a disclosure of the question', async () => {
    const longCampaign = { ...CAMPAIGN, question: `How do other teams handle API rate limiting? ${'Detail. '.repeat(40)}`.trim() }
    vi.mocked(api.researchCampaigns).mockResolvedValue([longCampaign])
    vi.mocked(api.researchCampaign).mockResolvedValue(longCampaign)
    renderWithProviders(<ResearchLabPage />)
    fireEvent.click(await screen.findByText(longCampaign.question))

    const toggle = await screen.findByRole('button', {
      name: i18nT('apps.autoResearch.researchLabPage.show_more'),
    })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const controlled = toggle.getAttribute('aria-controls')
    expect(controlled).toBeTruthy()
    expect(document.getElementById(controlled!)).toHaveTextContent(longCampaign.question)
    expect(toggle.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAccessibleName(i18nT('apps.autoResearch.researchLabPage.show_less'))
    expect(toggle.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('marks a finding card and the guidance list as disclosures', async () => {
    const finding = {
      cycle: 1, summary: 'Token buckets dominate.', sources_checked: [], sources_empty: [],
      new_findings_count: 1, evidence_strength: 'strong', key_insight: 'Token buckets win',
    }
    const running = { ...CAMPAIGN, status: 'running', findings: [finding] }
    vi.mocked(api.researchCampaigns).mockResolvedValue([running])
    vi.mocked(api.researchCampaign).mockResolvedValue(running)
    renderWithProviders(<ResearchLabPage />)
    fireEvent.click(await screen.findByText(running.question))

    const card = await screen.findByRole('button', { name: /Token buckets win/ })
    const guidance = await screen.findByRole('button', {
      name: i18nT('apps.autoResearch.researchLabPage.sub_questions_guidance_count', { count: 0 }),
    })
    for (const toggle of [card, guidance]) {
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      expect(toggle.firstElementChild?.tagName.toLowerCase()).toBe('svg')
      expect(toggle.firstElementChild).toHaveAttribute('aria-hidden', 'true')
      fireEvent.click(toggle)
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      expect(document.getElementById(toggle.getAttribute('aria-controls')!)).not.toBeNull()
      fireEvent.click(toggle)
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
    }
  })
})
