import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useAgentSync } from '../hooks/useAgentSync'

// #24727: the 45-unit agent name cap must not keep half of an emoji. A lone
// high surrogate before the ellipsis renders as U+FFFD in the scene rail.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

vi.mock('../api/client', () => ({
  api: {
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'radar', default_template: 'atlas' }),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

async function cronName(name: string): Promise<string> {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({
    jobs: [{ id: 'job-1', name, message: 'm', enabled: true, schedule: 'every 5m', last_status: 'ok' }],
  })
  const { result } = renderHookWithProviders(() => useAgentSync(), { store: createTestStore() })
  let shown = ''
  await waitFor(() => {
    const cron = result.current.agents.find(a => a.id === 'cron-job-1')
    expect(cron).toBeDefined()
    shown = cron!.name
  })
  return shown
}

describe('useAgentSync agent name truncation', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('drops an emoji the 45-unit name cap would cut in half', async () => {
    const name = await cronName(`${'a'.repeat(43)}🎉 tail`)
    expect(name).toBe(`${'a'.repeat(43)}…`)
    expect(name).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji that ends exactly at the cut', async () => {
    expect(await cronName(`${'a'.repeat(42)}🎉 tail`)).toBe(`${'a'.repeat(42)}🎉…`)
  })
})
