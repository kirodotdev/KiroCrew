import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@playwright/test', () => ({
  defineConfig: (config: unknown) => config,
  devices: { 'Desktop Chrome': {} },
}))

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('dedicated memory evidence output', () => {
  it.each(['', '0', '1'])('only opts in for mode %j', async (mode) => {
    vi.stubEnv('PLAYWRIGHT_RUN_MEMORY_EVIDENCE', mode)
    vi.stubEnv('PLAYWRIGHT_MEMORY_EVIDENCE_OUTPUT_DIR', '/owned-evidence/configured-inactive')
    const { default: config } = await import('../../playwright.config')
    expect(config.outputDir).toBe(mode === '1' ? '/owned-evidence/configured-inactive' : undefined)
  })

  it('keeps the Playwright default without a dedicated output', async () => {
    vi.stubEnv('PLAYWRIGHT_RUN_MEMORY_EVIDENCE', '1')
    vi.stubEnv('PLAYWRIGHT_MEMORY_EVIDENCE_OUTPUT_DIR', undefined)
    const { default: config } = await import('../../playwright.config')
    expect(config.outputDir).toBeUndefined()
  })
})
