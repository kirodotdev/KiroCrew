import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

/**
 * The copy chip's "Copied!" live region must never land in a native
 * selection: a drag across the chip during the flash once copied
 * `111122223333Copied!`. It is portaled to <body>, outside the rendered text,
 * so no selection of the message can reach it, while it still announces.
 * `select-none` stays on it as a second guard.
 *
 * jsdom applies no stylesheet and its Selection ignores `user-select`, so
 * this pins where the region lives; the browser behaviour was checked in
 * Chromium (see the PR).
 */
describe('copy chip status and native selection', () => {
  it('keeps the filled "Copied!" status out of a native selection but still announces it', async () => {
    const { container } = render(
      <MarkdownRenderer content={'Target account `111122223333`, role ReadOnly'} />,
    )
    const chip = container.querySelector('code[data-chip-action="copy"]') as HTMLElement
    expect(chip).toBeTruthy()
    await act(async () => { fireEvent.click(chip) })

    const status = document.querySelector('[role="status"][aria-live="polite"]') as HTMLElement
    expect(status.textContent).toBe('Copied!')
    expect(container.contains(status)).toBe(false)
    expect(container.textContent).not.toContain('Copied!')
    expect(status.classList.contains('sr-only')).toBe(true)
    expect(status.classList.contains('select-none')).toBe(true)
  })

  it('announces from inside the dialog when the chip sits in a modal one', async () => {
    // A screen reader confined to an aria-modal dialog skips a live region
    // outside it, so the region follows the chip into its dialog.
    const { container } = render(
      <div role="dialog" aria-modal="true" aria-label="Preview">
        <MarkdownRenderer content={'Run `npm test`, and the rest of the line.'} />
      </div>,
    )
    const dialog = container.querySelector('[role="dialog"]') as HTMLElement
    const chip = container.querySelector('code[data-chip-action="copy"]') as HTMLElement
    await act(async () => { fireEvent.focus(chip) })
    const status = document.querySelector('[role="status"][aria-live="polite"]') as HTMLElement
    expect(dialog.contains(status)).toBe(true)
    expect(status.textContent).toBe('')
    expect(status.closest('p')).toBeNull()

    await act(async () => { fireEvent.click(chip) })
    expect(status.isConnected).toBe(true)
    expect(dialog.contains(status)).toBe(true)
    expect(status.textContent).toBe('Copied!')
  })
})
