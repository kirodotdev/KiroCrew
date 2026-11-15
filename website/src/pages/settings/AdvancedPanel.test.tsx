import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: vi.fn(async () => ({ agent: { streaming: true } })), patchConfig: vi.fn() },
}))

import { AdvancedPanel } from './AdvancedPanel'

describe('AdvancedPanel', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('frames the list under the anchor the page search lands on', async () => {
    const entries = [{ path: 'agent.streaming', type: 'boolean', label: 'Streaming', help: '', defaultValue: true, allSettings: { mode: 'edit', control: 'bool', resettable: true } }]
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries }), { status: 200 })))
    const { container } = renderWithProviders(<AdvancedPanel />)
    expect(container.querySelector('[data-setting-label="All settings"]')).toBeTruthy()
    expect(await screen.findByText('Streaming')).toBeTruthy()
  })
})
