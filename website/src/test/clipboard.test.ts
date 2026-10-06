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

  it('writes the text through the copy event even when focus left the staging textarea', async () => {
    // An open modal menu's focus trap pulls focus back off the textarea on
    // select(), so the browser would copy an empty selection and still report
    // success. The fallback must hand the text over in the copy event instead.
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const setData = vi.fn()
    const execCommand = vi.fn(() => {
      const ev = new Event('copy', { bubbles: true, cancelable: true }) as Event & { clipboardData: { setData: typeof setData } }
      ev.clipboardData = { setData }
      document.body.dispatchEvent(ev)
      return true
    })
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })

    await expect(copyToClipboard('assistant reply')).resolves.toBe(true)
    expect(setData).toHaveBeenCalledWith('text/plain', 'assistant reply')
  })

  it('resolves false when neither clipboard path succeeds', async () => {
    writeText.mockRejectedValueOnce(new Error('denied'))
    mockExecCommand(false)

    await expect(copyToClipboard('mobile link')).resolves.toBe(false)
  })

  it('resolves false rather than rejecting when the fallback throws', async () => {
    writeText.mockRejectedValueOnce(new Error('denied'))
    const execCommand = vi.fn(() => {
      throw new Error('no copy')
    })
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })

    await expect(copyToClipboard('mobile link')).resolves.toBe(false)
  })

  it('falls back when navigator.clipboard is entirely absent (non-secure origin)', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const execCommand = mockExecCommand(true)

    await expect(copyToClipboard('mobile link')).resolves.toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
  })
})
