import { afterEach, describe, expect, it, vi } from 'vitest'
vi.mock('@radix-ui/react-select', async () => await import('../../../test/__mocks__/@radix-ui/react-select'))
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { api } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import { KasCreatorEffortSetting } from './KasWorkflowSettings'

afterEach(() => vi.restoreAllMocks())

const mockGateway = (effort: string, engine = 'kas') => {
  vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine, backend: engine === 'kas' ? 'kas' : '', kas_backend: engine === 'kas', kas_workflows: engine === 'kas' } as Awaited<ReturnType<typeof api.workflowEngine>>)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { kas_creator_effort: effort } } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
  return vi.spyOn(api, 'patchConfig').mockResolvedValue({} as Awaited<ReturnType<typeof api.patchConfig>>)
}

describe('9. Creator step effort (agent.kas_creator_effort)', () => {
  it('lists KAS default plus KAS effort levels and shows the stored value', async () => {
    mockGateway('high')
    renderWithProviders(<KasCreatorEffortSetting />)
    const select = await screen.findByRole('combobox', { name: 'Creator step effort' })
    await waitFor(() => expect(select).toHaveTextContent('High'))
    fireEvent.click(select)
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['Kiro Agent default', 'Low', 'Medium', 'High', 'Extra high', 'Max'])
    expect(screen.getByText(/takes effect the next time a chat starts its session/)).toBeInTheDocument()
  })

  it('writes the chosen level, and "" for KAS default', async () => {
    const patch = mockGateway('high')
    renderWithProviders(<KasCreatorEffortSetting />)
    const select = await screen.findByRole('combobox', { name: 'Creator step effort' })
    await waitFor(() => expect(select).toHaveTextContent('High'))
    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'Kiro Agent default' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.kas_creator_effort', ''))
  })

  it('is disabled off KAS', async () => {
    mockGateway('', 'builtin')
    renderWithProviders(<KasCreatorEffortSetting />)
    const select = await screen.findByRole('combobox', { name: 'Creator step effort' })
    await waitFor(() => expect(select).toHaveTextContent('Kiro Agent default'))
    await waitFor(() => expect(api.workflowEngine).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    fireEvent.click(select)
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
