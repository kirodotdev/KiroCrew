/**
 * SettingsTab — Knowledge page ingestion settings.
 *
 * Covers: render, commit-on-blur validation, effort selects, error banner.
 * The extraction model / effort / pool-size rows live HERE (this tab is the
 * single home for the Knowledge LLM-pool knobs); ChatPanel carries no
 * Knowledge controls.
 */

// SimpleSelect wraps Radix Select, whose portalled listbox jsdom cannot open;
// the repo's mock (shared with SettingsSelect.test.tsx) makes every picker
// here driveable as real role="option" nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({
    knowledge: {
      embed_rate_limit: 120,
      extraction_model: '',
      extraction_effort: '',
      fetch_effort: '',
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
    { name: 'auto', description: 'Default' },
    { name: 'claude-opus-4.8', description: 'Opus' },
    { name: 'claude-haiku-4.5', description: 'Haiku' },
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

/** Seed the config query with a deep-merged override of the default fixture. */
function seedCfg(knowledge: Record<string, unknown>) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({
      knowledge: {
        embed_rate_limit: 120,
        extraction_model: '',
        extraction_effort: '',
        fetch_effort: '',
        extraction_pool_size: 3,
        ...knowledge,
      },
    }) as never,
  )
}

function rejectOnce(mock: ReturnType<typeof vi.fn>) {
  mock.mockRejectedValueOnce(new Error('fail'))
}

beforeEach(() => {
  patchConfigMock.mockClear()
  kirocrewConfigMock.mockClear()
})

/** Open a SimpleSelect by accessible name; returns its OWN listbox element. */
async function openSelect(label: string): Promise<HTMLElement> {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toBeDisabled())
  fireEvent.click(trigger)
  // A picker's mocked Content stays mounted while the next picker opens, so
  // several role="listbox" nodes can coexist — the LAST one is the fresh one.
  const boxes = screen.getAllByRole('listbox')
  return boxes[boxes.length - 1]
}

describe('KnowledgeSettingsTab', () => {
  it('renders all 5 settings fields and no removed auto-registration rows', async () => {
    wrap()
    expect(await screen.findByText('Ingestion Settings')).toBeInTheDocument()
    // Check labels exist
    expect(screen.getByText('Embedding rate limit')).toBeInTheDocument()
    expect(screen.getByText('Extraction model')).toBeInTheDocument()
    expect(screen.getByText('Extraction Effort')).toBeInTheDocument()
    expect(screen.getByText('URL Fetch Effort')).toBeInTheDocument()
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

  describe('extraction model', () => {
    it('lists auto plus the advertised models', async () => {
      wrap()
      const opts = within(await openSelect('Extraction model')).getAllByRole('option')
      expect(opts.map(o => o.textContent)).toEqual([
        'auto (use chat model)',
        'claude-opus-4.8',
        'claude-haiku-4.5',
      ])
    })

    it('PATCHes the model, translating auto to the empty string', async () => {
      seedCfg({ extraction_model: 'claude-haiku-4.5' })
      wrap()
      await openSelect('Extraction model')
      fireEvent.click(screen.getByRole('option', { name: 'auto (use chat model)' }))
      await waitFor(() =>
        expect(patchConfigMock).toHaveBeenCalledWith('knowledge.extraction_model', ''),
      )
    })

    it('keeps a pinned model selectable when the backend stops listing it', async () => {
      seedCfg({ extraction_model: 'claude-opus-4.7-retired' })
      wrap()
      const opts = within(await openSelect('Extraction model')).getAllByRole('option')
      expect(opts.map(o => o.textContent)).toContain('claude-opus-4.7-retired')
      expect(patchConfigMock).not.toHaveBeenCalled()
    })
  })

  describe('effort selects', () => {
    it('offers the inherit option plus every effort level for both efforts', async () => {
      wrap()
      for (const label of ['Extraction Effort', 'URL Fetch Effort']) {
        // Scope options to the freshly-opened picker: a SimpleSelect's mocked
        // Content stays mounted while the next picker opens, so two
        // role="listbox" nodes coexist by the second iteration.
        const opts = within(await openSelect(label)).getAllByRole('option')
        // '' is the inherit option, labelled like the role-effort rows.
        expect(opts.map(o => o.textContent)).toEqual([
          'Default',
          'Low',
          'Medium',
          'High',
          'Extra High',
          'Max',
        ])
        fireEvent.click(opts[1])
      }
    })

    it('PATCHes Extraction Effort to its own config path', async () => {
      wrap()
      fireEvent.click(within(await openSelect('Extraction Effort')).getByRole('option', { name: 'Low' }))
      await waitFor(() =>
        expect(patchConfigMock).toHaveBeenCalledWith('knowledge.extraction_effort', 'low'),
      )
    })

    it('PATCHes URL Fetch Effort to its own config path', async () => {
      wrap()
      fireEvent.click(within(await openSelect('URL Fetch Effort')).getByRole('option', { name: 'Medium' }))
      await waitFor(() =>
        expect(patchConfigMock).toHaveBeenCalledWith('knowledge.fetch_effort', 'medium'),
      )
    })

    it('surfaces a failed effort write', async () => {
      rejectOnce(patchConfigMock)
      wrap()
      fireEvent.click(within(await openSelect('Extraction Effort')).getByRole('option', { name: 'Low' }))
      expect(await screen.findByText(/Failed to save knowledge setting/)).toBeInTheDocument()
    })

    it('shows the stored effort values once loaded', async () => {
      seedCfg({ extraction_effort: 'xhigh' })
      wrap()
      const trigger = await screen.findByRole('combobox', { name: 'Extraction Effort' })
      await waitFor(() => expect(trigger).toHaveTextContent('Extra High'))
    })
  })

  describe('extraction pool size', () => {
    /** The pool-size input is the second number input on the tab. */
    async function poolInput() {
      wrap()
      await waitFor(() => {
        const inputs = document.querySelectorAll('input[type="number"]')
        expect(inputs.length).toBeGreaterThanOrEqual(2)
      })
      const inputs = document.querySelectorAll('input[type="number"]')
      return inputs[inputs.length - 1] as HTMLInputElement
    }

    it('PATCHes the pool size on blur with an in-range value', async () => {
      const input = await poolInput()
      await waitFor(() => expect(input.value).toBe('3'))
      fireEvent.change(input, { target: { value: '5' } })
      fireEvent.blur(input)
      await waitFor(() =>
        expect(patchConfigMock).toHaveBeenCalledWith('knowledge.extraction_pool_size', 5),
      )
    })

    it.each([
      ['above the ceiling', '11'],
      ['below the floor', '0'],
      ['not a number', 'abc'],
    ])('reverts the pool size and writes nothing when %s', async (_case, typed) => {
      const input = await poolInput()
      await waitFor(() => expect(input.value).toBe('3'))
      fireEvent.change(input, { target: { value: typed } })
      fireEvent.blur(input)
      expect(patchConfigMock).not.toHaveBeenCalled()
      await waitFor(() => expect(input.value).toBe('3'))
    })

    it('reverts the pool size to the server value when the write fails', async () => {
      rejectOnce(patchConfigMock)
      const input = await poolInput()
      await waitFor(() => expect(input.value).toBe('3'))
      fireEvent.change(input, { target: { value: '6' } })
      fireEvent.blur(input)
      expect(await screen.findByText(/Failed to save knowledge setting/)).toBeInTheDocument()
      await waitFor(() => expect(input.value).toBe('3'))
    })
  })
})
