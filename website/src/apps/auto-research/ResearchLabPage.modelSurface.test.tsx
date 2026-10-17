/**
 * ResearchLabPage model surfaces after creation: the list rows and the detail
 * name the campaign's model pick, the fork flow starts on the parent's model
 * (pre-selected, editable) and sends what the user leaves selected, and a
 * withheld pick on an active campaign shows a notice naming the pick and what
 * actually runs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import ResearchLabPage from './ResearchLabPage'
import { api } from '../../api/client'

vi.mock('../../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [{ name: 'auto' }, { name: 'model-x' }, { name: 'model-y' }],
}))

// Native-select stand-in: the selected value is under test, not the Radix picker.
vi.mock('../../components/SimpleSelect', () => ({
  default: ({ options, value, onChange, clearLabel, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; clearLabel?: string; 'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} value={value} onChange={e => onChange(e.target.value)}>
      {clearLabel !== undefined && <option value="">{clearLabel}</option>}
      {options.map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  ),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      researchCampaigns: vi.fn(),
      researchCampaign: vi.fn(),
      researchAction: vi.fn(),
      researchGrillExpand: vi.fn(),
      researchKnowledgeStatus: vi.fn(() => Promise.resolve({ in_library: false })),
      researchReportStatus: vi.fn(() => Promise.resolve({ slug: null })),
    },
  }
})

const k = (key: string, opts?: Record<string, unknown>) => i18nT(`apps.autoResearch.researchLabPage.${key}`, opts)

const base = {
  name: 'Rate limits', question: 'How do other teams handle API rate limiting?', sub_questions: '[]',
  sources: '[]', max_cycles: 10, idle_secs: 60, total_cycles: 3, findings: [],
}
const parent = { ...base, id: 'aaaa1111', status: 'complete', model: 'model-x', model_withheld: null, served_model: '' }

async function openFork() {
  vi.mocked(api.researchCampaigns).mockResolvedValue([parent])
  vi.mocked(api.researchCampaign).mockResolvedValue(parent)
  renderWithProviders(<ResearchLabPage />)
  fireEvent.click(await screen.findByText(parent.question))
  fireEvent.click(await screen.findByRole('button', { name: k('fork_challenge') }))
  return (await screen.findByLabelText(k('model'))) as HTMLSelectElement
}

async function submitFork() {
  fireEvent.click(screen.getByRole('button', { name: k('challenge_findings') }))
  await screen.findByDisplayValue('What do peers use?')
  fireEvent.click(screen.getByRole('button', { name: /Fork with 1/ }))
  await waitFor(() => expect(api.researchAction).toHaveBeenCalledWith(parent.id, 'fork', expect.anything()))
  return vi.mocked(api.researchAction).mock.calls.find(c => c[1] === 'fork')![2] as { model?: string }
}

describe('ResearchLabPage campaign model surfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.researchAction).mockResolvedValue({ id: 'bbbb2222' })
    vi.mocked(api.researchGrillExpand).mockResolvedValue({
      nodes: [{ id: 'r1', parent: null, kind: 'research', text: 'What do peers use?', recommended: '', answer: '', origin: 'grill', status: 'promoted' }],
    })
  })

  it('fork pre-selects the parent campaign model and sends it', async () => {
    const select = await openFork()
    expect(select.value).toBe('model-x')
    expect(screen.getByText(k('model_fork_hint'))).toBeInTheDocument()
    expect((await submitFork()).model).toBe('model-x')
  })

  it('fork sends the model the user picks instead', async () => {
    const select = await openFork()
    fireEvent.change(select, { target: { value: 'model-y' } })
    // The "same model as the original" hint is false once the pick changes.
    expect(screen.queryByText(k('model_fork_hint'))).not.toBeInTheDocument()
    expect((await submitFork()).model).toBe('model-y')
  })

  it('history rows and the detail name the pick as the pick', async () => {
    vi.mocked(api.researchCampaigns).mockResolvedValue([parent])
    vi.mocked(api.researchCampaign).mockResolvedValue(parent)
    renderWithProviders(<ResearchLabPage />)
    expect(await screen.findByText(k('model_line_picked', { model: 'model-x' }))).toBeInTheDocument()
    fireEvent.click(screen.getByText(parent.question))
    expect(await screen.findByRole('button', { name: k('fork_challenge') })).toBeInTheDocument()
    expect(screen.getByText(k('model_line_picked', { model: 'model-x' }))).toBeInTheDocument()
  })

  it('a withheld pick shows a notice naming the pick and the served model', async () => {
    const running = { ...parent, status: 'running', model_withheld: true, served_model: 'model-default' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([running])
    vi.mocked(api.researchCampaign).mockResolvedValue(running)
    renderWithProviders(<ResearchLabPage />)
    const text = k('model_withheld_notice', { requested: 'model-x', actual: 'model-default' })
    expect(await screen.findByText(text)).toBeInTheDocument()
    // The line names the pick as the pick, never as the model that runs.
    expect(screen.getByText(k('model_line_picked', { model: 'model-x' }))).toBeInTheDocument()
    expect(screen.queryByText(k('model_line', { model: 'model-x' }))).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(running.question))
    await screen.findByRole('button', { name: new RegExp(k('pause')) })
    expect(screen.getByText(text)).toBeInTheDocument()
    expect(screen.getByText(k('model_line_picked', { model: 'model-x' }))).toBeInTheDocument()
  })

  it('a finished campaign shows no notice from a verdict its worker left behind', async () => {
    const finished = { ...parent, model_withheld: true, served_model: 'model-default' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([finished])
    vi.mocked(api.researchCampaign).mockResolvedValue(finished)
    renderWithProviders(<ResearchLabPage />)
    expect(await screen.findByText(k('model_line_picked', { model: 'model-x' }))).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(finished.question))
    fireEvent.click(await screen.findByRole('button', { name: k('fork_challenge') }))
    await screen.findByLabelText(k('model'))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('a withheld pick with no known served model names the default model', async () => {
    const running = { ...parent, status: 'running', model_withheld: true, served_model: '' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([running])
    renderWithProviders(<ResearchLabPage />)
    expect(await screen.findByText(k('model_withheld_notice_default', { requested: 'model-x' }))).toBeInTheDocument()
  })

  it.each([false, null])('no notice when the verdict is %s', async (verdict) => {
    const running = { ...parent, status: 'running', model_withheld: verdict, served_model: 'model-x' }
    vi.mocked(api.researchCampaigns).mockResolvedValue([running])
    renderWithProviders(<ResearchLabPage />)
    await screen.findByText(k('model_line_picked', { model: 'model-x' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
