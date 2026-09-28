/**
 * ResearchLabPage setup wizard: the step-3 review card names the model the
 * campaign will run on (agent mode only), or the inherit label when none is picked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import ResearchLabPage from './ResearchLabPage'
import { api } from '../../api/client'

vi.mock('../../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [{ name: 'auto' }, { name: 'model-x' }],
}))

// Native-select stand-in: the review line is under test, not the Radix picker.
vi.mock('../../components/SimpleSelect', () => ({
  default: ({ options, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; 'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} value={value} onChange={e => onChange(e.target.value)}>
      {options.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  ),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return { ...mod, api: { ...mod.api, researchCampaigns: vi.fn(), researchValidate: vi.fn() } }
})

const k = (key: string) => i18nT(`apps.autoResearch.researchLabPage.${key}`)

async function openReview(setup: () => void) {
  renderWithProviders(<ResearchLabPage />)
  fireEvent.click(await screen.findByRole('button', { name: k('new_campaign_2') }))
  fireEvent.change(screen.getByLabelText(k('what_do_you_want_to_research')), {
    target: { value: 'How do other teams handle API rate limiting?' },
  })
  setup()
  fireEvent.click(screen.getByRole('button', { name: k('next') }))
  fireEvent.click(screen.getByRole('button', { name: k('next') }))
  await screen.findByText(k('all_checks_passed'))
}

describe('ResearchLabPage review card model line', () => {
  beforeEach(() => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([])
    vi.mocked(api.researchValidate).mockResolvedValue({
      errors: [], warnings: [], can_start: true, estimated_duration_min: 5,
    })
  })

  it('shows the inherit label when no model is picked', async () => {
    await openReview(() => {})
    expect(screen.getByText(i18nT('apps.autoResearch.researchLabPage.model_line', { model: k('model_default_inherit') }))).toBeInTheDocument()
  })

  it('shows the picked model', async () => {
    await openReview(() => {})
    fireEvent.click(screen.getByRole('button', { name: k('back') }))
    fireEvent.change(screen.getByLabelText(k('model')), { target: { value: 'model-x' } })
    fireEvent.click(screen.getByRole('button', { name: k('next') }))
    await screen.findByText(k('all_checks_passed'))
    expect(screen.getByText(i18nT('apps.autoResearch.researchLabPage.model_line', { model: 'model-x' }))).toBeInTheDocument()
  })

  it('omits the line in workflow mode', async () => {
    await openReview(() => fireEvent.click(screen.getByText(k('dynamic_workflow'))))
    expect(screen.queryByText(i18nT('apps.autoResearch.researchLabPage.model_line', { model: k('model_default_inherit') }))).not.toBeInTheDocument()
  })
})
