import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

/**
 * The copy chip's "Copied!" live region sits in the sentence right after the
 * chip. `sr-only` hides it from sight but not from a native selection, so a
 * drag across the chip during the flash copied `111122223333Copied!`. The
 * span must stay out of selections (`select-none`, the same treatment the
 * path chips' hidden full path gets) while still announcing.
 *
 * jsdom applies no stylesheet and its Selection ignores `user-select`, so
 * this pins the class that carries the rule; the browser behaviour was
 * checked in Chromium and Firefox (see the PR).
 */
describe('copy chip status and native selection', () => {
  it('keeps the filled "Copied!" status out of a native selection but still announces it', async () => {
    const { container } = render(
      <MarkdownRenderer content={'Target account `111122223333`, role ReadOnly'} />,
    )
    const chip = container.querySelector('code[data-chip-action="copy"]') as HTMLElement
    expect(chip).toBeTruthy()
    await act(async () => { fireEvent.click(chip) })

    const status = container.querySelector('[role="status"][aria-live="polite"]') as HTMLElement
    expect(status.textContent).toBe('Copied!')
    expect(status.previousElementSibling).toBe(chip)
    expect(status.classList.contains('sr-only')).toBe(true)
    expect(status.classList.contains('select-none')).toBe(true)
  })
})
