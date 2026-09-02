import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { copyCode, copyToClipboard } from '../utils/clipboard'

/**
 * The async-clipboard path and its textarea fallback. The fallback is what runs
 * on a non-secure origin (or when permission is denied), and it must always
 * remove the scratch textarea again — a leaked node would sit invisible on the
 * page and steal focus/selection on the next copy.
 */
function stubClipboard(writeText: () => Promise<void>): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: vi.fn(writeText) },
  })
}

let execCommand: ReturnType<typeof vi.fn>

beforeEach(() => {
  execCommand = vi.fn(() => true)
  ;(document as unknown as { execCommand: unknown }).execCommand = execCommand
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('copyToClipboard', () => {
  it('uses the async clipboard API when it resolves, with no DOM fallback', async () => {
    stubClipboard(() => Promise.resolve())
    await copyToClipboard('zzz-payload')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('zzz-payload')
    expect(execCommand).not.toHaveBeenCalled()
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('falls back to a hidden textarea + execCommand when the API rejects', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    let seen: HTMLTextAreaElement | null = null
    execCommand.mockImplementation(() => {
      // Captured mid-copy: the node must exist, be off-screen, and hold the text.
      seen = document.querySelector('textarea')
      return true
    })

    await copyToClipboard('zzz-fallback')

    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(seen).not.toBeNull()
    expect(seen!.value).toBe('zzz-fallback')
    expect(seen!.style.position).toBe('fixed')
    expect(seen!.style.opacity).toBe('0')
    // …and it is gone again afterwards.
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('still removes the textarea when the copy command itself throws', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    execCommand.mockImplementation(() => { throw new Error('zzz no copy') })

    await expect(copyToClipboard('zzz-boom')).rejects.toThrow('zzz no copy')
    expect(document.querySelector('textarea')).toBeNull()
  })
})

describe('copyCode', () => {
  it('trims surrounding whitespace so a pasted command lands clean at the prompt', async () => {
    stubClipboard(() => Promise.resolve())
    await copyCode('\n  zzz --run  \n\t')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('zzz --run')
  })

  it('keeps interior whitespace intact', async () => {
    stubClipboard(() => Promise.resolve())
    await copyCode('  zzz one\n  zzz two  ')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('zzz one\n  zzz two')
  })
})
