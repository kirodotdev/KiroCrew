/**
 * Mochi ChatPanel — the assistant message's "Copy markdown" button (`Bubble`).
 *
 * This one used a bare `navigator.clipboard.writeText(text)` with no gate at
 * all — the confirmation ran unconditionally regardless of whether the write
 * ever landed, and a missing `navigator.clipboard` (no secure context) would
 * throw synchronously out of the click handler. It now routes through the
 * shared `copyToClipboard` helper and gates "Copied" on the resolved boolean.
 */
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../../../utils/clipboard'
import { Bubble } from '../src/renderer/ChatPanel'
import { i18nT } from '../../../i18n/t'

afterEach(cleanup)

function assistantMessage(content: string) {
  return { id: 'm1', role: 'assistant' as const, content, timestamp: 1 }
}

describe('mochi ChatPanel copy-markdown button', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(copyToClipboard).mockResolvedValue(true)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('copies the exact message text through the shared helper and confirms, then reverts', async () => {
    render(<Bubble animate={false} message={assistantMessage('the answer is 42')} />)

    const btn = screen.getByLabelText(i18nT('apps.mochi.chatPanel.copy_markdown'))
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('the answer is 42'))
    await waitFor(() =>
      expect(screen.getByLabelText(i18nT('apps.mochi.chatPanel.copied'))).toBeInTheDocument(),
    )

    vi.advanceTimersByTime(1600)
    await waitFor(() =>
      expect(screen.getByLabelText(i18nT('apps.mochi.chatPanel.copy_markdown'))).toBeInTheDocument(),
    )
  })

  it('renders no confirmation when the copy fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    render(<Bubble animate={false} message={assistantMessage('the answer is 42')} />)

    const btn = screen.getByLabelText(i18nT('apps.mochi.chatPanel.copy_markdown'))
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(screen.queryByLabelText(i18nT('apps.mochi.chatPanel.copied'))).toBeNull()
    expect(screen.getByLabelText(i18nT('apps.mochi.chatPanel.copy_markdown'))).toBeInTheDocument()
  })
})
