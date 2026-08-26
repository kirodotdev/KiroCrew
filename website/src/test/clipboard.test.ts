import { describe, it, expect, vi, beforeEach } from 'vitest'
import { copyCode, copyToClipboard } from '../utils/clipboard'

describe('copyCode', () => {
  const writeText = vi.fn().mockResolvedValue(undefined)

  const mockExecCommand = (result: boolean) => {
    const execCommand = vi.fn().mockReturnValue(result)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    return execCommand
  }

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

  it('uses the legacy copy command when Clipboard API access is denied', async () => {
    writeText.mockRejectedValueOnce(new Error('denied'))
    const execCommand = mockExecCommand(true)

    await copyToClipboard('mobile link')

    expect(execCommand).toHaveBeenCalledWith('copy')
  })

  it('resolves false when neither clipboard path succeeds', async () => {
    writeText.mockRejectedValueOnce(new Error('denied'))
    mockExecCommand(false)

    await expect(copyToClipboard('mobile link')).resolves.toBe(false)
  })
})
