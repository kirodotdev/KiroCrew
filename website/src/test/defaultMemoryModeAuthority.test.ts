import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  queryClient,
  resolveDefaultMemoryMode,
  serializeDefaultMemoryModeUpdate,
} from '../api/queryClient'

beforeEach(() => queryClient.clear())

describe('default memory mode authority', () => {
  it('serializes writes in selection order while the latest choice stays authoritative', async () => {
    const order: string[] = []
    let releaseFirst!: () => void
    const first = serializeDefaultMemoryModeUpdate('persistent', async () => {
      order.push('persistent:start')
      await new Promise<void>(resolve => { releaseFirst = resolve })
      order.push('persistent:end')
    })
    const second = serializeDefaultMemoryModeUpdate('temporary', async () => {
      order.push('temporary')
    })
    await Promise.resolve()
    expect(order).toEqual(['persistent:start'])

    const load = vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' }))
    await expect(resolveDefaultMemoryMode(load)).resolves.toBe('temporary')
    expect(load).not.toHaveBeenCalled()

    releaseFirst()
    await Promise.all([first, second])
    expect(order).toEqual(['persistent:start', 'persistent:end', 'temporary'])
  })

  it('retries a config response that overlapped a completed mode write', async () => {
    let releaseRead!: (value: { default_memory_mode: string }) => void
    let reads = 0
    const resolving = resolveDefaultMemoryMode(() => {
      reads += 1
      if (reads === 1) {
        return new Promise(resolve => { releaseRead = resolve })
      }
      return Promise.resolve({ default_memory_mode: 'temporary' })
    })
    await Promise.resolve()

    await serializeDefaultMemoryModeUpdate('temporary', () => Promise.resolve())
    releaseRead({ default_memory_mode: 'persistent' })

    await expect(resolving).resolves.toBe('temporary')
    expect(reads).toBe(2)
  })

  it("verifies the server instead of trusting another tab's stale cache", async () => {
    queryClient.setQueryData(['dashboardConfig'], { default_memory_mode: 'persistent' })
    const load = vi.fn(() => Promise.resolve({ default_memory_mode: 'temporary' }))
    await expect(resolveDefaultMemoryMode(load)).resolves.toBe('temporary')
    expect(load).toHaveBeenCalledOnce()
  })

  it('keeps older backends persistent but fails malformed or unreadable config closed', async () => {
    await expect(resolveDefaultMemoryMode(() => Promise.resolve({}))).resolves.toBe('persistent')
    await expect(
      resolveDefaultMemoryMode(() => Promise.resolve({ default_memory_mode: 'surprise' })),
    ).resolves.toBe('temporary')
    await expect(
      resolveDefaultMemoryMode(() => Promise.reject(new Error('offline'))),
    ).resolves.toBe('temporary')
  })
})
