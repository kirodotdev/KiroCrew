import { describe, it, expect, vi, beforeEach } from 'vitest'
import { copyCode } from '../utils/clipboard'

describe('copyCode', () => {
  const writeText = vi.fn().mockResolvedValue(undefined)

  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  })

  it('strips leading and trailing whitespace from a command', async () => {
    await copyCode('        my-cli    ')
    expect(writeText).toHaveBeenCalledWith('my-cli')
  })

  it('strips surrounding blank lines', async () => {
    await copyCode('\n\n  deploy --now  \n\n')
    expect(writeText).toHaveBeenCalledWith('deploy --now')
  })

  it('preserves internal blank lines', async () => {
    await copyCode('echo a\n\necho b')
    expect(writeText).toHaveBeenCalledWith('echo a\n\necho b')
  })
})
