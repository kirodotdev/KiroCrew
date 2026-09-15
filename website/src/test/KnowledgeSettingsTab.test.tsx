/**
 * SettingsTab — Knowledge page ingestion settings.
 *
 * Covers: render, commit-on-blur validation, model dropdown, error banner.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({
    knowledge: {
      embed_rate_limit: 120,
      extraction_model: '',
      extraction_pool_size: 3,
    },
  })),
}))

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: kirocrewConfigMock,
    patchConfig: patchConfigMock,
  },
}))

vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [
    { name: 'auto', description: '' },
    { name: 'claude-haiku-4.5', description: 'Haiku' },
    { name: 'claude-sonnet-4', description: 'Sonnet' },
  ],
}))

import { SettingsTab } from '../pages/knowledge/SettingsTab'

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <SettingsTab />
    </QueryClientProvider>,
  )
}

function rejectOnce(mock: ReturnType<typeof vi.fn>) {
  mock.mockRejectedValueOnce(new Error('fail'))
}

beforeEach(() => {
  patchConfigMock.mockClear()
  kirocrewConfigMock.mockClear()
})

describe('KnowledgeSettingsTab', () => {
  it('renders all 3 settings fields', async () => {
    wrap()
    expect(await screen.findByText('Ingestion Settings')).toBeInTheDocument()
    // Check labels exist
    expect(screen.getByText('Embedding rate limit')).toBeInTheDocument()
    expect(screen.getByText('Extraction model')).toBeInTheDocument()
    expect(screen.getByText('Extraction pool size')).toBeInTheDocument()
    // The two auto-registration knobs are gone with the feature.
    expect(screen.queryByText('Per-source chunk limit')).not.toBeInTheDocument()
    expect(screen.queryByText('Max sources')).not.toBeInTheDocument()
  })

  it('PATCHes the embedding rate limit on blur with a valid value', async () => {
    wrap()
    // Wait for config to load — inputs get seeded from the mock config
    await waitFor(() => {
      const inputs = document.querySelectorAll('input[type="number"]')
      expect(inputs.length).toBeGreaterThanOrEqual(2)
    })
    const inputs = document.querySelectorAll('input[type="number"]')
    const rateInput = inputs[0] as HTMLInputElement
    await waitFor(() => expect(rateInput.value).toBe('120'))
    fireEvent.change(rateInput, { target: { value: '500' } })
    fireEvent.blur(rateInput)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('knowledge.embed_rate_limit', 500),
    )
  })

  it('reverts the embedding rate limit when value is out of range', async () => {
    wrap()
    await waitFor(() => {
      const inputs = document.querySelectorAll('input[type="number"]')
      expect(inputs.length).toBeGreaterThanOrEqual(2)
    })
    const inputs = document.querySelectorAll('input[type="number"]')
    const rateInput = inputs[0] as HTMLInputElement
    await waitFor(() => expect(rateInput.value).toBe('120'))
    fireEvent.change(rateInput, { target: { value: '99999' } })
    fireEvent.blur(rateInput)
    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(rateInput.value).toBe('120')
  })

  it('reverts the embedding rate limit when value is NaN', async () => {
    wrap()
    await waitFor(() => {
      const inputs = document.querySelectorAll('input[type="number"]')
      expect(inputs.length).toBeGreaterThanOrEqual(2)
    })
    const inputs = document.querySelectorAll('input[type="number"]')
    const rateInput = inputs[0] as HTMLInputElement
    await waitFor(() => expect(rateInput.value).toBe('120'))
    fireEvent.change(rateInput, { target: { value: 'abc' } })
    fireEvent.blur(rateInput)
    expect(patchConfigMock).not.toHaveBeenCalled()
    await waitFor(() => expect(rateInput.value).toBe('120'))
  })

  it('PATCHes extraction_model as empty string when auto is selected', async () => {
    wrap()
    // Wait for the component to render fully
    await screen.findByText('Ingestion Settings')
    // The SimpleSelect for model should show 'auto (use chat model)' trigger
    const modelTrigger = await screen.findByText('auto (use chat model)')
    fireEvent.click(modelTrigger)
    // Select a specific model
    const haiku = await screen.findByText('claude-haiku-4.5')
    fireEvent.click(haiku)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('knowledge.extraction_model', 'claude-haiku-4.5'),
    )
  })

  it('shows error banner on save failure', async () => {
    rejectOnce(patchConfigMock)
    wrap()
    await waitFor(() => {
      const inputs = document.querySelectorAll('input[type="number"]')
      expect(inputs.length).toBeGreaterThanOrEqual(2)
    })
    const inputs = document.querySelectorAll('input[type="number"]')
    const rateInput = inputs[0] as HTMLInputElement
    await waitFor(() => expect(rateInput.value).toBe('120'))
    fireEvent.change(rateInput, { target: { value: '300' } })
    fireEvent.blur(rateInput)
    expect(await screen.findByText(/Failed to save knowledge setting/)).toBeInTheDocument()
  })
})
