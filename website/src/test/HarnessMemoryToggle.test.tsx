/**
 * HarnessMemoryToggle — Settings > Agent Harness, KAS row: `agent.harness_memory`.
 *
 * What is pinned:
 * - the switch shows the stored value and writes `agent.harness_memory` through the
 *   ordinary config PATCH;
 * - a gateway whose config does not carry the key gets no switch;
 * - a failed save says so.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { HarnessMemoryToggle } from '../pages/developer/HarnessMemoryToggle'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn().mockResolvedValue({}),
    },
  }
})

const kirocrewConfig = vi.mocked(api.kirocrewConfig)
const patchConfig = vi.mocked(api.patchConfig)

describe('HarnessMemoryToggle', () => {
  beforeEach(() => {
    kirocrewConfig.mockReset()
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({} as never)
  })

  it('shows the stored ON and turns it off', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { harness_memory: true } } as never)
    renderWithProviders(<HarnessMemoryToggle />)
    const sw = await screen.findByRole('switch', { name: 'Harness memory' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.harness_memory', false))
  })

  it('turns a stored OFF on', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { harness_memory: false } } as never)
    renderWithProviders(<HarnessMemoryToggle />)
    const sw = await screen.findByRole('switch', { name: 'Harness memory' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.harness_memory', true))
  })

  it('draws nothing for a gateway that does not carry the key', async () => {
    kirocrewConfig.mockResolvedValue({ agent: {} } as never)
    renderWithProviders(<HarnessMemoryToggle />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByTestId('harness-memory-toggle')).toBeNull()
  })

  it('says so when the save fails', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { harness_memory: true } } as never)
    patchConfig.mockRejectedValue(new Error('nope'))
    renderWithProviders(<HarnessMemoryToggle />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Harness memory' }))
    expect(await screen.findByText('Could not save Harness memory. Try again.')).toBeInTheDocument()
  })
})
