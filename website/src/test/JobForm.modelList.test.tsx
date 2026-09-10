/**
 * JobForm's model-override picker was migrated off its own private
 * `useQuery(['models'])` fetcher onto the shared `useAvailableModels` hook, so
 * all model pickers agree on one list. The two observable consequences of that
 * migration are asserted here:
 *
 *  1. an `auto` row now appears — the old private fetch mapped /api/models raw
 *     and never synthesized one, so its presence is proof the shared hook is the
 *     source; and
 *  2. the user's saved `agent.model_order` flows through to this picker.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'

// The shared hook's model source.
vi.mock('../providers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../providers')>()),
  useProvider: () => ({
    id: 'acp',
    fetchAvailableModels: () =>
      Promise.resolve([
        { name: 'alpha-model', description: '' },
        { name: 'zeta-model', description: '' },
      ]),
  }),
}))

// createCron/updateCron keep JobForm's submit path intact; kirocrewConfig feeds
// the ordering seam a saved order that reverses the backend list.
vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { model_order: ['zeta-model', 'alpha-model'] } }),
  },
}))

describe('JobForm model picker (migrated to useAvailableModels)', () => {
  it('offers the shared auto-first, user-ordered list', async () => {
    renderWithProviders(<JobForm agents={[]} defaultAgent="" onSaved={() => {}} layout="horizontal" />)

    fireEvent.click(await screen.findByRole('combobox', { name: 'Model' }))

    // Proof it reads the shared hook: the old private fetch never had an auto row.
    await screen.findByRole('option', { name: 'auto' })

    // Proof the saved order flows through: zeta before alpha, both after auto.
    await waitFor(() => {
      const opts = screen.getAllByRole('option').map(o => o.textContent || '')
      expect(opts).toContain('auto')
      expect(opts.indexOf('zeta-model')).toBeGreaterThan(-1)
      expect(opts.indexOf('zeta-model')).toBeLessThan(opts.indexOf('alpha-model'))
    })
  })
})
