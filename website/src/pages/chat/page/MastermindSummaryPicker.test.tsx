/**
 * The Mastermind chat's "Summaries: <model>" picker beside the composer's model
 * chip: drawn only on the `mastermind` slot while Mastermind is enabled, it writes
 * `agent.role_models.mastermind` -- the setting Settings > Mastermind edits -- and
 * the two stay in step through the shared config cache.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('../../../test/__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-select', async () => await import('../../../test/__mocks__/@radix-ui/react-select'))

const api = vi.hoisted(() => ({
  mastermind: vi.fn(),
  kirocrewConfig: vi.fn(),
  patchConfig: vi.fn(),
  models: vi.fn(),
  mastermindView: vi.fn(),
  chatSlots: vi.fn(),
}))
vi.mock('../../../api/client', () => ({ api }))

import { MastermindSummaryPicker } from './MastermindSummaryPicker'
import { MastermindPanel } from '../../settings/MastermindPanel'

const INFO = { enabled: true, slot_exists: true, messages: 3, pending_summaries: 0, view_bytes: 0, view_lines: 0, summary_model: 'auto' }
let cfg: { agent: { role_models: Record<string, string> } }

function renderTree(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><Provider store={createTestStore({})}>{ui}</Provider></QueryClientProvider>,
  )
}

beforeEach(() => {
  cfg = { agent: { role_models: { mastermind: '' } } }
  api.mastermind.mockResolvedValue(INFO)
  api.kirocrewConfig.mockImplementation(() => Promise.resolve(structuredClone(cfg)))
  api.patchConfig.mockImplementation((path: string, value: string) => {
    if (path === 'agent.role_models.mastermind') cfg.agent.role_models.mastermind = value
    return Promise.resolve({})
  })
  api.models.mockResolvedValue([{ model_name: 'auto' }, { model_name: 'model-small' }, { model_name: 'model-large' }])
  api.mastermindView.mockResolvedValue({ count: 0, lines: [] })
  api.chatSlots.mockResolvedValue([])
})
afterEach(() => vi.clearAllMocks())

const chip = () => screen.findByTestId('composer-mastermind-summary-chip')

describe('composer — Mastermind summary model picker', () => {
  it('renders nothing on any other slot, and reads nothing', async () => {
    renderTree(<MastermindSummaryPicker slotKey="k1" />)
    expect(screen.queryByTestId('composer-mastermind-summary-chip')).toBeNull()
    expect(api.mastermind).not.toHaveBeenCalled()
  })

  it('renders nothing on the mastermind slot while Mastermind is disabled', async () => {
    api.mastermind.mockResolvedValue({ ...INFO, enabled: false })
    renderTree(<MastermindSummaryPicker slotKey="mastermind" />)
    await waitFor(() => expect(api.mastermind).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-mastermind-summary-chip')).toBeNull()
  })

  it('enabled: shows the summary model and writes agent.role_models.mastermind', async () => {
    renderTree(<MastermindSummaryPicker slotKey="mastermind" />)
    const trigger = await chip()
    await waitFor(() => expect(trigger).not.toBeDisabled())
    expect(trigger).toHaveTextContent('Summaries: Auto (provider picks)')
    fireEvent.click(trigger)
    const menu = await screen.findByTestId('composer-mastermind-summary-menu')
    // The advertised list plus Auto; nothing hardcoded.
    await waitFor(() => expect(within(menu).getAllByRole('menuitemradio').map(i => i.textContent))
      .toEqual(['Auto (provider picks)', 'model-small', 'model-large']))
    fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'model-large' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.role_models.mastermind', 'model-large'))
    await waitFor(() => expect(trigger).toHaveTextContent('Summaries: model-large'))
  })

  it('stays in step with Settings > Mastermind (one config cache)', async () => {
    renderTree(<>
      <MastermindSummaryPicker slotKey="mastermind" />
      <MastermindPanel />
    </>)
    const trigger = await chip()
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    const menu = await screen.findByTestId('composer-mastermind-summary-menu')
    fireEvent.click(await within(menu).findByRole('menuitemradio', { name: 'model-small' }))
    const select = await screen.findByRole('combobox', { name: 'Summary model' })
    await waitFor(() => expect(select).toHaveTextContent('model-small'))
  })
})
