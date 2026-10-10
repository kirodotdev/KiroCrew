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

  it('resolves false, never rejects, when the copy command itself throws', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    execCommand.mockImplementation(() => { throw new Error('zzz no copy') })

    await expect(copyToClipboard('zzz-boom')).resolves.toBe(false)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('falls back straight to execCommand when navigator.clipboard is absent entirely', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })

    await expect(copyToClipboard('zzz-nonsecure')).resolves.toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('restores focus to the previously focused element after the fallback runs', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    expect(document.activeElement).toBe(input)

    await copyToClipboard('zzz-focus')

    expect(document.activeElement).toBe(input)
    document.body.removeChild(input)
  })

  it('restores focus even when execCommand throws (restore runs in a finally)', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    execCommand.mockImplementation(() => { throw new Error('zzz no copy') })
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    expect(document.activeElement).toBe(input)

    await copyToClipboard('zzz-focus-throw')

    expect(document.activeElement).toBe(input)
    document.body.removeChild(input)
  })

  it('preserves a pre-existing document selection across the fallback copy', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    const p = document.createElement('p')
    p.textContent = 'select me'
    document.body.appendChild(p)
    const range = document.createRange()
    range.selectNodeContents(p)
    const selection = document.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)

    await copyToClipboard('zzz-selection')

    expect(selection.rangeCount).toBe(1)
    expect(selection.getRangeAt(0).toString()).toBe('select me')
    document.body.removeChild(p)
  })

  it('stages a readonly textarea, so focusing it cannot raise a touch keyboard', async () => {
    stubClipboard(() => Promise.reject(new Error('zzz denied')))
    let seenReadOnly: boolean | null = null
    execCommand.mockImplementation(() => {
      seenReadOnly = document.querySelector('textarea')?.readOnly ?? null
      return true
    })

    await copyToClipboard('zzz-readonly')

    expect(seenReadOnly).toBe(true)
  })
})

/**
 * #9920: an unfocused copy (fired from a closing context menu) must fall back
 * to execCommand rather than trust the async API's false-success resolution.
 */
describe('copyToClipboard when the document is not focused (#9920)', () => {
  const setDocumentFocused = (v: boolean) =>
    Object.defineProperty(document, 'hasFocus', { value: () => v, configurable: true })

  afterEach(() => {
    // Restore the happy-dom default so other cases keep seeing a focused doc.
    setDocumentFocused(true)
  })

  it('skips the unfocused async write and uses the execCommand fallback instead', async () => {
    // writeText would RESOLVE here (the false-success shape), but must not be trusted.
    stubClipboard(() => Promise.resolve())
    setDocumentFocused(false)

    await expect(copyToClipboard('zzz-unfocused')).resolves.toBe(true)

    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('reports failure honestly when the fallback cannot copy while unfocused', async () => {
    stubClipboard(() => Promise.resolve())
    setDocumentFocused(false)
    execCommand.mockReturnValue(false)

    // A false tick over an unchanged clipboard is worse than an honest failure.
    await expect(copyToClipboard('zzz-unfocused-fail')).resolves.toBe(false)
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
  })

  it('still uses the async API when the document IS focused', async () => {
    stubClipboard(() => Promise.resolve())
    setDocumentFocused(true)

    await copyToClipboard('zzz-focused')

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('zzz-focused')
    expect(execCommand).not.toHaveBeenCalled()
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
