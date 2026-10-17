import { describe, it, expect, vi } from 'vitest'

const calls = vi.hoisted(() => [] as string[])
vi.mock('../lib/basePath', () => ({
  installBasePathShims: () => { calls.push('transport') },
  installBasePathDomShims: () => { calls.push('dom') },
}))

describe('installBasePath entry', () => {
  it('installs the transport shims, then the DOM shims, on import', async () => {
    await import('../lib/installBasePath')
    expect(calls).toEqual(['transport', 'dom'])
  })
})
